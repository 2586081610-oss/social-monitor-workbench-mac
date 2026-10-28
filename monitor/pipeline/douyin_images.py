"""抖音图集作品下载：F2 详情接口取图片直链 + CDN 直取，全程不打开/播放作品网页。

数据来源：`monitor/platforms/f2_fetch.py --mode detail`（aweme/detail 只读接口，
在隔离 F2 运行时内签名请求）。图片字节经 CDN URL 直接 GET，与网页播放无关。

完整性与安全语义：
- 作品身份核对：详情返回的 aweme_id 必须与请求的作品一致，否则拒绝下载/登记；
- 无图片直链 → 如实报"不是图集"，不伪造下载；
- 逐张校验图片内容（PNG/JPEG/WebP/GIF 魔数），HTML 错误页/损坏数据不算原图；
- **staging 隔离 + 全册验证后原子提交**：先写临时目录并逐张验证，全部通过才
  替换最终文件与 manifest——任何一张失败都不会触碰既有归档与登记文件；
- 复用要求 manifest 源 URL 顺序一致且已登记文件的 size/sha256 校验通过；
- 失败文案走白名单/脱敏，凭据与账号/作品标识不落日志与 API。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from monitor.pipeline.download_result import DownloadResult, MediaFile


class DouyinImageError(RuntimeError):
    """图集下载失败（文案为白名单/脱敏后的安全信息）。"""


MANIFEST_NAME = "douyin_images_manifest.json"

# 魔数 → (规范扩展名, media_type)
_IMAGE_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
)
_WEBP_MAGIC = b"WEBP"  # RIFF....WEBP
_ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _completed_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _detect_image(payload: bytes) -> tuple[str, str] | None:
    """识别图片格式，返回 (扩展名, media_type)；HTML/损坏数据返回 None。"""
    if not payload or len(payload) < 12:
        return None
    for magic, ext, media_type in _IMAGE_MAGIC:
        if payload.startswith(magic):
            return ext, media_type
    if payload[:4] == b"RIFF" and payload[8:12] == _WEBP_MAGIC:
        return ".webp", "image/webp"
    return None


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


def _detail_failure_message(exc: PersonalSourceError) -> str:
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


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _registered_matches(images_dir: Path, urls: list[str], registered_files) -> list[MediaFile] | None:
    """已登记图片与源 URL 顺序、大小、哈希全部匹配 → 可复用。

    登记集中可能含 manifest 等 sidecar，先过滤为图片扩展名；逐文件校验
    size/sha256（与归档登记一致），任何不匹配都整体重新下载（部分复用不算数）。
    """
    image_records = []
    for record in registered_files:
        path_value = record.get("path") if isinstance(record, Mapping) else getattr(record, "path", None)
        if path_value and Path(str(path_value)).suffix.lower() in _IMAGE_EXTS:
            image_records.append(record)
    if len(image_records) != len(urls):
        return None
    image_records.sort(key=lambda r: Path(str(r.get("path") if isinstance(r, Mapping) else r.path)).name)
    manifest = _read_manifest(images_dir)
    if manifest is None or [entry.get("source_url") for entry in manifest] != urls:
        return None
    reusable: list[MediaFile] = []
    base = images_dir.absolute()
    for record, url in zip(image_records, urls):
        raw_path = record.get("path") if isinstance(record, Mapping) else getattr(record, "path", None)
        if not raw_path:
            return None
        path = Path(str(raw_path))
        if path.suffix.lower() not in _IMAGE_EXTS:
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


def download_douyin_images_with_result(
    work,
    images_dir: Path,
    *,
    registered_files: Iterable[MediaFile | Mapping[str, Any]] = (),
) -> tuple[Path | None, DownloadResult]:
    """下载/复用图集并返回 (manifest, 凭据)。失败抛 DouyinImageError（安全文案）。

    下载写入 staging 临时目录并逐张验证（魔数/完整数量），**全部通过后**才
    替换最终文件与 manifest——任何一张失败都不会覆盖既有归档原图。
    """
    images_dir = Path(images_dir)
    try:
        images_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise DouyinImageError("图集作品目录不可用") from None

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
        return manifest, DownloadResult("reused", tuple(reusable), None)

    staging = images_dir / f".staging-{os.getpid()}"
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
            detected = _detect_image(payload)
            if detected is None:
                raise DouyinImageError(f"图片下载失败（第 {index} 张）：内容不是有效图片（可能是错误页或损坏数据）")
            ext, media_type = detected
            target = staging / f"douyin-image-{index}{ext}"
            target.write_bytes(payload)
            staged.append((target, MediaFile(
                path=target, size_bytes=len(payload), media_type="image", sha256=_sha256(payload),
            ), url))
        # 全册验证通过 → 原子提交：逐个替换最终文件（替换前不触碰既有归档）
        written: list[MediaFile] = []
        frames: list[dict] = []
        for target, media, url in staged:
            final = images_dir / target.name
            os.replace(target, final)
            written.append(MediaFile(
                path=final, size_bytes=media.size_bytes, media_type="image", sha256=media.sha256,
            ))
            frames.append({"file": final.name, "source_url": url})
        manifest = _write_manifest(images_dir / MANIFEST_NAME, detail["external_id"], frames)
        return manifest, DownloadResult("downloaded", tuple(written), _completed_at())
    finally:
        shutil.rmtree(staging, ignore_errors=True)  # 失败/中断清理 staging，不损坏原件
