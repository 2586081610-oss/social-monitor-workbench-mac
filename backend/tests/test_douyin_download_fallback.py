"""抖音视频下载分层：jiji 主通道 → F2 API 直取 → 浏览器兜底。

覆盖三件事：
1. 主通道失败且 allow_browser_fallback=True 时，F2 被尝试并成功；
2. F2 也失败时，错误信息按层区分 jiji / f2 / browser；
3. allow_browser_fallback=False 时，浏览器兜底不被触发。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from monitor import jobs
from monitor.pipeline import download as download_mod


def _work() -> SimpleNamespace:
    return SimpleNamespace(
        id=16638,
        platform="douyin",
        external_id="7694857980425434993",
        url="https://www.douyin.com/video/7694857980425434993",
        content_type="video",
    )


def test_primary_failure_then_f2_succeeds_even_when_browser_allowed(tmp_path, monkeypatch):
    calls: list[bool] = []

    def _jiji_fails(platform, url, media_dir, *, registered_files, allow_browser_fallback):
        calls.append(allow_browser_fallback)
        raise download_mod.PipelineOfflineError("douyin downloader produced no newly written original media")

    f2_calls: list[str] = []

    def _f2_ok(work, media_dir, *, registered_files):
        f2_calls.append(work.external_id)
        return object()

    monkeypatch.setattr(download_mod, "download_with_result", _jiji_fails)
    from monitor.pipeline import douyin_video

    monkeypatch.setattr(douyin_video, "download_f2_video_with_result", _f2_ok)

    result, source = jobs._download_douyin_video(
        _work(), tmp_path, registered_files=None, allow_browser_fallback=True
    )

    assert result is not None
    assert source == "monitor.pipeline.douyin_video"
    assert f2_calls == ["7694857980425434993"]
    assert calls == [False]  # 只跑一次主通道，浏览器兜底未被触发


def test_f2_failure_reports_each_layer(tmp_path, monkeypatch):
    calls: list[bool] = []

    def _sequenced(platform, url, media_dir, *, registered_files, allow_browser_fallback):
        calls.append(allow_browser_fallback)
        if allow_browser_fallback:
            raise download_mod.PipelineOfflineError("douyin browser fallback failed")
        raise download_mod.PipelineOfflineError("douyin downloader failed (exit code 1)")

    def _f2_fails(work, media_dir, *, registered_files):
        raise RuntimeError("F2 详情不可用：登录态过期")

    monkeypatch.setattr(download_mod, "download_with_result", _sequenced)
    from monitor.pipeline import douyin_video

    monkeypatch.setattr(douyin_video, "download_f2_video_with_result", _f2_fails)

    with pytest.raises(download_mod.PipelineOfflineError) as caught:
        jobs._download_douyin_video(
            _work(), tmp_path, registered_files=None, allow_browser_fallback=True
        )

    message = str(caught.value)
    assert "jiji=" in message and "exit code 1" in message
    assert "f2=" in message and "F2 详情不可用" in message
    assert "browser=" in message and "browser fallback failed" in message
    # 先主通道（False），再浏览器兜底（True）
    assert calls == [False, True]


def test_browser_fallback_not_triggered_when_disallowed(tmp_path, monkeypatch):
    calls: list[bool] = []

    def _jiji_fails(platform, url, media_dir, *, registered_files, allow_browser_fallback):
        calls.append(allow_browser_fallback)
        raise download_mod.PipelineOfflineError("douyin downloader produced no newly written original media")

    def _f2_fails(work, media_dir, *, registered_files):
        raise RuntimeError("F2 详情不可用：登录态过期")

    monkeypatch.setattr(download_mod, "download_with_result", _jiji_fails)
    from monitor.pipeline import douyin_video

    monkeypatch.setattr(douyin_video, "download_f2_video_with_result", _f2_fails)

    with pytest.raises(download_mod.PipelineOfflineError) as caught:
        jobs._download_douyin_video(
            _work(), tmp_path, registered_files=None, allow_browser_fallback=False
        )

    message = str(caught.value)
    assert "browser=disabled" in message
    assert calls == [False]  # 没有第二次带 allow_browser_fallback=True 的调用
