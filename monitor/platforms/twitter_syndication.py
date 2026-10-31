"""推特/X 单条推文的匿名媒体探测（cdn.syndication.twimg.com，无需登录）。

仅用于单链接下载入口的预识别：
- 判断推文是视频还是图文，决定 content_type 分流（视频走 yt-dlp，图文走
  既有 twitter_image 适配器）；
- 图文时提供 pbs.twimg.com 图片直链清单（写入 works.media_urls）。
任何失败都返回 None（调用方退回视频路径，由下载器如实报错），本模块绝不
读取或存储登录态/Cookie，仅经由 monitor.pipeline.proxy 解析的本机代理出网。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from monitor.pipeline.proxy import resolve_platform_proxy

logger = logging.getLogger("monitor.platforms.twitter_syndication")

_SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"
_PBS_HOST = "pbs.twimg.com"
_TIMEOUT_SECONDS = 12.0


@dataclass(frozen=True)
class TweetMedia:
    text: str = ""
    created_at: str | None = None
    photos: list[str] = field(default_factory=list)
    has_video: bool = False


def _valid_photo_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and (host == _PBS_HOST or host.endswith("." + _PBS_HOST))
        and parsed.port in (None, 443)
    )


def fetch_tweet_media(tweet_id: str) -> TweetMedia | None:
    """匿名探测一条公开推文的媒体构成；不可达/失败一律返回 None。"""
    if not tweet_id or not tweet_id.isdigit():
        return None
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        logger.warning("curl_cffi is unavailable; twitter syndication probe skipped")
        return None

    session = cffi_requests.Session(impersonate="chrome120")
    proxy = resolve_platform_proxy("TWITTER_PROXY")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    try:
        response = session.get(
            _SYNDICATION_URL,
            params={"id": tweet_id, "lang": "zh-cn", "token": "a"},
            timeout=_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - 探测失败不应阻断入口，退回视频路径
        logger.info("twitter syndication probe failed (network); falling back to video")
        return None
    if response.status_code != 200:
        logger.info("twitter syndication probe returned HTTP %s", response.status_code)
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    # 推文不存在/不可匿名访问时 syndication 返回空对象或非对象形状
    if not isinstance(payload, dict) or not payload:
        return None
    media_items = payload.get("mediaDetails")
    if not isinstance(media_items, list):
        media_items = []

    photos: list[str] = []
    has_video = bool(payload.get("videoInfo") or payload.get("video"))
    for item in media_items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type", "")).lower()
        url = item.get("media_url_https") or item.get("url")
        if not isinstance(url, str):
            continue
        if kind == "video":
            has_video = True
            continue
        if kind == "photo" and _valid_photo_url(url) and url not in photos:
            photos.append(url)
    text = payload.get("text")
    created_at = payload.get("created_at")
    return TweetMedia(
        text=text if isinstance(text, str) else "",
        created_at=created_at if isinstance(created_at, str) else None,
        photos=photos,
        has_video=has_video,
    )
