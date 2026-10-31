"""抖音视频直取：F2 详情接口取媒体直链 + CDN 分块下载，全程不打开/播放作品网页。

作为个人来源作品的**第二尝试**接入（jiji262 主路径失败、且浏览器播放兜底被
禁用时启用）；不改变其他来源的既有行为。数据来源与图集相同：
`monitor/platforms/f2_fetch.py --mode detail`（aweme/detail 只读接口）。

完整性与安全语义：
- 作品身份核对（详情 ID 必须与请求作品一致）；
- 下载内容校验：MP4 `ftyp` 魔数 + 容器 box 链完整（`ftyp`/`moov`/非空 `mdat`）、
  `moov` 含视频轨，并通过 macOS AVFoundation（`avconvert` 直通读取）验证可读；
  HTML 错误页、截断、伪 MP4 不算原视频；
- staging 分块写入 + 完成后原子改名，失败不损坏已有文件；
- 多个候选直链按 F2 选优顺序依次尝试；全部失败才如实失败；
- 已登记视频文件 size/sha 校验**且媒体验证通过** → 复用（历史坏文件不再复用）；
- 失败文案白名单/脱敏，凭据与账号/作品标识不落日志与 API。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from monitor.pipeline.download_result import DownloadResult, MediaFile


class DouyinVideoError(RuntimeError):
    """视频直取失败（文案为白名单/脱敏后的安全信息）。"""


MP4_MAGIC = b"ftyp"  # bytes[4:8]
_MIN_VIDEO_BYTES = 32 * 1024  # 小于 32KB 视为异常响应（防 HTML/错误页冒充）
_AVCONVERT = "/usr/bin/avconvert"
_UNREADABLE_VIDEO = "视频下载失败：内容不是可播放的视频（AVFoundation 无法读取）"
_VALIDATOR_UNAVAILABLE = "视频下载失败：媒体验证不可用（缺少 macOS AVFoundation 工具）"
_ALL_CANDIDATES_FAILED = "视频下载失败：所有候选直链均不可用"


def _completed_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stream_url_to_file(url: str, target: Path, *, timeout: float = 60.0) -> int:
    """CDN 分块直取视频字节（无网页、无播放）。独立函数便于离线测试打桩。

    返回写入字节数；非 200 / 空内容 / 非 MP4（ftyp）都抛 DouyinVideoError。
    """
    import httpx

    written = 0
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as response:
        if response.status_code != 200:
            raise DouyinVideoError(f"视频下载失败：CDN 返回 HTTP {response.status_code}")
        header = b""
        with target.open("wb") as fh:
            for chunk in response.iter_bytes(chunk_size=1024 * 256):
                if not chunk:
                    continue
                if written < 8:
                    header += chunk[: 8 - written]
                written += len(chunk)
                fh.write(chunk)
    _validate_video_stream(header, written)
    return written


def _validate_video_stream(header: bytes, total_written: int) -> None:
    """校验视频流：非空、MP4 ftyp 魔数、最小体积（HTML/损坏数据拒绝）。"""
    if total_written == 0:
        raise DouyinVideoError("视频下载失败：CDN 返回空内容")
    if total_written < _MIN_VIDEO_BYTES or header[4:8] != MP4_MAGIC:
        raise DouyinVideoError("视频下载失败：内容不是有效 MP4（可能是错误页或损坏数据）")


_BAD_MP4 = "视频下载失败：内容不是有效 MP4（可能是错误页或损坏数据）"
_MISSING_MP4 = "视频下载失败：内容不是有效 MP4（缺少容器元数据）"
_MAX_MOOV_BYTES = 64 * 1024 * 1024  # moov 正常为几 MB；超限视为伪造/异常


def _mp4_boxes(payload: bytes) -> list[tuple[bytes, bytes]]:
    """解析 MP4 box 链（size/type，支持 64 位 largesize 与 size=0 的末盒）。

    返回 [(box_type, payload)]；任何越界/非法结构都抛 DouyinVideoError。
    """
    boxes: list[tuple[bytes, bytes]] = []
    total = len(payload)
    offset = 0
    while offset < total:
        if total - offset < 8:
            raise DouyinVideoError(_BAD_MP4)
        box_size = int.from_bytes(payload[offset:offset + 4], "big")
        box_type = payload[offset + 4:offset + 8]
        header_len = 8
        if box_size == 1:  # 64 位 largesize
            if total - offset < 16:
                raise DouyinVideoError(_BAD_MP4)
            box_size = int.from_bytes(payload[offset + 8:offset + 16], "big")
            header_len = 16
        elif box_size == 0:  # last box 延伸到末尾
            box_size = total - offset
        if box_size < header_len or offset + box_size > total:
            raise DouyinVideoError(_BAD_MP4)
        if not all(0x20 <= b < 0x7F for b in box_type):  # box 类型必须是可打印 ASCII
            raise DouyinVideoError(_BAD_MP4)
        boxes.append((box_type, payload[offset + header_len:offset + box_size]))
        offset += box_size
    return boxes


def _moov_has_video_track(moov: bytes) -> bool:
    """moov 内是否存在视频轨（moov > trak > mdia > hdlr 的 handler_type 为 vide）。"""
    for box_type, body in _mp4_boxes(moov):
        if box_type != b"trak":
            continue
        for child_type, child in _mp4_boxes(body):
            if child_type != b"mdia":
                continue
            for media_type, media in _mp4_boxes(child):
                if media_type != b"hdlr":
                    continue
                # hdlr: version/flags(4) + pre_defined(4) + handler_type(4)
                if len(media) >= 12 and media[8:12] == b"vide":
                    return True
    return False


def _validate_playable_video(path: Path) -> None:
    """用 macOS AVFoundation（`avconvert` 直通读取）验证轨道与样本表可读。

    仅探测/改写容器，不逐帧解码：可证明 AVFoundation 能解析出可用视频轨并
    读通文件，但不等同于完整解码或真实平台验收。校验器缺失或异常一律拒绝。
    探测输出写入本次调用独占的临时目录（唯一名），结束时只删除该目录，
    绝不覆盖或删除源文件旁边的任何既有文件。
    """
    if sys.platform != "darwin" or not os.path.exists(_AVCONVERT):
        raise DouyinVideoError(_VALIDATOR_UNAVAILABLE)
    try:
        probe_dir = Path(tempfile.mkdtemp(prefix="f2-video-probe-"))
    except OSError:
        raise DouyinVideoError("视频下载失败：媒体验证执行失败") from None
    probe = probe_dir / "probe.mp4"
    try:
        proc = subprocess.run(
            [_AVCONVERT, "--preset", "PresetPassthrough", "--source", str(path),
             "--output", str(probe), "--replace"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        if proc.returncode != 0 or not probe.is_file() or probe.stat().st_size <= 0:
            raise DouyinVideoError(_UNREADABLE_VIDEO)
    except (OSError, subprocess.TimeoutExpired):
        raise DouyinVideoError("视频下载失败：媒体验证执行失败") from None
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


def _validate_mp4_container(path: Path) -> None:
    """MP4 内容校验：容器结构 + 视频轨 + AVFoundation 可读性。

    要求：box 链可完整解析并恰好覆盖整个文件、同时有 `ftyp`、`moov` 与
    非空 `mdat`，`moov` 内含视频轨（handler_type=vide），且 AVFoundation 能
    读取出可用轨道。只有 ftyp 头后接垃圾、截断、只有音频轨、空样本表的
    伪造文件都会被拒绝。
    """
    try:
        size = path.stat().st_size
        if size < 8:
            raise DouyinVideoError(_BAD_MP4)
        has_ftyp = False
        has_mdat = False
        moov = None
        with path.open("rb") as fh:
            offset = 0
            while offset < size:
                fh.seek(offset)
                header = fh.read(8)
                if len(header) < 8:
                    raise DouyinVideoError(_BAD_MP4)
                box_size = int.from_bytes(header[:4], "big")
                box_type = header[4:8]
                header_len = 8
                if box_size == 1:  # 64 位 largesize
                    largesize = fh.read(8)
                    if len(largesize) < 8:
                        raise DouyinVideoError(_BAD_MP4)
                    box_size = int.from_bytes(largesize, "big")
                    header_len = 16
                elif box_size == 0:  # last box 延伸到文件尾
                    box_size = size - offset
                if box_size < header_len or offset + box_size > size:
                    raise DouyinVideoError(_BAD_MP4)
                if not all(0x20 <= b < 0x7F for b in box_type):
                    raise DouyinVideoError(_BAD_MP4)
                if box_type == b"ftyp":
                    has_ftyp = True
                elif box_type == b"mdat":
                    has_mdat = has_mdat or box_size > header_len
                elif box_type == b"moov":
                    body_len = box_size - header_len
                    if body_len > _MAX_MOOV_BYTES:
                        raise DouyinVideoError(_BAD_MP4)
                    moov = fh.read(body_len)
                    if len(moov) != body_len:
                        raise DouyinVideoError(_BAD_MP4)
                offset += box_size
    except OSError:
        raise DouyinVideoError("视频下载失败：文件不可读") from None
    if not has_ftyp or moov is None or not has_mdat:
        raise DouyinVideoError(_MISSING_MP4)
    if not _moov_has_video_track(moov):
        raise DouyinVideoError("视频下载失败：内容不是有效 MP4（没有视频轨）")
    _validate_playable_video(path)


# PersonalSourceError kind → 固定用户文案（与 douyin_images 同约定，不含路径/凭据）
_DETAIL_KIND_MESSAGES = {
    "login_required": "未配置抖音登录态文件，请先完成登录导出（config/f2/douyin_cookie.txt 或 F2_DOUYIN_COOKIE_FILE）",
    "runtime_missing": "F2 隔离运行时未安装（tools/f2-runtime，见其 README）",
    "login_expired": "抖音登录态可能已过期，请重新导出登录态文件",
    "rate_limited": "触发平台限流，请稍后重试",
    "timeout": "作品详情获取超时，请稍后重试",
    "identity_mismatch": "作品详情身份与目标作品不一致，已拒绝下载",
    "invalid_json": "作品详情返回异常，请稍后重试",
    "platform_error": "作品详情返回异常，请稍后重试",
    "schema_changed": "作品详情返回异常，请稍后重试",
}


def _detail_video_url(aweme_id: str) -> tuple[list[str], dict]:
    """取详情中的视频候选直链（保持 F2 选优顺序，可依次尝试）。"""
    from monitor.platforms.douyin_f2_source import PersonalSourceError, read_aweme_detail

    try:
        detail = read_aweme_detail(aweme_id)
    except PersonalSourceError as exc:
        raise DouyinVideoError(_DETAIL_KIND_MESSAGES.get(exc.kind, "作品详情获取失败，请稍后重试")) from None
    if str(detail.get("external_id") or "").strip() != str(aweme_id).strip():
        raise DouyinVideoError(_DETAIL_KIND_MESSAGES["identity_mismatch"])
    raw = detail.get("video_urls")
    candidates = [url for url in raw if isinstance(url, str)] if isinstance(raw, list) else []
    single = detail.get("video_url")
    if isinstance(single, str) and single not in candidates:
        candidates.insert(0, single)
    candidates = [url for url in candidates if url.startswith(("http://", "https://"))]
    if not candidates:
        raise DouyinVideoError("作品详情未包含可下载的视频直链（可能不是视频作品）")
    return candidates, detail


def _registered_video(registered_files, media_dir: Path) -> MediaFile | None:
    """已登记视频文件 size/sha 且媒体验证通过 → 复用；历史坏文件不再视为成功。"""
    for record in registered_files:
        raw_path = record.get("path") if isinstance(record, Mapping) else getattr(record, "path", None)
        if not raw_path or Path(str(raw_path)).suffix.lower() not in {".mp4", ".mov", ".m4v"}:
            continue
        expected_size = record.get("size_bytes") if isinstance(record, Mapping) else getattr(record, "size_bytes", None)
        expected_hash = record.get("sha256") if isinstance(record, Mapping) else getattr(record, "sha256", None)
        if not isinstance(expected_size, int) or expected_size <= 0 or not expected_hash:
            continue
        try:
            path = Path(str(raw_path))
            resolved = path if path.is_absolute() else (media_dir / path)
            info = resolved.stat()
            if info.st_size != expected_size:
                continue
            if _sha256_file(resolved) != str(expected_hash).lower():
                continue
            _validate_mp4_container(resolved)
            return MediaFile(path=resolved, size_bytes=info.st_size, media_type="video", sha256=expected_hash)
        except (OSError, ValueError, DouyinVideoError):
            continue
    return None


def download_f2_video_with_result(
    work,
    media_dir: Path,
    *,
    registered_files: Iterable[MediaFile | Mapping[str, Any]] = (),
) -> DownloadResult:
    """F2 详情直链直取视频并返回下载凭据。失败抛 DouyinVideoError（安全文案）。"""
    media_dir = Path(media_dir)
    try:
        media_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise DouyinVideoError("视频作品目录不可用") from None

    reusable = _registered_video(registered_files, media_dir)
    if reusable is not None:
        return DownloadResult("reused", (reusable,), None)

    aweme_id = str(getattr(work, "external_id", "") or "").strip()
    found, _detail = _detail_video_url(aweme_id)
    candidates = [found] if isinstance(found, str) else list(found or [])
    final = media_dir / f"douyin-video-{aweme_id}.mp4"
    staging = media_dir / f".staging-video-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        tmp_target = staging / final.name
        last_error: DouyinVideoError | None = None
        for url in candidates:
            try:
                size = stream_url_to_file(url, tmp_target)
                digest = _sha256_file(tmp_target)
                _validate_mp4_container(tmp_target)  # 容器 + 视频轨 + AVFoundation 可读
            except DouyinVideoError as exc:
                last_error = exc
                continue
            except Exception as exc:  # noqa: BLE001 - 网络层异常转安全文案
                last_error = DouyinVideoError(f"视频下载失败：{type(exc).__name__}")
                continue
            os.replace(tmp_target, final)  # 全部验证通过才落最终文件
            return DownloadResult("downloaded", (MediaFile(
                path=final, size_bytes=size, media_type="video", sha256=digest,
            ),), _completed_at())
        # 多候选全部失败时给出固定文案，不泄露候选 URL；单候选保留具体原因
        if last_error is not None and len(candidates) == 1:
            raise last_error
        raise DouyinVideoError(_ALL_CANDIDATES_FAILED)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
