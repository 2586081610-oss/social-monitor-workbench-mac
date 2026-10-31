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
from typing import Any
from urllib.parse import urlsplit

from monitor.pipeline.proxy import resolve_platform_proxy

logger = logging.getLogger("monitor.platforms.twitter_syndication")

_SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"
_PBS_HOST = "pbs.twimg.com"
_VIDEO_HOST = "video.twimg.com"
_TIMEOUT_SECONDS = 12.0


@dataclass(frozen=True)
class TweetMedia:
    text: str = ""
    created_at: str | None = None
    photos: list[str] = field(default_factory=list)
    has_video: bool = False
    video_variants: list[str] = field(default_factory=list)


def _valid_photo_url(url: str) -> bool:
    return _valid_media_url(url, _PBS_HOST)


def _valid_media_url(url: str, host: str) -> bool:
    try:
        parsed = urlsplit(url)
        actual = (parsed.hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and (actual == host or actual.endswith("." + host))
        and parsed.port in (None, 443)
    )


def _video_variant_urls(media_items: list[Any]) -> list[str]:
    """从 mediaDetails 的 video_info 变体里取 mp4 直链，码率高的在前。

    仅接受 https 的 video.twimg.com 直链；m3u8/未知域名一律排除——
    syndication 的返回内容不能当作任意 URL 下载源。
    """
    urls: list[tuple[int, str]] = []
    for item in media_items:
        if not isinstance(item, dict) or str(item.get("type", "")).lower() != "video":
            continue
        info = item.get("video_info") or item.get("videoInfo") or {}
        variants = info.get("variants") if isinstance(info, dict) else None
        if not isinstance(variants, list):
            continue
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            if str(variant.get("content_type", "")).lower() != "video/mp4":
                continue
            url = variant.get("content_url") or variant.get("url")
            if not isinstance(url, str) or not _valid_media_url(url, _VIDEO_HOST):
                continue
            # content_type 可能误标：只认 .mp4 路径，排除 .m4a 音频变体
            if not urlsplit(url).path.lower().endswith(".mp4"):
                continue
            try:
                bitrate = int(variant.get("bitrate") or 0)
            except (TypeError, ValueError):
                bitrate = 0
            if url not in [u for _, u in urls]:
                urls.append((bitrate, url))
    urls.sort(key=lambda pair: pair[0], reverse=True)
    return [url for _, url in urls]


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
    video_variants = _video_variant_urls(media_items)
    if video_variants:
        has_video = True
    text = payload.get("text")
    created_at = payload.get("created_at")
    return TweetMedia(
        text=text if isinstance(text, str) else "",
        created_at=created_at if isinstance(created_at, str) else None,
        photos=photos,
        has_video=has_video,
        video_variants=video_variants,
    )
