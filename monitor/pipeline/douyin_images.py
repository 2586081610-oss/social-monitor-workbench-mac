"""抖音图集作品下载：F2 详情接口取图片直链 + CDN 直取，全程不打开/播放作品网页。

数据来源：`monitor/platforms/f2_fetch.py --mode detail`（aweme/detail 只读接口，
在隔离 F2 运行时内签名请求）。图片字节经 CDN URL 直接 GET，与网页播放无关。

语义与 twitter_image 一致（归档凭据复用）：
- 复用要求 manifest 源 URL 顺序一致且已登记文件的 size/sha256 校验通过；
- 部分成功不算成功：任一图片失败 → 抛错，不产生下载凭据；
- 失败文案走白名单/脱敏，凭据与账号/作品标识不落日志与 API。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from monitor.pipeline.download_result import DownloadResult, MediaFile


class DouyinImageError(RuntimeError):
    """图集下载失败（文案为白名单/脱敏后的安全信息）。"""


MANIFEST_NAME = "douyin_images_manifest.json"


def _completed_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_url_bytes(url: str, *, timeout: float = 30.0) -> bytes:
    """CDN 直取图片字节（无网页、无播放）。独立函数便于离线测试打桩。"""
    import httpx

    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    if response.status_code != 200:
        raise DouyinImageError(f"图片 CDN 返回 HTTP {response.status_code}")
    if not response.content:
        raise DouyinImageError("图片 CDN 返回空内容")
    return response.content


def _detail_images(aweme_id: str) -> tuple[list[str], dict]:
    """从 F2 隔离运行时取作品详情的图片直链（子进程，失败 kind 明确）。"""
    from monitor.platforms.douyin_f2_source import PersonalSourceError, read_aweme_detail

    try:
        detail = read_aweme_detail(aweme_id)
    except PersonalSourceError as exc:
        raise DouyinImageError(str(exc)) from None
    urls = [u for u in detail.get("images") or [] if isinstance(u, str) and u.startswith(("http://", "https://"))]
    if not urls:
        raise DouyinImageError("作品详情未包含可下载的图片地址")
    return urls, detail


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


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


def _read_manifest(images_dir: Path) -> list[dict] | None:
    path = images_dir / MANIFEST_NAME
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    frames = doc.get("images") if isinstance(doc, dict) else None
    return frames if isinstance(frames, list) else None


def _write_manifest(images_dir: Path, aweme_id: str, frames: list[dict]) -> Path:
    path = images_dir / MANIFEST_NAME
    path.write_text(json.dumps({
        "aweme_id": aweme_id,
        "saved_at": _completed_at(),
        "images": frames,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def download_douyin_images_with_result(
    work,
    images_dir: Path,
    *,
    registered_files: Iterable[MediaFile | Mapping[str, Any]] = (),
) -> tuple[Path | None, DownloadResult]:
    """下载/复用图集并返回 (manifest, 凭据)。失败抛 DouyinImageError（安全文案）。"""
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
        return _write_manifest(images_dir, detail["external_id"], [
            {"file": media.path.name, "source_url": url}
            for media, url in zip(reusable, urls)
        ]), DownloadResult("reused", tuple(reusable), None)

    written: list[MediaFile] = []
    frames: list[dict] = []
    for index, url in enumerate(urls, start=1):
        target = images_dir / f"douyin-image-{index}.jpg"
        try:
            payload = fetch_url_bytes(url)
        except DouyinImageError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络层异常转安全文案
            raise DouyinImageError(f"图片下载失败（第 {index} 张）：{type(exc).__name__}") from None
        tmp = target.with_suffix(".part")
        tmp.write_bytes(payload)
        os.replace(tmp, target)
        written.append(MediaFile(
            path=target, size_bytes=len(payload), media_type="image", sha256=_sha256(payload),
        ))
        frames.append({"file": target.name, "source_url": url})
    manifest = _write_manifest(images_dir, detail["external_id"], frames)
    return manifest, DownloadResult("downloaded", tuple(written), _completed_at())
