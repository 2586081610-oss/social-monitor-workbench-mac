"""抖音图集作品下载：F2 详情接口取图片直链 + CDN 直取，全程不打开/播放作品网页。

数据来源：`monitor/platforms/f2_fetch.py --mode detail`（aweme/detail 只读接口，
在隔离 F2 运行时内签名请求）。图片字节经 CDN URL 直接 GET，与网页播放无关。

完整性与安全语义：
- 作品身份核对：详情返回的 aweme_id 必须与请求的作品一致，否则拒绝下载/登记；
- 无图片直链 → 如实报"不是图集"，不伪造下载；
- 逐张校验图片内容：魔数预检 + **macOS `sips` 全量解码**（必须真正解码并
  重新编码成功）；HTML 错误页、截断、只有文件头的损坏数据一律拒绝；
- **staging 隔离 + journal 提交协议**：先写提交 journal，再把既有最终文件
  原子备份（临时名 + 改名），替换完成后才写 manifest；任一点崩溃/失败都由
  恢复流程判定阶段：备份中途崩溃不覆盖原件，替换/manifest 失败回滚旧图，
  manifest 已写入则只清理备份；manifest 不可读或回滚未完成时保留备份并
  拒绝开始新提交；已回滚备份保留到下一次成功提交——反复恢复都保持图与
  manifest 一致，且唯一旧副本不会因重试被覆盖；
- 复用映射按 manifest 的文件名/顺序逐项验证（不受文件名字典序影响，支持 >9 张、
  混合扩展名），任何缺行/哈希不匹配都整体重新下载；
- 失败文案走白名单/脱敏，凭据与账号/作品标识不落日志与 API。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

try:  # POSIX 图集级互斥（macOS 目标平台固定可用）
    import fcntl
except ImportError:  # pragma: no cover - 非 POSIX 仅线程锁
    fcntl = None

from monitor.pipeline.download_result import DownloadResult, MediaFile

logger = logging.getLogger("monitor.douyin_images")


class DouyinImageError(RuntimeError):
    """图集下载失败（文案为白名单/脱敏后的安全信息）。"""


class AlbumBusyError(OSError):
    """同一图集已有恢复/提交在进行（同进程线程或跨进程），拒绝并发操作。"""


MANIFEST_NAME = "douyin_images_manifest.json"

# 魔数 → 规范扩展名（快筛；可解码验证交给 _verify_decodable）
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)
_WEBP_MAGIC = b"WEBP"  # RIFF....WEBP
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_SIPS = "/usr/bin/sips"
_LOCK_FILE = ".album.lock"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_HELD = threading.local()


def _thread_lock(key: str) -> threading.RLock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


class _AlbumLock:
    """同一图集的进程内 + 跨进程互斥；不支持时安全拒绝（非阻塞）。

    同一线程可重入（下载入口持锁后再调用提交）；其他线程/进程尝试时立即
    抛 AlbumBusyError，避免相互恢复、覆盖或混档，也不会死等。
    """

    def __init__(self, images_dir: Path) -> None:
        self.key = str(Path(images_dir).resolve())
        self.lock_file = Path(images_dir) / _LOCK_FILE
        self.lock: threading.RLock | None = None
        self.fd: int | None = None
        self.reentered = False

    def __enter__(self) -> "_AlbumLock":
        held = getattr(_THREAD_HELD, "held", None)
        if held is None:
            held = {}
            _THREAD_HELD.held = held
        entry = held.get(self.key)
        if entry is not None:
            entry[0] += 1
            self.reentered = True
            return self
        lock = _thread_lock(self.key)
        if not lock.acquire(blocking=False):
            raise AlbumBusyError("图集正被其他线程处理，已拒绝并发操作")
        fd = None
        try:
            if fcntl is not None:
                fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if fd is not None:
                os.close(fd)
            lock.release()
            raise AlbumBusyError("图集正被其他进程处理或锁文件不可用，已拒绝并发操作") from None
        self.lock, self.fd = lock, fd
        held[self.key] = [1, fd]
        return self

    def __exit__(self, *exc_info) -> None:
        held = getattr(_THREAD_HELD, "held", None) or {}
        if self.reentered:
            entry = held.get(self.key)
            if entry is not None:
                entry[0] -= 1
                if entry[0] <= 0:
                    held.pop(self.key, None)
            return
        held.pop(self.key, None)
        if self.fd is not None:
            try:
                os.close(self.fd)  # 关闭即释放 flock
            except OSError:
                pass
        if self.lock is not None:
            try:
                self.lock.release()
            except RuntimeError:
                pass


def _album_lock(images_dir: Path) -> _AlbumLock:
    return _AlbumLock(images_dir)



def _completed_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _detect_image_magic(payload: bytes) -> str | None:
    """魔数快筛（HTML/文本/未知格式直接拒绝）；通过者仍需可解码验证。"""
    if not payload or len(payload) < 12:
        return None
    for magic, ext in _IMAGE_MAGIC:
        if payload.startswith(magic):
            return ext
    if payload[:4] == b"RIFF" and payload[8:12] == _WEBP_MAGIC:
        return ".webp"
    return None


def _verify_decodable(path: Path) -> str | None:
    """完整可解码验证：魔数 + macOS `sips` 全量解码（真正转码为 PNG）。

    仅用 `sips -g pixelWidth` 读头对截断文件仍会报尺寸（实测），不能证明
    可完整解码；因此要求解码并重新编码成功——只有魔数、截断、CRC/zlib 损坏、
    HTML 错误页都会失败。非 macOS 无法证明可解码 → 一律拒绝（本品仅支持 macOS）。
    返回规范扩展名。
    """
    try:
        with path.open("rb") as stream:
            head = stream.read(16)
    except OSError:
        return None
    detected = _detect_image_magic(head)
    if detected is None:
        return None
    if sys.platform != "darwin" or not os.path.exists(_SIPS):
        return None
    decoded = path.with_name(path.name + ".decode.png")
    try:
        proc = subprocess.run(
            [_SIPS, "-s", "format", "png", str(path), "--out", str(decoded)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        if proc.returncode != 0:
            return None
        if not decoded.is_file() or decoded.stat().st_size <= 0:
            return None
        return detected
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        try:
            decoded.unlink()
        except OSError:
            pass


def fetch_url_bytes(url: str, *, timeout: float = 30.0) -> bytes:
    """CDN 直取图片字节（无网页、无播放）。独立函数便于离线测试打桩。"""
    import httpx

    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    if response.status_code != 200:
        raise DouyinImageError(f"图片下载失败：CDN 返回 HTTP {response.status_code}")
    if not response.content:
        raise DouyinImageError("图片下载失败：CDN 返回空内容")
    return response.content


# PersonalSourceError kind → 固定用户文案（白名单字面量，不含本机路径/凭据）
_DETAIL_KIND_MESSAGES = {
    "login_required": "未配置抖音登录态文件，请先完成登录导出（config/f2/douyin_cookie.txt 或 F2_DOUYIN_COOKIE_FILE）",
    "runtime_missing": "F2 隔离运行时未安装（tools/f2-runtime，见其 README）",
    "login_expired": "抖音登录态可能已过期，请重新导出登录态文件",
    "rate_limited": "触发平台限流，请稍后重试",
    "timeout": "作品详情获取超时，请稍后重试",
    "identity_mismatch": "作品详情身份与目标作品不一致，已拒绝下载",
    "incomplete_album": "作品图集不完整（缺少有效图片直链），已拒绝下载",
    "invalid_json": "作品详情返回异常，请稍后重试",
    "platform_error": "作品详情返回异常，请稍后重试",
    "schema_changed": "作品详情返回异常，请稍后重试",
    "not_image_post": "作品详情未包含可下载的图片地址（可能不是图集作品）",
}


def _detail_failure_message(exc) -> str:
    return _DETAIL_KIND_MESSAGES.get(exc.kind, "作品详情获取失败，请稍后重试")


def _detail_images(aweme_id: str) -> tuple[list[str], dict]:
    """从 F2 隔离运行时取作品详情的图片直链（子进程，失败 kind 明确）。"""
    from monitor.platforms.douyin_f2_source import PersonalSourceError, read_aweme_detail

    try:
        detail = read_aweme_detail(aweme_id)
    except PersonalSourceError as exc:
        raise DouyinImageError(_detail_failure_message(exc)) from None
    if str(detail.get("external_id") or "").strip() != str(aweme_id).strip():
        # 双保险：f2_fetch 已做身份核对，这里对目标作品再核对一次
        raise DouyinImageError(_DETAIL_KIND_MESSAGES["identity_mismatch"])
    urls = [u for u in detail.get("images") or [] if isinstance(u, str) and u.startswith(("http://", "https://"))]
    if not urls:
        raise DouyinImageError(_DETAIL_KIND_MESSAGES["not_image_post"])
    return urls, detail


def _read_manifest(images_dir: Path) -> list[dict] | None:
    path = images_dir / MANIFEST_NAME
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    frames = doc.get("images") if isinstance(doc, dict) else None
    return frames if isinstance(frames, list) else None


def _write_manifest(path: Path, aweme_id: str, frames: list[dict]) -> Path:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({
        "aweme_id": aweme_id,
        "saved_at": _completed_at(),
        "images": frames,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _registered_matches(images_dir: Path, urls: list[str], registered_files) -> list[MediaFile] | None:
    """已登记图片与源 URL 按 **manifest 顺序** 逐项验证后复用。

    manifest 的 (file, source_url) 序列是顺序与映射的权威：逐帧查登记记录、
    校验 size/sha256；不按文件名字典序排序（douyin-image-10 vs -2 的错序陷阱），
    支持 >9 张与混合扩展名。任何缺行/不匹配都整体重新下载。
    """
    frames = _read_manifest(images_dir)
    if frames is None or len(frames) != len(urls):
        return None
    if [entry.get("source_url") for entry in frames] != urls:
        return None
    by_name: dict[str, Any] = {}
    for record in registered_files:
        path_value = record.get("path") if isinstance(record, Mapping) else getattr(record, "path", None)
        if not path_value:
            continue
        name = Path(str(path_value)).name
        if Path(name).suffix.lower() in _IMAGE_EXTS:
            by_name[name] = record
    base = images_dir.absolute()
    reusable: list[MediaFile] = []
    for frame, url in zip(frames, urls):
        name = frame.get("file")
        if not isinstance(name, str) or name not in by_name:
            return None
        record = by_name[name]
        raw_path = record.get("path") if isinstance(record, Mapping) else getattr(record, "path", None)
        if not raw_path:
            return None
        path = Path(str(raw_path))
        if path.suffix.lower() not in _IMAGE_EXTS or path.name != name:
            return None
        try:
            resolved = path if path.is_absolute() else (base / path)
            resolved = Path(os.path.abspath(resolved))
            resolved.relative_to(base)  # 越界拒绝
            info = resolved.stat()
        except (OSError, ValueError):
            return None
        expected_size = record.get("size_bytes") if isinstance(record, Mapping) else getattr(record, "size_bytes", None)
        expected_hash = record.get("sha256") if isinstance(record, Mapping) else getattr(record, "sha256", None)
        if not isinstance(expected_size, int) or expected_size <= 0 or info.st_size != expected_size:
            return None
        if expected_hash:
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
            if digest.lower() != str(expected_hash).lower():
                return None
        reusable.append(MediaFile(
            path=resolved, size_bytes=info.st_size, media_type="image", sha256=expected_hash,
        ))
    return reusable


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _atomic_copy(source: Path, target: Path) -> None:
    """先复制到临时名再原子改名：崩溃时目标要么不存在，要么是完整内容。"""
    tmp = target.with_name(target.name + ".tmp")
    shutil.copy2(source, tmp)
    os.replace(tmp, target)


def _manifest_state(path: Path) -> tuple[str, str | None]:
    """manifest 状态：('present', sha256) / ('missing', None) / ('unknown', None)。

    读取失败（权限/IO）与“不存在”必须区分：前者不能推断提交阶段。
    """
    try:
        return "present", hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unknown", None


def _manifest_digest(path: Path) -> str | None:
    """manifest 内容 sha256；不存在或不可读返回 None（journal 旧摘要用）。"""
    return _manifest_state(path)[1]


def _write_journal(backup_dir: Path, doc: dict) -> None:
    path = backup_dir / "journal.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_journal(backup_dir: Path) -> dict | None:
    """读取并严格校验事务记录；结构不完整/不可识别一律视为无效（不得推断阶段）。"""
    try:
        doc = json.loads((backup_dir / "journal.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _valid_journal(doc)


def _safe_media_name(name: Any) -> bool:
    if not isinstance(name, str) or not name or name in {".", ".."}:
        return False
    if "/" in name or "\\" in name or Path(name).name != name:
        return False
    return Path(name).suffix.lower() in _IMAGE_EXTS


def _valid_journal(doc: Any) -> dict | None:
    """必需字段**必须存在**且类型正确：pid(int)、manifest_sha256(sha256 或显式 null)、
    restore/created（安全图片文件名列表）；可选 aweme_id(str)、rolled_back(bool)。

    字段缺失与显式 null 是不同状态：缺失 = 记录损坏（无法确认阶段）；
    显式 null 才表示“提交前没有 manifest”。"""
    if not isinstance(doc, dict):
        return None
    if any(key not in doc for key in ("pid", "manifest_sha256", "restore", "created")):
        return None
    pid = doc["pid"]
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    digest = doc["manifest_sha256"]
    if digest is not None and not (isinstance(digest, str) and _SHA256_RE.fullmatch(digest)):
        return None
    if "aweme_id" in doc and not isinstance(doc["aweme_id"], str):
        return None
    for key in ("restore", "created"):
        names = doc[key]
        if not isinstance(names, list) or any(not _safe_media_name(name) for name in names):
            return None
    if "rolled_back" in doc and not isinstance(doc["rolled_back"], bool):
        return None
    return doc


def _rollback_album(images_dir: Path, backup_dir: Path, journal: dict) -> bool:
    """按 journal 回滚：只从已完整落地的备份还原，删除本次新增文件。返回是否全部成功。"""
    ok = True
    for name in journal.get("restore") or []:
        backup = backup_dir / name
        if not backup.is_file():  # 备份未完成：原件从未被替换，保持现状
            continue
        try:
            _atomic_copy(backup, images_dir / name)
        except OSError:
            ok = False
            logger.warning("图集回滚失败：备份未恢复 file=%s（保留 %s 供重试）", name, backup_dir.name)
    for name in journal.get("created") or []:
        try:
            (images_dir / name).unlink(missing_ok=True)
        except OSError:
            ok = False
            logger.warning("图集回滚失败：新增文件未删除 file=%s", name)
    return ok


def _mark_rolled_back(backup_dir: Path, journal: dict) -> None:
    journal["rolled_back"] = True
    try:
        _write_journal(backup_dir, journal)
    except OSError:  # 标记失败不影响回滚结果；下次恢复会重复回滚（幂等）
        logger.warning("图集回滚标记写入失败 dir=%s", backup_dir.name)


def _purge_rolled_back(images_dir: Path, keep: Path | None = None) -> None:
    """清理已确认回滚完成的旧备份（新提交成功后旧事务不再需要）。"""
    for backup_dir in images_dir.glob(".backup-*"):
        if not backup_dir.is_dir() or (keep is not None and backup_dir == keep):
            continue
        journal = _read_journal(backup_dir)
        if journal is not None and journal.get("rolled_back"):
            shutil.rmtree(backup_dir, ignore_errors=True)


def _restore_interrupted_backups(images_dir: Path) -> bool:
    """恢复中断的图集提交（可重入），返回是否已无未决备份（False = 不得开始新提交）。

    journal 记录提交前 manifest 摘要与备份/新增清单。判定：
    - manifest 读取失败 → 状态未知，保留备份、阻塞新提交；
    - 当前 manifest 已是新内容 → 提交已生效，只清理备份；
    - 否则回滚（只还原完整备份、删除新增文件），并在 journal 标记 rolled_back
      后保留该备份（唯一旧副本），下次成功提交才清理；
    - 其他进程仍在提交 → 跳过并阻塞；
    - 回滚失败/损坏 journal → 保留备份、阻塞。
    """
    settled = True
    for backup_dir in sorted(images_dir.glob(".backup-*")):
        if not backup_dir.is_dir():
            continue
        journal = _read_journal(backup_dir)
        if journal is None:
            if not any(backup_dir.iterdir()):
                try:
                    backup_dir.rmdir()
                except OSError:
                    pass
            else:
                logger.warning("备份目录缺少有效 journal，保留待人工核对 dir=%s", backup_dir.name)
                settled = False
            continue
        if journal.get("rolled_back"):
            continue  # 已回滚完成，等待下一次成功提交清理
        owner = journal.get("pid")
        if isinstance(owner, int) and owner != os.getpid() and _pid_alive(owner):
            logger.warning("图集提交正在进行，暂不恢复 dir=%s", backup_dir.name)
            settled = False
            continue
        state, current = _manifest_state(images_dir / MANIFEST_NAME)
        if state == "unknown":
            logger.warning("manifest 无法读取，保留备份并阻塞新提交 dir=%s", backup_dir.name)
            settled = False
            continue
        before = journal.get("manifest_sha256")
        committed = (state == "present") if before is None else \
            (state == "present" and current != before)
        if committed:
            shutil.rmtree(backup_dir, ignore_errors=True)
            logger.info("图集提交已完成，清理备份 dir=%s", backup_dir.name)
            continue
        ok = _rollback_album(images_dir, backup_dir, journal)
        if ok and before is not None and state == "missing":
            saved = backup_dir / "manifest.before"
            if saved.is_file():
                try:
                    _atomic_copy(saved, images_dir / MANIFEST_NAME)
                except OSError:
                    ok = False
                    logger.warning("旧 manifest 恢复失败，保留事务 dir=%s", backup_dir.name)
            else:
                ok = False
                logger.warning("缺少旧 manifest 副本，保留事务待人工核对 dir=%s", backup_dir.name)
        if ok:
            _mark_rolled_back(backup_dir, journal)
            logger.info("图集提交中断已回滚（恢复既有原件，备份保留至下次成功提交）")
        else:
            logger.warning("图集回滚未完成，保留备份并阻塞新提交 dir=%s", backup_dir.name)
            settled = False
    return settled


def download_douyin_images_with_result(
    work,
    images_dir: Path,
    *,
    registered_files: Iterable[MediaFile | Mapping[str, Any]] = (),
) -> tuple[Path | None, DownloadResult]:
    """下载/复用图集并返回 (manifest, 凭据)。失败抛 DouyinImageError（安全文案）。

    下载写入 staging 并逐张验证（魔数 + sips 可解码）；全册通过后按 journal
    协议提交（备份→替换→manifest），并先处理上次中断遗留的提交。
    """
    images_dir = Path(images_dir)
    try:
        images_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise DouyinImageError("图集作品目录不可用") from None
    with _album_lock(images_dir):
        return _download_douyin_images_locked(work, images_dir, registered_files=registered_files)


def _download_douyin_images_locked(work, images_dir: Path, *, registered_files) -> tuple[Path | None, DownloadResult]:
    """持图集锁执行：恢复 → 复用/下载 → 提交/清理，全程串行且可安全拒绝并发。"""
    if not _restore_interrupted_backups(images_dir):
        # 状态无法确认或回滚未完成：保留旧备份，禁止继续下载/提交
        raise DouyinImageError("图集存在未完成的恢复，已停止本次下载")

    urls, detail = _detail_images(str(getattr(work, "external_id", "") or ""))
    # 源 URL 记入 media_urls（归档凭据校验与来源追溯用；与 twitter 图集同约定）
    try:
        work.media_urls = json.dumps(urls, ensure_ascii=False)
    except (OSError, ValueError):  # pragma: no cover - 属性写入失败不阻塞下载
        pass

    reusable = _registered_matches(images_dir, urls, registered_files)
    if reusable is not None:
        manifest = _write_manifest(images_dir / MANIFEST_NAME, detail["external_id"], [
            {"file": media.path.name, "source_url": url}
            for media, url in zip(reusable, urls)
        ])
        _purge_rolled_back(images_dir)  # 复用成功即确认图与 manifest 一致，旧回滚备份可清
        return manifest, DownloadResult("reused", tuple(reusable), None)

    staging = images_dir / f".staging-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        staged: list[tuple[Path, MediaFile, str]] = []
        for index, url in enumerate(urls, start=1):
            try:
                payload = fetch_url_bytes(url)
            except DouyinImageError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络层异常转安全文案
                raise DouyinImageError(f"图片下载失败（第 {index} 张）：{type(exc).__name__}") from None
            target = staging / f"douyin-image-{index}.part"
            target.write_bytes(payload)
            detected = _verify_decodable(target)
            if detected is None:
                raise DouyinImageError(
                    f"图片下载失败（第 {index} 张）：内容不是完整可解码的图片（可能是错误页、截断或损坏数据）")
            final_staged = staging / f"douyin-image-{index}{detected}"
            os.replace(target, final_staged)
            staged.append((final_staged, MediaFile(
                path=final_staged, size_bytes=len(payload), media_type="image", sha256=_sha256(payload),
            ), url))
        manifest = _commit_staged_album(images_dir, staged, detail["external_id"])
        written = tuple(MediaFile(
            path=images_dir / staged_path.name,
            size_bytes=media.size_bytes,
            media_type="image",
            sha256=media.sha256,
        ) for staged_path, media, _url in staged)
        return manifest, DownloadResult("downloaded", written, _completed_at())
    finally:
        shutil.rmtree(staging, ignore_errors=True)  # staging 残留清理（提交后应为空）


def _commit_staged_album(images_dir: Path,
                         staged: list[tuple[Path, MediaFile, str]], aweme_id: str) -> Path:
    """全册验证后提交：journal 先行，备份原子落地，manifest 最后写入。

    开始前强制恢复/确认上次事务：存在未决备份（状态未知、回滚失败、其他进程
    仍在提交）时拒绝开始，避免覆盖唯一旧副本。每次提交使用唯一事务目录，
    同进程失败重试也不会复用旧目录。
    任一点崩溃/失败都能由 `_restore_interrupted_backups` 判定：
    备份复制中崩溃 → 原件未被替换，只清临时备份；
    替换中崩溃或 manifest 写入失败 → 从完整备份回滚旧图并保留旧 manifest；
    manifest 已写入后崩溃 → 提交已生效，只清理备份。
    返回 manifest 路径。
    """
    images_dir = Path(images_dir)
    with _album_lock(images_dir):
        return _commit_staged_album_locked(images_dir, staged, aweme_id)


def _settle_failed_commit(images_dir: Path, backup_dir: Path, journal: dict) -> bool:
    """异常中断后的收尾：按 manifest 是否已发布决定“向前完成”或“完整回滚”。

    - 新 manifest 已可见 → 提交实际已生效：保留新图片与清单，只清理备份；
    - 否则回滚图片；若提交前有 manifest 而当前缺失，用备份里的 manifest.before
      恢复旧清单（无副本则保持事务未决）；
    - manifest 状态无法确认/还原失败 → 保留 journal 与备份（未决，阻止后续处理）。
    """
    manifest_path = images_dir / MANIFEST_NAME
    state, current = _manifest_state(manifest_path)
    if state == "unknown":
        logger.warning("提交中断后 manifest 无法读取，保留事务待重试 dir=%s", backup_dir.name)
        return False
    before = journal.get("manifest_sha256")
    committed = (state == "present") if before is None else \
        (state == "present" and current != before)
    if committed:
        # 新清单已发布：中断发生在发布之后，图片已是新版本 → 保留并清理备份
        shutil.rmtree(backup_dir, ignore_errors=True)
        logger.info("提交中断在 manifest 发布之后，按已完成处理 dir=%s", backup_dir.name)
        return True
    ok = _rollback_album(images_dir, backup_dir, journal)
    if ok and before is not None and state == "missing":
        saved = backup_dir / "manifest.before"
        if saved.is_file():
            try:
                _atomic_copy(saved, manifest_path)
            except OSError:
                ok = False
                logger.warning("旧 manifest 恢复失败，保留事务待重试 dir=%s", backup_dir.name)
        else:
            ok = False
            logger.warning("缺少旧 manifest 副本，保留事务待人工核对 dir=%s", backup_dir.name)
    if ok:
        shutil.rmtree(backup_dir, ignore_errors=True)
        logger.info("提交中断已完整回滚（图片与 manifest 均为旧版本）")
    return ok


def _commit_staged_album_locked(images_dir: Path,
                                staged: list[tuple[Path, MediaFile, str]], aweme_id: str) -> Path:
    """持图集锁执行提交；并发提交立即 AlbumBusyError（同线程嵌套可重入）。"""
    if not _restore_interrupted_backups(images_dir):
        raise OSError("图集存在未完成的恢复，拒绝开始新的提交")
    state, before = _manifest_state(images_dir / MANIFEST_NAME)
    if state == "unknown":
        raise OSError("图集 manifest 无法读取，拒绝开始新的提交")
    backup_dir = images_dir / f".backup-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    names = [target.name for target, _media, _url in staged]
    journal = {
        "pid": os.getpid(),
        "aweme_id": aweme_id,
        "manifest_sha256": before,
        "restore": [name for name in names if (images_dir / name).exists()],
        "created": [name for name in names if not (images_dir / name).exists()],
    }
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        _write_journal(backup_dir, journal)
        if before is not None:
            # 保存旧 manifest 字节：中断后回滚可恢复“完整旧版本”
            _atomic_copy(images_dir / MANIFEST_NAME, backup_dir / "manifest.before")
        for target, _media, _url in staged:
            final = images_dir / target.name
            if final.exists():
                _atomic_copy(final, backup_dir / final.name)
            os.replace(target, final)
        frames = [{"file": target.name, "source_url": url} for target, _media, url in staged]
        manifest = _write_manifest(images_dir / MANIFEST_NAME, aweme_id, frames)
    except BaseException:
        _settle_failed_commit(images_dir, backup_dir, journal)
        raise
    else:
        shutil.rmtree(backup_dir, ignore_errors=True)
        _purge_rolled_back(images_dir)
        return manifest
