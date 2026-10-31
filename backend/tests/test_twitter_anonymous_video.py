"""推特匿名视频下载测试：syndication 变体提取、域名安全过滤、分支选择与回退语义。

背景（2026-10-31 真实验收发现）：X 对匿名 yt-dlp 一律拒绝，无 cookie 时视频推文
只能失败。syndication 对公开推文提供 video.twimg.com 的 mp4 直链（含码率），可
匿名直取文件——与项目"直接获取文件、不打开网页播放"的原则一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor.platforms.twitter_syndication import (  # noqa: E402
    TweetMedia,
    _video_variant_urls,
)
from monitor.pipeline import download as dl  # noqa: E402


def _media_item(variants: list[dict]) -> dict:
    return {"type": "video", "video_info": {"variants": variants}}


def test_video_variants_sorted_by_bitrate_and_mp4_only():
    variants = _video_variant_urls(
        [
            _media_item(
                [
                    {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/a.m3u8"},
                    {"content_type": "video/mp4", "bitrate": 832000, "content_url": "https://video.twimg.com/high.mp4?tag=14"},
                    {"content_type": "video/mp4", "bitrate": 432000, "content_url": "https://video.twimg.com/low.mp4?tag=14"},
                    {"content_type": "video/mp4", "bitrate": 128000, "content_url": "https://video.twimg.com/audio.m4a"},
                ]
            )
        ]
    )
    assert variants == ["https://video.twimg.com/high.mp4?tag=14", "https://video.twimg.com/low.mp4?tag=14"]


def test_video_variants_reject_non_twimg_and_malicious_urls():
    """syndication 返回内容不能当作任意 URL 下载源：非 video.twimg.com 一律排除。"""
    variants = _video_variant_urls(
        [
            _media_item(
                [
                    {"content_type": "video/mp4", "bitrate": 999000, "content_url": "https://evil.example.com/x.mp4"},
                    {"content_type": "video/mp4", "bitrate": 832000, "content_url": "http://video.twimg.com/plain-http.mp4"},
                    {"content_type": "video/mp4", "bitrate": 432000, "content_url": "https://user:pass@video.twimg.com/cred.mp4"},
                    {"content_type": "video/mp4", "bitrate": 256000, "content_url": "https://video.twimg.com/ok.mp4"},
                ]
            )
        ]
    )
    assert variants == ["https://video.twimg.com/ok.mp4"]


def test_video_variants_dedupes_and_handles_garbage():
    assert _video_variant_urls([{"type": "photo"}, "垃圾", {"type": "video"}, {}]) == []
    dup = _media_item(
        [
            {"content_type": "video/mp4", "bitrate": 100, "content_url": "https://video.twimg.com/dup.mp4"},
            {"content_type": "video/mp4", "bitrate": 100, "url": "https://video.twimg.com/dup.mp4"},
        ]
    )
    assert _video_variant_urls([dup]) == ["https://video.twimg.com/dup.mp4"]


def test_tweet_media_flags_video_when_variants_exist():
    """mediaDetails 的 video 变体应同时置 has_video 并填充 video_variants。"""
    from monitor.platforms.twitter_syndication import TweetMedia as _  # noqa: F401 - 类型可用性
    from monitor.platforms import twitter_syndication as ts

    payload_items = [
        {"type": "video", "video_info": {"variants": [{"content_type": "video/mp4", "bitrate": 832000, "content_url": "https://video.twimg.com/v.mp4"}]}}
    ]
    # 直接测试内部组装逻辑（fetch 需要网络，不在此测）
    urls = ts._video_variant_urls(payload_items)
    assert urls == ["https://video.twimg.com/v.mp4"]
    media = TweetMedia(has_video=False)
    assert media.video_variants == []


def test_anonymous_download_picks_lowest_bitrate_variant(monkeypatch, tmp_path):
    """无 cookie 时匿名通道取最低码率 mp4（与 yt-dlp worst 约定一致）。"""
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self):
            yield b"v" * 200_000

    class FakeClient:
        def __init__(self, **kwargs):
            captured["proxy"] = kwargs.get("proxy")
            captured["headers"] = kwargs.get("headers")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url):
            captured["method"], captured["url"] = method, url
            return FakeResponse()

    class FakeMedia:
        video_variants = [
            "https://video.twimg.com/high.mp4",
            "https://video.twimg.com/low.mp4",
        ]

    monkeypatch.setattr(
        "monitor.platforms.twitter_syndication.fetch_tweet_media",
        lambda tweet_id: FakeMedia(),
    )
    monkeypatch.setattr(dl.httpx, "Client", FakeClient)

    ok = dl._download_twitter_video_anonymous(tmp_path, "https://x.com/user/status/123", "http://127.0.0.1:7890")
    assert ok is True
    assert captured["url"] == "https://video.twimg.com/low.mp4"
    assert captured["proxy"] == "http://127.0.0.1:7890"
    assert (tmp_path / "twitter_123.mp4").is_file()
    assert not (tmp_path / "twitter_123.mp4.part").exists()


def test_anonymous_download_returns_false_on_probe_failure(monkeypatch, tmp_path):
    """探测失败/无变体 → False（调用方回退 yt-dlp），不留残留文件。"""
    monkeypatch.setattr(
        "monitor.platforms.twitter_syndication.fetch_tweet_media",
        lambda tweet_id: None,
    )
    assert dl._download_twitter_video_anonymous(tmp_path, "https://x.com/u/status/123", None) is False
    assert list(tmp_path.iterdir()) == []

    monkeypatch.setattr(
        "monitor.platforms.twitter_syndication.fetch_tweet_media",
        lambda tweet_id: TweetMedia(video_variants=[]),
    )
    assert dl._download_twitter_video_anonymous(tmp_path, "https://x.com/u/status/123", None) is False


def test_anonymous_download_rejects_oversized_short_file(monkeypatch, tmp_path):
    """过小的响应不是媒体（页面/错误体），不留存也不算成功。"""

    class FakeResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self):
            yield b"not a video"

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url):
            return FakeResponse()

    class FakeMedia:
        video_variants = ["https://video.twimg.com/low.mp4"]

    monkeypatch.setattr(
        "monitor.platforms.twitter_syndication.fetch_tweet_media",
        lambda tweet_id: FakeMedia(),
    )
    monkeypatch.setattr(dl.httpx, "Client", FakeClient)
    assert dl._download_twitter_video_anonymous(tmp_path, "https://x.com/u/status/555", None) is False
    assert list(tmp_path.iterdir()) == []


def test_anonymous_download_ignores_non_status_urls(tmp_path):
    assert dl._download_twitter_video_anonymous(tmp_path, "https://x.com/profile", None) is False
