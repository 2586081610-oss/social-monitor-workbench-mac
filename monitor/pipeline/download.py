"""Platform download adapters with validated, backward-compatible receipts.

``download()`` keeps the original ``-> str`` contract. Archive-aware callers
should use ``download_with_result()`` and pass only media records already
registered for that work. A directory scan alone never makes an old file a
successful download receipt.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .download_result import DownloadResult, MediaFile
from .proxy import resolve_platform_proxy

logger = logging.getLogger("monitor.pipeline.download")

_COOKIE_CONFIG = os.getenv("PLATFORM_COOKIES_FILE", "")
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".flv", ".webm", ".m4v", ".avi", ".ts"}
_AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".flac", ".aac", ".ogg", ".opus"}
# 图文作品（当前仅 xhs 单链接下载）把原始图片存到作品目录根部；
# 抖音/推特图集仍走 images/ 派生目录，不受此扩展影响。
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
_DERIVED_DIRS = {"frames", "images", "transcript", "transcripts", "notes", "transcription"}
_REPARSE_POINT = 0x0400


class PipelineOfflineError(Exception):
    """A downloader is unavailable, failed, or produced no verifiable new media."""


@dataclass(frozen=True)
class _FileObservation:
    size_bytes: int
    mtime_ns: int
    ctime_ns: int
    inode: int


def _project_root() -> Path:
    # download.py is <project>/monitor/pipeline/download.py; parents[2] is
    # the project root. Going one parent farther would leave the project root.
    return Path(__file__).resolve().parents[2]


def _python_executable() -> str:
    """Use the configured helper interpreter when available, else this Python."""
    configured = os.getenv("PYTHON_EXE", "").strip()
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return str(candidate)
        resolved = shutil.which(configured)
        if resolved:
            return resolved
        logger.warning("PYTHON_EXE is unavailable; using the current interpreter")
    return sys.executable


def _load_platform_cookies() -> dict[str, Any]:
    """Load optional platform cookies without logging paths or credential data."""
    path = Path(_COOKIE_CONFIG).expanduser() if _COOKIE_CONFIG else _project_root() / "config" / "platform_cookies.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("platform cookie configuration could not be read")
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_xhs_download_url(url: str) -> str:
    """保留分享短链首跳的作品地址，避免下载器继续跳到登录页面。

    裸域 `xiaohongshu.com` 归一化为 `www.xiaohongshu.com`：XHS-Downloader 的
    链接提取只认 www 域名，裸域会报「提取小红书作品链接失败」但退出码为 0
    （实测 2026-10-31），管道只能看到"无新产出"的离线错误。
    """
    try:
        parsed = urlsplit(url)
        if parsed.hostname and parsed.hostname.lower() == "xiaohongshu.com":
            return urlunsplit(("https", "www.xiaohongshu.com", parsed.path, parsed.query, parsed.fragment))
        if (parsed.hostname or "").lower() not in {"xhslink.com", "xhslink.cn"}:
            return url
        response = httpx.get(url, follow_redirects=False, timeout=10.0)
        location = response.headers.get("location") if 300 <= response.status_code < 400 else None
        if not location:
            return url
        target = urljoin(url, location)
        parsed = urlsplit(target)
        if (
            parsed.scheme == "https"
            and parsed.hostname == "www.xiaohongshu.com"
            and (parsed.path.startswith("/discovery/item/") or parsed.path.startswith("/explore/"))
        ):
            return target
    except (httpx.HTTPError, ValueError):
        pass
    return url


def _run_cli(platform: str, command: list[str], *, cwd: str | Path | None = None) -> None:
    """Run a downloader silently; never echo URLs, cookies, auth paths, or argv."""
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        raise PipelineOfflineError(f"{platform} downloader could not start") from None
    if completed.returncode != 0:
        raise PipelineOfflineError(f"{platform} downloader failed (exit code {completed.returncode})")


def _is_reparse(info) -> bool:
    attrs = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attrs & _REPARSE_POINT)


def _safe_file_under(media_dir: Path, path: Path) -> Path:
    """Resolve an original file below media_dir without following links."""
    base = media_dir.absolute()
    candidate = path if path.is_absolute() else base / path
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("media receipt path leaves its work directory") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("media receipt path is not a file below its work directory")

    current = base
    try:
        base_info = current.lstat()
    except OSError as exc:
        raise ValueError("media work directory cannot be inspected") from exc
    if _is_reparse(base_info):
        raise ValueError("media work directory cannot be a link")
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ValueError("media receipt file is unavailable") from exc
        if _is_reparse(info):
            raise ValueError("media receipt cannot traverse links")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ValueError("media receipt path contains a non-directory component")
        if index == len(relative.parts) - 1 and not stat.S_ISREG(info.st_mode):
            raise ValueError("media receipt is not a regular file")
    resolved = current.resolve(strict=True)
    try:
        resolved.relative_to(base.resolve(strict=True))
    except ValueError as exc:
        raise ValueError("media receipt path leaves its work directory") from exc
    return resolved


def _coerce_registered(item: Any) -> tuple[Path, int, str | None, str | None]:
    """Read an explicit persisted file record; a bare path is not registration."""
    if isinstance(item, MediaFile):
        return item.path, item.size_bytes, item.media_type, item.sha256
    if isinstance(item, Mapping):
        raw_path = item.get("path", item.get("relative_path"))
        size = item.get("size_bytes")
        if raw_path is None or isinstance(size, bool) or not isinstance(size, int):
            raise ValueError("registered media requires a path and validated size")
        media_type = item.get("media_type")
        digest = item.get("sha256")
        if media_type is not None and not isinstance(media_type, str):
            raise ValueError("registered media type must be a string")
        if digest is not None and not isinstance(digest, str):
            raise ValueError("registered media hash must be a string")
        return Path(str(raw_path)), size, media_type, digest
    raise ValueError("registered media must include its persisted file record")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _media_type(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in _VIDEO_EXTENSIONS:
        return "video"
    if suffix in _AUDIO_EXTENSIONS:
        return "audio"
    if suffix in _IMAGE_EXTENSIONS:
        return "image"
    return None


def _is_derived_registered_path(path: Path) -> bool:
    return any(part.casefold() in _DERIVED_DIRS for part in path.parts[:-1])


def _registered_media_files(
    media_dir: Path,
    registered_files: Iterable[Any],
    *,
    include_images: bool = False,
) -> tuple[tuple[MediaFile, ...], bool]:
    """Return verified originals and whether every eligible registration was valid."""
    valid: dict[Path, MediaFile] = {}
    complete = True
    for item in registered_files or ():
        raw_path_value = item.path if isinstance(item, MediaFile) else (
            item.get("path", item.get("relative_path")) if isinstance(item, Mapping) else None
        )
        if raw_path_value is None:
            complete = False
            continue
        try:
            path_hint = Path(raw_path_value)
        except (TypeError, ValueError):
            complete = False
            continue
        if _is_derived_registered_path(path_hint):
            continue
        inferred_hint = _media_type(path_hint)
        if inferred_hint is None or (inferred_hint == "image" and not include_images):
            continue
        try:
            raw_path, expected_size, declared_type, expected_hash = _coerce_registered(item)
        except (TypeError, ValueError):
            complete = False
            continue
        inferred_type = _media_type(raw_path)
        if inferred_type is None or (inferred_type == "image" and not include_images):
            continue
        if declared_type not in {None, inferred_type}:
            complete = False
            continue
        try:
            resolved = _safe_file_under(media_dir, raw_path)
            info = resolved.stat()
            if expected_size <= 0 or info.st_size <= 0 or info.st_size != expected_size:
                complete = False
                continue
            digest = _sha256(resolved) if expected_hash else None
            if expected_hash and digest.lower() != expected_hash.lower():
                complete = False
                continue
            # Recheck the size after hashing to avoid accepting a concurrent edit.
            if resolved.stat().st_size != info.st_size:
                complete = False
                continue
        except (OSError, ValueError):
            complete = False
            continue
        valid[resolved] = MediaFile(resolved, info.st_size, inferred_type, digest or expected_hash)
    files = tuple(valid[path] for path in sorted(valid, key=lambda value: str(value).casefold()))
    return files, complete


def _observe(path: Path) -> _FileObservation | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    if _is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        return None
    return _FileObservation(info.st_size, info.st_mtime_ns, info.st_ctime_ns, getattr(info, "st_ino", 0))


def _allowed_original_extensions(include_images: bool) -> frozenset[str]:
    return frozenset(
        _VIDEO_EXTENSIONS | _AUDIO_EXTENSIONS | (_IMAGE_EXTENSIONS if include_images else set())
    )


def _original_media_paths(media_dir: Path, *, include_images: bool = False) -> list[Path]:
    """Find only original audio/video candidates, excluding known derived folders."""
    allowed = _allowed_original_extensions(include_images)
    found: list[Path] = []
    try:
        candidates = media_dir.rglob("*")
        for path in candidates:
            try:
                relative = path.relative_to(media_dir)
                if any(part.casefold() in _DERIVED_DIRS for part in relative.parts[:-1]):
                    continue
                if path.suffix.lower() not in allowed:
                    continue
                if _observe(path) is not None:
                    _safe_file_under(media_dir, path)
                    found.append(path.resolve(strict=True))
            except (OSError, ValueError):
                continue
    except OSError:
        return []
    return sorted(set(found), key=lambda value: str(value).casefold())


def _snapshot(media_dir: Path, *, include_images: bool = False) -> dict[Path, _FileObservation]:
    return {
        path: observation
        for path in _original_media_paths(media_dir, include_images=include_images)
        if (observation := _observe(path)) is not None
    }


def _new_media_files(
    media_dir: Path,
    before: Mapping[Path, _FileObservation],
    *,
    include_images: bool = False,
) -> tuple[MediaFile, ...]:
    new_files: list[MediaFile] = []
    for path in _original_media_paths(media_dir, include_images=include_images):
        after = _observe(path)
        if after is None:
            continue
        previous = before.get(path)
        if previous is not None and previous == after:
            continue
        try:
            safe_path = _safe_file_under(media_dir, path)
            before_hash_stat = safe_path.stat()
            digest = _sha256(safe_path)
            after_hash_stat = safe_path.stat()
        except (OSError, ValueError):
            continue
        if (
            before_hash_stat.st_size <= 0
            or before_hash_stat.st_size != after_hash_stat.st_size
            or before_hash_stat.st_mtime_ns != after_hash_stat.st_mtime_ns
        ):
            continue
        new_files.append(MediaFile(safe_path, after_hash_stat.st_size, _media_type(safe_path), digest))
    return tuple(new_files)


def _completed_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# 抖音 jiji262 画质档位。480p 是本项目实测的最低含音轨档。
_JIJI_VIDEO_QUALITY = "480p"
_JIJI_QUALITY_NOTE = "# 480p 为本项目最低可转写档位\n"


def _ensure_jiji_config(cfg: Path) -> None:
    """Generate/upgrade the jiji config without logging a machine-specific path."""
    if not cfg.exists():
        cfg.write_text(
            _JIJI_QUALITY_NOTE
            + f"video_quality: {_JIJI_VIDEO_QUALITY}\n"
            + "thread: 3\nretry_times: 3\nprogress:\n  quiet_logs: true\n",
            encoding="utf-8",
        )
        logger.info("created jiji downloader configuration at the default quality")
        return
    text = cfg.read_text(encoding="utf-8")
    if re.search(r"^video_quality:\s*lowest\s*$", text, re.M):
        text = re.sub(r"^video_quality:\s*lowest\s*$", f"video_quality: {_JIJI_VIDEO_QUALITY}", text, flags=re.M)
        cfg.write_text(text, encoding="utf-8")
    elif "video_quality:" not in text:
        cfg.write_text(_JIJI_QUALITY_NOTE + f"video_quality: {_JIJI_VIDEO_QUALITY}\n" + text, encoding="utf-8")


MEDIACRAWLER_PLATFORMS = {"xhs", "douyin", "bilibili", "weibo", "tieba", "zhihu", "kuaishou"}


def download_via_mediacrawler(platform: str, target: str, out_dir: Path) -> str:
    """Run the existing MediaCrawler fallback without echoing the target URL."""
    mc_dir_value = os.getenv("MEDIACRAWLER_DIR", "")
    if not mc_dir_value:
        raise PipelineOfflineError("MediaCrawler is not configured")
    mc_dir = Path(mc_dir_value)
    if not (mc_dir / "main.py").is_file():
        raise PipelineOfflineError("MediaCrawler directory is unavailable")
    command = ["uv", "run", "main.py", "--platform", platform, "--lt", "qrcode", "--type", "detail"]
    _run_cli(platform, command, cwd=mc_dir)
    # The legacy fallback contract returns its output directory for downstream handling.
    return str(out_dir)


_TWITTER_ANONYMOUS_MIN_BYTES = 100_000

def _download_twitter_video_anonymous(media_dir: Path, tweet_url: str, proxy: str | None) -> bool:
    """syndication 匿名视频通道：公开推文的 mp4 直链直接取文件，成功返回 True。

    探测失败、无 mp4 变体、下载异常一律返回 False（调用方回退 yt-dlp）；
    绝不把部分文件当成成功。仅接受 https 的 video.twimg.com 直链。
    """
    match = re.search(r"/status(?:es)?/(\d+)", tweet_url or "")
    if not match:
        return False
    tweet_id = match.group(1)
    try:
        from monitor.platforms.twitter_syndication import fetch_tweet_media

        media = fetch_tweet_media(tweet_id)
    except Exception:  # noqa: BLE001 - 探测层任何异常都走回退
        return False
    if media is None or not media.video_variants:
        return False
    # 变体按码率降序：与既有 yt-dlp "worst" 约定一致，取最低码率 mp4
    variant_url = media.video_variants[-1]
    try:
        import httpx

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Referer": "https://x.com/",
        }
        proxies = proxy or None
        target = media_dir / f"twitter_{tweet_id}.mp4"
        tmp = target.with_name(target.name + ".part")
        downloaded = 0
        with httpx.Client(timeout=60.0, headers=headers, proxy=proxies, follow_redirects=True) as client:
            with client.stream("GET", variant_url) as response:
                if response.status_code != 200:
                    return False
                with open(tmp, "wb") as fh:
                    for chunk in response.iter_bytes():
                        fh.write(chunk)
                        downloaded += len(chunk)
        if downloaded < _TWITTER_ANONYMOUS_MIN_BYTES:
            tmp.unlink(missing_ok=True)
            return False
        os.replace(tmp, target)
        logger.info("twitter anonymous video downloaded: %s bytes", downloaded)
        return True
    except Exception as exc:  # noqa: BLE001 - 下载异常回退 yt-dlp，由它如实报错
        logger.warning("twitter anonymous video download failed: %s", type(exc).__name__)
        return False


def _invoke_downloader(platform: str, url: str, media_dir: Path) -> None:
    if platform == "douyin":
        jiji_dir_value = os.getenv("DOUYIN_DOWNLOADER_DIR", "")
        if not jiji_dir_value:
            raise PipelineOfflineError("Douyin downloader is not configured")
        jiji_dir = Path(jiji_dir_value)
        cfg = jiji_dir / "config.yml"
        try:
            _ensure_jiji_config(cfg)
        except OSError:
            raise PipelineOfflineError("Douyin downloader configuration is unavailable") from None
        _run_cli(
            platform,
            [_python_executable(), "run.py", "-c", str(cfg), "-u", url, "-p", str(media_dir)],
            cwd=jiji_dir,
        )
        return

    if platform == "bilibili":
        try:
            import yutto  # noqa: F401
        except ImportError as exc:
            raise PipelineOfflineError("yutto is not installed") from exc
        command = [sys.executable, "-m", "yutto", "download", url, "-d", str(media_dir), "-q", "16"]
        configured_auth = os.getenv("BILIBILI_AUTH_FILE", "").strip()
        auth_file = Path(configured_auth).expanduser() if configured_auth else _project_root() / "config" / "yutto_auth.toml"
        skip_auth = os.getenv("BILIBILI_SKIP_AUTH", "").strip().casefold() in {"1", "true", "yes", "on"}
        if auth_file.is_file() and not skip_auth:
            command += ["--auth-file", str(auth_file)]
        skip_danmaku = os.getenv("BILIBILI_SKIP_DANMAKU", "").strip().casefold() in {"1", "true", "yes", "on"}
        if skip_danmaku:
            command += ["--no-danmaku"]
        _run_cli(platform, command)
        return

    if platform == "xhs":
        xhs_dir = _project_root() / "tools" / "xhs-downloader"
        script = xhs_dir / "main.py"
        if not script.is_file():
            raise PipelineOfflineError("XHS downloader is not configured")
        cookie_section = _load_platform_cookies().get("xhs", {})
        cookie_string = cookie_section.get("cookie", "") if isinstance(cookie_section, dict) else ""
        command = [
            sys.executable,
            str(script),
            "--url", _resolve_xhs_download_url(url),
            "--work_path", str(media_dir),
            "--folder_mode", "False",
            "--video_cover_download", "False",
            "--download_record", "False",
        ]
        # XHS-Downloader 的网页 Cookie 标注"无需登录"：未配置时匿名尝试公开作品，
        # 失败由"no newly written original media"如实报错，不再强制要求 Cookie。
        if isinstance(cookie_string, str) and cookie_string:
            command += ["--cookie", cookie_string]
        proxy = resolve_platform_proxy("XHS_PROXY")
        if proxy:
            command += ["--proxy", proxy, "--proxy_download", "True"]
        _run_cli(
            platform,
            command,
            cwd=xhs_dir,
        )
        return

    if platform == "twitter":
        try:
            import yt_dlp  # noqa: F401
        except ImportError as exc:
            raise PipelineOfflineError("yt-dlp is not installed") from exc
        cookie_section = _load_platform_cookies().get("twitter", {})
        cookies_file = cookie_section.get("cookies_file", "") if isinstance(cookie_section, dict) else ""
        # 代理：用户显式 TWITTER_PROXY 优先，其次标准环境变量/macOS 系统设置（只读，
        # 不硬编码端口）；解析不到则直连。仅推特子进程使用，不改动全局代理。
        proxy = resolve_platform_proxy("TWITTER_PROXY")
        if not cookies_file:
            # 匿名基础路径：syndication 对公开推文提供 video.twimg.com 的 mp4 直链，
            # 无需登录即可取文件（与"不打开网页播放"原则一致）；探测失败再走
            # yt-dlp 尝试，由它如实报错。
            if _download_twitter_video_anonymous(media_dir, url, proxy):
                return
            logger.warning("Twitter cookies are not configured; syndication variant unavailable, trying yt-dlp")
        command = [sys.executable, "-m", "yt_dlp", "-f", "worst[acodec!=none][vcodec!=none]/worstvideo+worstaudio/worst", url]
        if cookies_file:
            command += ["--cookies", str(cookies_file)]
        if proxy:
            command += ["--proxy", proxy]
        command += ["-o", str(media_dir / "%(title)s.%(ext)s")]
        _run_cli(platform, command)
        return

    if platform in MEDIACRAWLER_PLATFORMS:
        download_via_mediacrawler(platform, url, media_dir)
        return
    raise PipelineOfflineError(f"unsupported downloader platform: {platform}")


def _download_douyin_browser(url: str, media_dir: Path) -> None:
    """Run the optional Mac browser fallback in a bounded private scratch dir."""
    try:
        with tempfile.TemporaryDirectory(prefix='.douyin-fallback-', dir=media_dir) as scratch:
            result = subprocess.run(
                [sys.executable, '-m', 'monitor.pipeline.douyin_browser', url, scratch],
                cwd=_project_root(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=150, check=False,
            )
            files = list(Path(scratch).glob('douyin-*.mp4'))
            if result.returncode or len(files) != 1 or files[0].is_symlink() or not files[0].is_file():
                raise PipelineOfflineError('douyin browser fallback failed')
            # Link within the same filesystem without overwriting an existing file.
            os.link(files[0], media_dir / files[0].name)
    except (OSError, subprocess.TimeoutExpired):
        raise PipelineOfflineError('douyin browser fallback could not complete') from None


def download_with_result(
    platform: str,
    url: str,
    media_dir: Path,
    *,
    registered_files: Iterable[MediaFile | Mapping[str, Any]] = (),
    allow_browser_fallback: bool = True,
    include_images: bool | None = None,
) -> DownloadResult:
    """Download video/audio and return only verified new or registered original media.

    ``registered_files`` accepts the shared ``MediaFile`` type or persisted
    mappings with ``path``, ``size_bytes``, and optional ``sha256``/``media_type``.
    Relative paths are interpreted from ``media_dir``. Unregistered existing
    files are snapshots only; an unchanged stale file can never become success.
    ``allow_browser_fallback=False`` disables the Mac browser-playback fallback;
    automatic personal-source syncs must not open or play work pages.
    ``include_images=None`` 按平台推断：xhs 单链接图文/视频统一从作品目录根部
    接收图片凭据；其余平台维持仅音视频凭据的既有规则。
    """
    media_dir = Path(media_dir)
    try:
        media_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise PipelineOfflineError("media work directory is unavailable") from None

    if include_images is None:
        include_images = platform == "xhs"

    reusable, registrations_complete = _registered_media_files(
        media_dir, registered_files, include_images=include_images
    )
    if reusable and registrations_complete:
        logger.info("%s media receipt reused %d registered file(s)", platform, len(reusable))
        return DownloadResult("reused", reusable, None)

    before = _snapshot(media_dir, include_images=include_images)
    fallback_available = (
        allow_browser_fallback and platform == 'douyin' and sys.platform == 'darwin'
    )
    primary_failed = False
    try:
        _invoke_downloader(platform, url, media_dir)
    except PipelineOfflineError:
        if not fallback_available:
            raise
        primary_failed = True
    written = () if primary_failed else _new_media_files(media_dir, before, include_images=include_images)
    if not written and fallback_available:
        # Ignore files left by a failed primary downloader; only new validated
        # files produced by the fallback can become its download receipt.
        before_fallback = _snapshot(media_dir, include_images=include_images)
        _download_douyin_browser(url, media_dir)
        written = _new_media_files(media_dir, before_fallback, include_images=include_images)
    if not written:
        raise PipelineOfflineError(f"{platform} downloader produced no newly written original media")
    result = DownloadResult("downloaded", written, _completed_at())
    logger.info("%s media receipt downloaded %d validated file(s)", platform, len(result.files))
    return result


def _find_video(media_dir: Path) -> str:
    """Legacy helper retained for callers that only need one media path."""
    candidates = _original_media_paths(Path(media_dir))
    for path in candidates:
        if path.suffix.lower() in _VIDEO_EXTENSIONS:
            return str(path)
    raise PipelineOfflineError("download produced no video file")


def download(platform: str, url: str, media_dir: Path) -> str:
    """Backward-compatible string API; archive-aware callers use download_with_result."""
    result = download_with_result(platform, url, media_dir)
    for item in result.files:
        if item.media_type == "video":
            return str(item.path)
    return str(result.files[0].path)
