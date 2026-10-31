"""单链接下载入口的基本契约：只处理用户指定的作品。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.repositories import work_repo
from app.services import link_analysis_service as service_module


@pytest.mark.parametrize(
    ("url", "platform", "external_id"),
    [
        ("https://www.bilibili.com/video/BVtest411c7mD", "bilibili", "BVtest411c7mD"),
        ("https://www.xiaohongshu.com/explore/673adb17000000001d037a1f", "xhs", "673adb17000000001d037a1f"),
        ("https://x.com/example/status/1234567890123456789", "twitter", "1234567890123456789"),
    ],
)
def test_single_link_download_starts_only_requested_work(client, auth_headers, monkeypatch, url, platform, external_id):
    launched = []
    monkeypatch.setattr(service_module, "_spawn_single_link_runner", lambda work_id: launched.append(work_id) or True)
    monkeypatch.setattr(
        service_module.link_analysis_service,
        "_probe_twitter_content",
        lambda _id: ("image", ["https://pbs.twimg.com/media/test.jpg"], "test"),
    )
    no_metadata = SimpleNamespace(
        title=SimpleNamespace(verified=False),
        published_at=SimpleNamespace(verified=False),
        metadata_checked_at=None,
        conflicts=[],
    )
    monkeypatch.setattr(service_module, "resolve_archive_metadata", lambda *_args, **_kwargs: no_metadata)

    response = client.post("/api/v1/link-analyses/download", json={"url": url}, headers=auth_headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["platform"], data["external_id"]) == (platform, external_id)
    assert data["processing"] is True
    assert launched == [data["work"]["id"]]
    if platform == "twitter":
        assert data["content_type"] == "image"


def test_downloaded_work_remains_visible_through_work_api(client, auth_headers, db):
    work = work_repo.upsert(
        db, "xhs", "test-downloaded-single", title="测试作品", url="https://example.com/test",
        source_type="manual", status="DOWNLOADED",
    )
    db.commit()

    response = client.get(f"/api/v1/works/{work.id}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "DOWNLOADED"


def test_twitter_probe_imports_without_loading_a_real_account():
    from monitor.platforms.twitter_syndication import fetch_tweet_media

    assert fetch_tweet_media("not-an-id") is None


def test_xhs_cn_share_link_is_recognized_after_redirect():
    from app.services.link_analysis_service import parse_link_identity

    identity = parse_link_identity(
        "https://xhslink.cn/o/test",
        resolve_redirect=lambda _url: "https://www.xiaohongshu.com/explore/673adb17000000001d037a1f",
    )
    assert identity.platform == "xhs"
    assert identity.external_id == "673adb17000000001d037a1f"


def test_xhs_short_link_stops_at_work_url_before_login_redirect(monkeypatch):
    from app.services.link_analysis_service import parse_link_identity

    calls = []
    def redirect(url, **_kwargs):
        calls.append(url)
        return SimpleNamespace(
            status_code=302,
            headers={"location": "https://www.xiaohongshu.com/discovery/item/673adb17000000001d037a1f?xsec_token=synthetic"},
            url=url,
        )

    monkeypatch.setattr(service_module.httpx, "get", redirect)
    identity = parse_link_identity("https://xhslink.com/o/test")
    assert (identity.platform, identity.external_id) == ("xhs", "673adb17000000001d037a1f")
    assert calls == ["https://xhslink.com/o/test"]


def test_xhs_download_does_not_skip_due_to_downloader_global_history(tmp_path, monkeypatch):
    from monitor.pipeline import download

    script = tmp_path / "tools" / "xhs-downloader" / "main.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    commands = []
    monkeypatch.setattr(download, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(download, "_load_platform_cookies", lambda: {})
    monkeypatch.setattr(download, "_resolve_xhs_download_url", lambda url: url)
    monkeypatch.setattr(download, "_run_cli", lambda _platform, command, **_kwargs: commands.append(command))
    download._invoke_downloader("xhs", "https://xhslink.com/o/test", tmp_path)
    command = commands[0]
    assert command[command.index("--download_record") + 1] == "False"


def test_xhs_downloader_uses_expanded_link_and_configured_proxy(tmp_path, monkeypatch):
    from monitor.pipeline import download

    script = tmp_path / "tools" / "xhs-downloader" / "main.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    commands = []
    target = "https://www.xiaohongshu.com/discovery/item/673adb17000000001d037a1f?xsec_token=synthetic"
    monkeypatch.setattr(download, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(download, "_load_platform_cookies", lambda: {})
    monkeypatch.setattr(download, "resolve_platform_proxy", lambda _name: "http://127.0.0.1:9999")
    monkeypatch.setattr(download.httpx, "get", lambda *_args, **_kwargs: SimpleNamespace(status_code=302, headers={"location": target}))
    monkeypatch.setattr(download, "_run_cli", lambda _platform, command, **_kwargs: commands.append(command))
    download._invoke_downloader("xhs", "https://xhslink.com/o/test", tmp_path)
    command = commands[0]
    assert command[command.index("--url") + 1] == target
    assert command[command.index("--proxy") + 1] == "http://127.0.0.1:9999"
    assert command[command.index("--proxy_download") + 1] == "True"


@pytest.mark.parametrize(("platform", "suffix"), [("bilibili", ".mp4"), ("xhs", ".jpg"), ("twitter", ".mp4")])
def test_single_link_runner_records_simulated_media_and_reports_success(db, client, auth_headers, monkeypatch, platform, suffix):
    from app.models import ArchiveEntry
    from monitor import jobs
    from monitor.pipeline.single_link import run_single_link_download

    work = work_repo.upsert(
        db, platform, f"synthetic-{platform}", title="测试作品", url="https://example.com/test",
        source_type="manual", status="QUEUED", content_type="video",
    )
    work.published_at = "2026-05-01T08:00:00Z"
    work.published_time_source = "test"
    work.published_time_precision = "second"
    work.published_time_timezone = "UTC"
    db.commit()

    monkeypatch.setattr(
        jobs.download, "_invoke_downloader",
        lambda _platform, _url, directory: (directory / f"original{suffix}").write_bytes(b"synthetic media"),
    )
    monkeypatch.setattr(jobs, "_sync_checkpoint_after_commit", lambda *_args: None)

    result = run_single_link_download(work.id)
    assert result["status"] == "DOWNLOADED"
    db.refresh(work)
    assert work.media_path and work.downloaded_at
    assert db.query(ArchiveEntry).filter_by(external_id=work.external_id).count() == 1
    detail = client.get(f"/api/v1/works/{work.id}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "DOWNLOADED"
    local = client.get(f"/api/v1/works/{work.id}/local-view", headers=auth_headers)
    assert local.status_code == 200
    assert any(item["name"] == f"original{suffix}" for item in local.json()["data"]["media_files"])


def test_single_link_runner_reports_download_failure(db, monkeypatch):
    from monitor import jobs
    from monitor.pipeline.download import PipelineOfflineError
    from monitor.pipeline.single_link import run_single_link_download

    work = work_repo.upsert(
        db, "xhs", "synthetic-failure", title="测试失败", url="https://example.com/test",
        source_type="manual", status="QUEUED", content_type="video",
    )
    db.commit()

    def fail(*_args):
        raise PipelineOfflineError("xhs downloader failed (exit code 1)")

    monkeypatch.setattr(jobs.download, "_invoke_downloader", fail)
    result = run_single_link_download(work.id)
    assert result["status"] == "FAILED"


def test_twitter_image_post_is_saved_and_visible(db, client, auth_headers, monkeypatch):
    from monitor import jobs
    from monitor.pipeline import twitter_image
    from monitor.pipeline.single_link import run_single_link_download

    work = work_repo.upsert(
        db, "twitter", "synthetic-twitter-image", title="测试图片", url="https://example.com/test",
        source_type="manual", status="QUEUED", content_type="image",
    )
    work.media_urls = json.dumps(["https://pbs.twimg.com/media/test.jpg"])
    db.commit()

    class Session:
        def get(self, *_args, **_kwargs):
            return SimpleNamespace(content=b"\xff\xd8\xffsynthetic jpeg", raise_for_status=lambda: None)

        def close(self):
            pass

    monkeypatch.setattr(twitter_image, "_session", Session)
    monkeypatch.setattr(jobs, "_sync_checkpoint_after_commit", lambda *_args: None)
    assert run_single_link_download(work.id)["status"] == "DOWNLOADED"
    response = client.get(f"/api/v1/works/{work.id}/local-view", headers=auth_headers)
    assert response.status_code == 200
    assert any(item["name"] == "001.jpg" for item in response.json()["data"]["media_files"])
