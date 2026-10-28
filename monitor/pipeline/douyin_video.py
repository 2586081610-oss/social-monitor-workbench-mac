"""抖音视频直取：F2 详情接口取媒体直链 + CDN 分块下载，全程不打开/播放作品网页。

作为个人来源作品的**第二尝试**接入（jiji262 主路径失败、且浏览器播放兜底被
禁用时启用）；不改变其他来源的既有行为。数据来源与图集相同：
`monitor/platforms/f2_fetch.py --mode detail`（aweme/detail 只读接口）。

完整性与安全语义：
- 作品身份核对（详情 ID 必须与请求作品一致）；
- 下载内容校验 MP4 `ftyp` 魔数（HTML 错误页/损坏数据不算原视频）；
- staging 分块写入 + 完成后原子改名，失败不损坏已有文件；
- 已登记视频文件 size/sha 校验通过 → 复用（归档凭据语义与全局一致）；
- 失败文案白名单/脱敏，凭据与账号/作品标识不落日志与 API。
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


class DouyinVideoError(RuntimeError):
    """视频直取失败（文案为白名单/脱敏后的安全信息）。"""


MP4_MAGIC = b"ftyp"  # bytes[4:8]
_MIN_VIDEO_BYTES = 32 * 1024  # 小于 32KB 视为异常响应（防 HTML/错误页冒充）


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


def _detail_video_url(aweme_id: str) -> tuple[str, dict]:
    from monitor.platforms.douyin_f2_source import PersonalSourceError, read_aweme_detail

    try:
        detail = read_aweme_detail(aweme_id)
    except PersonalSourceError as exc:
        raise DouyinVideoError(_DETAIL_KIND_MESSAGES.get(exc.kind, "作品详情获取失败，请稍后重试")) from None
    if str(detail.get("external_id") or "").strip() != str(aweme_id).strip():
        raise DouyinVideoError(_DETAIL_KIND_MESSAGES["identity_mismatch"])
    url = detail.get("video_url")
    if not (isinstance(url, str) and url.startswith(("http://", "https://"))):
        raise DouyinVideoError("作品详情未包含可下载的视频直链（可能不是视频作品）")
    return url, detail


def _registered_video(registered_files, media_dir: Path) -> MediaFile | None:
    """已登记视频文件 size/sha 校验通过 → 可复用（取第一个匹配项）。"""
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
            return MediaFile(path=resolved, size_bytes=info.st_size, media_type="video", sha256=expected_hash)
        except (OSError, ValueError):
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
    url, _detail = _detail_video_url(aweme_id)
    final = media_dir / f"douyin-video-{aweme_id}.mp4"
    staging = media_dir / f".staging-video-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        tmp_target = staging / final.name
        try:
            size = stream_url_to_file(url, tmp_target)
        except DouyinVideoError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络层异常转安全文案
            raise DouyinVideoError(f"视频下载失败：{type(exc).__name__}") from None
        digest = _sha256_file(tmp_target)
        os.replace(tmp_target, final)  # 全部验证（ftyp/大小/哈希）后才落最终文件
        return DownloadResult("downloaded", (MediaFile(
            path=final, size_bytes=size, media_type="video", sha256=digest,
        ),), _completed_at())
    finally:
        shutil.rmtree(staging, ignore_errors=True)
