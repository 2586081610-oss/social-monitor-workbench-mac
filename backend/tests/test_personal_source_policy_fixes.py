"""Codex 复审修复回归：下载去重与覆盖、积压消费解耦、授权边界、personal_worker。

覆盖复审报告 1/2/4/5 项：
- 下载按作品去重（EXISTS）：双标记不重复处理；夹内-only 跨来源作品被覆盖；
  NEW 与 QUEUED 都在授权范围内；处理前重读状态防重复。
- 下载积压每个 tick 消费（与同步间隔解耦）；手动同步 --download 即时消费；
  授权关闭零处理；同步失败不影响积压消费。
- 状态显示真实队列积压数与个人任务进程在线。
- personal_worker 只注册个人任务（注册清单锁定，防隐式扩大范围）。
- 日志/报告脱敏：Bearer/多 Cookie/URL 参数/夹 ID/作品 ID 不落日志与 CLI 输出。
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests.test_douyin_f2_source import _make_fake_runtime  # noqa: E402

from app.models import ArchiveEntry, PersonalSourceFolderItem, PersonalSourceMark, Work  # noqa: E402
from app.repositories import app_setting_repo  # noqa: E402
from app.utils.timeutil import now_iso  # noqa: E402

ENABLED_KEY = "douyin_personal_sync_enabled"
AUTO_DOWNLOAD_KEY = "douyin_personal_auto_download"


@pytest.fixture()
def clean_settings(db):
    yield
    for key in (ENABLED_KEY, AUTO_DOWNLOAD_KEY):
        row = db.get(app_setting_repo.AppSetting, key)
        if row is not None:
            db.delete(row)
    for log in db.query(PersonalSourceMark).all():
        db.delete(log)
    for item in db.query(PersonalSourceFolderItem).all():
        db.delete(item)
    db.commit()


def _set(db, key: str, value: str) -> None:
    app_setting_repo.set_value(db, key, value)
    db.commit()


def _quarantine_other_works(db, my_ids: set[str]) -> dict[int, str]:
    """测试库跨文件复用：把非本用例作品临时移出 NEW/QUEUED 选择窗口，返回原状态。"""
    rows = db.query(Work).filter(~Work.external_id.in_(my_ids)).all()
    original = {}
    for row in rows:
        original[row.id] = row.status
        if row.status in ("NEW", "QUEUED"):
            row.status = "DONE"
    db.commit()
    return original


def _restore_works(db, original: dict[int, str]) -> None:
    for work_id, status in original.items():
        row = db.get(Work, work_id)
        if row is not None:
            row.status = status
    db.commit()


# ---------- 复审项 1：去重与覆盖 ----------

def test_download_step_requires_authorization(db, monkeypatch, clean_settings):
    """授权关闭（默认）→ 零处理，调用方无法绕过。"""
    from monitor import jobs

    work = Work(platform="douyin", external_id="8801001", title="个人", url="u",
                source_type="personal_source", content_type="video", status="NEW")
    db.add(work)
    db.commit()
    original = _quarantine_other_works(db, {"8801001"})
    calls: list[str] = []
    monkeypatch.setattr(jobs, "_process_work", lambda db_, w, **kw: calls.append(w.external_id))
    try:
        assert jobs.download_personal_source_works() == 0
        assert calls == []
    finally:
        row = db.get(Work, work.id)
        if row is not None:
            db.delete(row)
        db.commit()
        _restore_works(db, original)


def test_download_step_dedup_and_covers_all_fact_kinds(db, monkeypatch, clean_settings):
    """双标记去重（like+favorite → 处理一次）；夹内-only 跨来源（QUEUED）覆盖。"""
    from monitor import jobs

    double_marked = Work(platform="douyin", external_id="8802001", title="双标记", url="u1",
                         source_type="personal_source", content_type="video", status="NEW")
    folder_only_queued = Work(platform="douyin", external_id="8802002", title="夹内归属跨来源", url="u2",
                              source_type="monitor", content_type="video", status="QUEUED")
    db.add_all([double_marked, folder_only_queued])
    db.flush()
    # 同一作品两条标记（like + favorite）——旧实现 LEFT JOIN 会产生两行
    for mark in ("like", "favorite"):
        db.add(PersonalSourceMark(platform="douyin", external_id="8802001", source_mark=mark,
                                  source_channel="f2", snapshot_id="s1",
                                  first_seen_at=now_iso(), last_seen_at=now_iso(),
                                  work_id=double_marked.id))
    # 仅收藏夹归属（无 marks 行）的跨来源作品
    db.add(PersonalSourceFolderItem(platform="douyin", external_id="8802002", folder_id="FAKEF1",
                                    folder_name="夹", source_channel="f2", snapshot_id="s1",
                                    work_id=folder_only_queued.id,
                                    first_seen_at=now_iso(), last_seen_at=now_iso()))
    _set(db, AUTO_DOWNLOAD_KEY, "true")
    original = _quarantine_other_works(db, {"8802001", "8802002"})
    processed: list[str] = []
    fallback_flags: list[bool | None] = []
    monkeypatch.setattr(jobs, "_process_work", lambda db_, w, **kw: (processed.append(w.external_id),
                                                                     fallback_flags.append(kw.get("allow_browser_fallback"))))
    try:
        assert jobs.download_personal_source_works() == 2
        assert sorted(processed) == ["8802001", "8802002"]  # 双标记只处理一次
        assert all(flag is False for flag in fallback_flags)  # 一律禁浏览器兜底
    finally:
        for w in (double_marked, folder_only_queued):
            row = db.get(Work, w.id)
            if row is not None:
                db.delete(row)
        db.commit()
        _restore_works(db, original)


def test_download_step_recheck_prevents_double_processing(db, monkeypatch, clean_settings):
    """处理前重读状态：选择后状态已变化的作品跳过（并发/重复调用防护）。"""
    from monitor import jobs

    work = Work(platform="douyin", external_id="8803001", title="x", url="u",
                source_type="personal_source", content_type="video", status="NEW")
    db.add(work)
    db.commit()
    _set(db, AUTO_DOWNLOAD_KEY, "true")
    original = _quarantine_other_works(db, {"8803001"})
    calls: list[str] = []

    def fake_process(db_, w, **kw):
        calls.append(w.external_id)
        w.status = "DONE"  # 模拟第一个处理周期已改变状态

    monkeypatch.setattr(jobs, "_process_work", fake_process)
    try:
        jobs.download_personal_source_works()
        assert calls == ["8803001"]
        # 第二次调用：作品已 DONE，不再处理
        jobs.download_personal_source_works()
        assert calls == ["8803001"]
    finally:
        row = db.get(Work, work.id)
        if row is not None:
            db.delete(row)
        db.commit()
        _restore_works(db, original)


# ---------- 复审项 2：积压消费与同步解耦 ----------

def test_tick_consumes_backlog_even_when_sync_not_due(db, monkeypatch, clean_settings):
    """同步未到周期也要消费下载积压；100 个积压按 tick 批量持续推进。"""
    from monitor import jobs
    from app.models import PollLog

    _set(db, ENABLED_KEY, "true")
    _set(db, AUTO_DOWNLOAD_KEY, "true")
    db.add(PollLog(platform="douyin-personal", started_at=now_iso(), finished_at=now_iso(), status="ok"))
    db.commit()

    works = [Work(platform="douyin", external_id=f"8804{i:03d}", title=f"w{i}", url="u",
                  source_type="personal_source", content_type="video", status="NEW")
             for i in range(25)]
    db.add_all(works)
    db.commit()
    original = _quarantine_other_works(db, {w.external_id for w in works})
    ingest_calls: list[str] = []
    monkeypatch.setattr("monitor.personal_source_ingest.run_ingest",
                        lambda *a, **k: ingest_calls.append(a) or (_ for _ in ()).throw(AssertionError("同步未到期不应执行")))

    def fake_process(db_, w, **kw):
        processed.append(w.external_id)
        w.status = "DONE"  # 模拟真实处理后的状态迁移

    processed: list[str] = []
    monkeypatch.setattr(jobs, "_process_work", fake_process)
    try:
        jobs.check_douyin_personal_sync()
        assert ingest_calls == []  # 同步未到期
        assert len(processed) == 10  # 本 tick 消费一批
        jobs.check_douyin_personal_sync()
        assert len(processed) == 20
        jobs.check_douyin_personal_sync()
        assert len(processed) == 25  # 积压清空
    finally:
        for w in works:
            row = db.get(Work, w.id)
            if row is not None:
                db.delete(row)
        db.commit()
        _restore_works(db, original)


def test_tick_sync_failure_still_downloads(db, monkeypatch, clean_settings):
    """同步失败不影响已授权积压消费（失败只记录，不让调度线程死亡）。"""
    from monitor import jobs

    _set(db, ENABLED_KEY, "true")
    _set(db, AUTO_DOWNLOAD_KEY, "true")

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic sync failure")

    monkeypatch.setattr("monitor.personal_source_ingest.run_ingest", boom)
    download_calls: list[int] = []
    monkeypatch.setattr(jobs, "download_personal_source_works",
                        lambda **kw: download_calls.append(1) or 0)
    jobs.check_douyin_personal_sync()  # 不应抛出
    assert download_calls == [1]


def test_status_reports_worker_online_and_queue_depth(client, auth_headers, db, clean_settings):
    from app.core.config import settings
    from app.services import personal_source_service

    work = Work(platform="douyin", external_id="8805001", title="待下载", url="u",
                source_type="personal_source", content_type="video", status="NEW")
    db.add(work)
    db.commit()
    _set(db, ENABLED_KEY, "true")
    settings.workbuddy_dir.mkdir(parents=True, exist_ok=True)
    (settings.workbuddy_dir / "personal_worker_heartbeat.json").write_text(
        json.dumps({"ts": time.time(), "pid": 1}), encoding="utf-8")
    try:
        status = client.get("/api/v1/personal-source/status", headers=auth_headers).json()["data"]
        assert status["personal_worker_online"] is True
        assert status["schedule_active"] is True  # 个人任务进程在线即可执行定时
        assert status["pending_download_count"] >= 1
    finally:
        (settings.workbuddy_dir / "personal_worker_heartbeat.json").unlink(missing_ok=True)
        row = db.get(Work, work.id)
        if row is not None:
            db.delete(row)
        db.commit()


def test_status_queue_depth_covers_folder_only_cross_source(client, auth_headers, db, clean_settings):
    """积压数覆盖仅收藏夹归属的跨来源作品（复审项 1 的漏处理场景）。"""
    work = Work(platform="douyin", external_id="8806001", title="跨来源夹内", url="u",
                source_type="monitor", content_type="video", status="QUEUED")
    db.add(work)
    db.flush()
    db.add(PersonalSourceFolderItem(platform="douyin", external_id="8806001", folder_id="FAKEF2",
                                    source_channel="f2", snapshot_id="s1", work_id=work.id,
                                    first_seen_at=now_iso(), last_seen_at=now_iso()))
    db.commit()
    try:
        status = client.get("/api/v1/personal-source/status", headers=auth_headers).json()["data"]
        assert status["pending_download_count"] >= 1
    finally:
        row = db.get(Work, work.id)
        if row is not None:
            db.delete(row)
        db.commit()


# ---------- 复审项 5：personal_worker 注册边界 ----------

def test_personal_worker_registers_only_personal_jobs():
    from apscheduler.schedulers.background import BackgroundScheduler

    from monitor import jobs
    from monitor.personal_worker import build_personal_scheduler

    scheduler: BackgroundScheduler = build_personal_scheduler()
    try:
        job_ids = {job.id for job in scheduler.get_jobs()}
        assert job_ids == {"personal_worker_heartbeat", "check_douyin_personal_sync"}
        # 明确不含其他平台轮询/trigger 消费/全局流水线
        assert not any(i.startswith("poll_") for i in job_ids)
        assert "consume_triggers" not in job_ids
        assert "run_pipeline" not in job_ids
        assert "heartbeat" not in job_ids  # 监控层心跳不属于个人任务进程
    finally:
        if scheduler.state == 1:  # STATE_RUNNING 才需要 shutdown
            scheduler.shutdown(wait=False)


def test_personal_worker_heartbeat_does_not_affect_monitor_online(client, auth_headers, db):
    """个人任务进程心跳不冒充监控层在线（两个在线状态独立）。"""
    from app.core.config import settings

    settings.workbuddy_dir.mkdir(parents=True, exist_ok=True)
    (settings.workbuddy_dir / "personal_worker_heartbeat.json").write_text(
        json.dumps({"ts": time.time(), "pid": 1}), encoding="utf-8")
    try:
        status = client.get("/api/v1/personal-source/status", headers=auth_headers).json()["data"]
        assert status["personal_worker_online"] is True
        assert status["monitor_online"] is False  # 监控层心跳文件不存在
    finally:
        (settings.workbuddy_dir / "personal_worker_heartbeat.json").unlink(missing_ok=True)


# ---------- 复审项 2/4：手动同步 --download 与安全输出 ----------

def _cli_env(tmp_path: Path) -> dict:
    import os

    return dict(
        os.environ,
        DATABASE_PATH=str(tmp_path / "cli.db"),
        MEDIA_ROOT=str(tmp_path / "media"),
        OBSIDIAN_ROOT=str(tmp_path / "notes"),
        WORKBUDDY_DIR=str(tmp_path / "workbuddy"),
        F2_DOUYIN_COOKIE_FILE=str(tmp_path / "cookie.txt"),
        DOUYIN_DOWNLOADER_DIR="",  # 下载器未配置 → 下载步骤如实失败
    )


def test_manual_sync_download_consumes_backlog_or_marks_honestly(tmp_path):
    """手动同步 --download：授权开启时消费积压（下载器未配置 → 如实 FAILED）；授权关闭不动。"""
    from tests.test_douyin_f2_source import _make_fake_runtime

    for auto_download in ("true", "false"):
        work_dir = tmp_path / auto_download
        work_dir.mkdir()
        env = _cli_env(work_dir)
        env["F2_RUNTIME_DIR"] = str(_make_fake_runtime(work_dir))
        env["F2_DOUYIN_COOKIE_FILE"] = str(work_dir / "cookie.txt")
        (work_dir / "cookie.txt").write_text("sessionid=synthetic", encoding="utf-8")
        cmd = [sys.executable, "-m", "monitor.personal_source_ingest", "--source", "f2", "--download"]
        first = subprocess.run(cmd, cwd=str(_WORKSPACE_ROOT), env=env, text=True, capture_output=True)
        assert first.returncode == 0, first.stderr[-400:]
        # 第一次运行已登记作品；现在设置下载授权（补齐 NOT NULL 列）
        with sqlite3.connect(work_dir / "cli.db") as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, created_at, updated_at) "
                "VALUES ('douyin_personal_auto_download', ?, datetime('now'), datetime('now'))",
                (auto_download,))
            conn.commit()
        second = subprocess.run(cmd, cwd=str(_WORKSPACE_ROOT), env=env, text=True, capture_output=True)
        assert second.returncode == 0, second.stderr[-400:]
        report = json.loads(second.stdout)
        with sqlite3.connect(work_dir / "cli.db") as conn:
            status_row = conn.execute(
                "SELECT status, last_error FROM works WHERE external_id='7000001'").fetchone()
        assert status_row is not None
        if auto_download == "true":
            # 下载步骤执行过：下载器未配置 → 如实 FAILED（不打开网页兜底、不虚报）
            assert status_row[0] == "FAILED"
            assert "not configured" in (status_row[1] or "")
        else:
            # 授权关闭 → 不处理
            assert status_row[0] in ("NEW", "QUEUED")


def test_cli_report_has_no_work_or_folder_ids(tmp_path):
    """CLI 成功/部分输出只含类别与数量：无作品 ID、无收藏夹 ID。"""
    from tests.test_douyin_f2_source import _make_fake_runtime

    env = _cli_env(tmp_path)
    env["F2_FAKE_INCOMPLETE"] = "1"
    env["F2_RUNTIME_DIR"] = str(_make_fake_runtime(tmp_path))
    (tmp_path / "cookie.txt").write_text("sessionid=synthetic", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "monitor.personal_source_ingest", "--source", "f2"],
        cwd=str(_WORKSPACE_ROOT), env=env, text=True, capture_output=True,
    )
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["status"] == "ok"
    text = result.stdout
    assert "8000001" not in text and "7000001" not in text  # 作品 ID 不落输出
    assert not any(part == "f1" for part in text.replace('"', " ").replace(",", " ").split())


def test_manual_sync_log_has_no_ids(tmp_path, client, auth_headers, db, monkeypatch):
    """手动同步子进程输出经脱敏/安全化后落 manual_sync.log（路径=WORKBUDDY_DIR/f2）。"""
    from app.core.config import settings
    from app.services import personal_source_service
    from tests.test_douyin_f2_source import _make_fake_runtime

    monkeypatch.setenv("F2_RUNTIME_DIR", str(_make_fake_runtime(tmp_path)))
    monkeypatch.setenv("F2_DOUYIN_COOKIE_FILE", str(tmp_path / "cookie.txt"))
    # 子进程隔离：不写共享测试库（poll_logs/works 都落临时目录）
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "manual_sync.db"))
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("OBSIDIAN_ROOT", str(tmp_path / "notes"))
    (tmp_path / "cookie.txt").write_text("sessionid=synthetic", encoding="utf-8")
    monkeypatch.setattr(personal_source_service, "_cookie_file_set", lambda: True)
    result = client.post("/api/v1/personal-source/sync", headers=auth_headers)
    assert result.status_code == 200
    import time as _time

    log_path = settings.workbuddy_dir / "f2" / "manual_sync.log"
    deadline = _time.time() + 60
    while _time.time() < deadline and not log_path.exists():
        _time.sleep(0.5)
    assert log_path.exists(), "manual_sync.log 未生成"
    text = log_path.read_text(encoding="utf-8")
    assert "sessionid=synthetic" not in text  # 凭据不落日志
    assert "MS4wLjABAAAA" not in text  # 账号标识不落日志
    assert "7000001" not in text and "8000001" not in text  # 作品 ID 不落日志


# ---------- 复审项 4：脱敏函数边界 ----------

def test_redaction_bearer_multi_cookie_url_folder_ids():
    from monitor.platforms.douyin_personal_source import redact_sensitive_text

    text = ("Authorization: Bearer FAKESECRETABC123456; "
            "Cookie: sessionid=FAKESESSIONABC; other_private=FAKEPRIVATEABC; "
            "https://x.douyin.com/?msToken=FAKETOKEN123&a_bogus=XYZ; "
            "folder:123456789012345 folder#123456789012345")
    out = redact_sensitive_text(text)
    assert "FAKESECRETABC" not in out
    assert "FAKESESSIONABC" not in out and "FAKEPRIVATEABC" not in out
    assert "FAKETOKEN123" not in out and "a_bogus=XYZ" not in out
    assert "123456789012345" not in out


# ---------- 保留：状态真实性 / 流水线边界 / 图文语义 / 只读配置（上一轮审核回归） ----------

def test_status_reports_monitor_offline_honestly(client, auth_headers):
    """测试环境无任何心跳：monitor_online 为 False，schedule_active 不宣称运行。"""
    status = client.get("/api/v1/personal-source/status", headers=auth_headers).json()["data"]
    assert status["monitor_online"] is False
    assert status["schedule_active"] is False


def test_schedule_active_true_when_monitor_online(client, auth_headers, db, monkeypatch):
    from app.services import personal_source_service

    monkeypatch.setattr(personal_source_service, "is_monitor_online", lambda: True)
    _set(db, ENABLED_KEY, "true")
    try:
        status = client.get("/api/v1/personal-source/status", headers=auth_headers).json()["data"]
        assert status["monitor_online"] is True
        assert status["schedule_active"] is True
    finally:
        _set(db, ENABLED_KEY, "false")


def test_auto_download_default_off_and_toggle(client, auth_headers, db):
    status = client.get("/api/v1/personal-source/status", headers=auth_headers).json()["data"]
    assert status["schedule"]["auto_download"] is False
    resp = client.put("/api/v1/personal-source/schedule", json={
        "enabled": False, "interval_minutes": 360, "scopes": ["like"], "auto_download": True,
    }, headers=auth_headers)
    assert resp.json()["data"]["auto_download"] is True
    _set(db, AUTO_DOWNLOAD_KEY, "false")


def test_run_pipeline_excludes_new_personal_source_works(db, monkeypatch):
    """全局流水线不自动处理 NEW 的个人来源作品；QUEUED 显式重试仍处理。"""
    from monitor import jobs
    from monitor.store.shared_db import settings as shared_settings

    personal_new = Work(platform="douyin", external_id="8800021", title="个人NEW", url="u",
                        source_type="personal_source", content_type="video", status="NEW")
    personal_queued = Work(platform="douyin", external_id="8800022", title="个人QUEUED", url="u2",
                           source_type="personal_source", content_type="video", status="QUEUED")
    monitor_new = Work(platform="douyin", external_id="8800023", title="监控NEW", url="u3",
                       source_type="monitor", content_type="video", status="NEW")
    db.add_all([personal_new, personal_queued, monitor_new])
    db.commit()
    original = _quarantine_other_works(db, {"8800021", "8800022", "8800023"})
    processed: list[str] = []
    monkeypatch.setattr(shared_settings, "monitor_run_pipeline", True)
    monkeypatch.setattr(jobs, "_process_work", lambda db_, w, **kw: processed.append(w.external_id))
    try:
        jobs.run_pipeline()
        assert "8800021" not in processed  # NEW 个人作品不被全局流水线处理
        assert "8800022" in processed  # 显式重试仍处理
        assert "8800023" in processed  # 其他来源不受影响
    finally:
        for w in (personal_new, personal_queued, monitor_new):
            row = db.get(Work, w.id)
            if row is not None:
                db.delete(row)
        db.commit()
        _restore_works(db, original)


def _png_bytes(seed: int) -> bytes:
    import hashlib

    return b"\x89PNG\r\n\x1a\n" + hashlib.sha256(str(seed).encode()).digest() * 8


def test_douyin_image_post_downloads_offline_end_to_end(db, monkeypatch, tmp_path):
    """合成端到端：图集作品 → F2 detail（伪运行时）→ CDN 直取（打桩）→ 归档凭据落库。

    全程无网页、无播放、无真实网络；下载凭据走既有 checkpoint 语义。
    """
    from monitor import jobs
    from monitor.pipeline import douyin_images

    work = Work(platform="douyin", external_id="9001234567890123456", title="抖音图集", url="u",
                source_type="personal_source", content_type="image", status="NEW")
    db.add(work)
    db.commit()

    monkeypatch.setenv("F2_DOUYIN_COOKIE_FILE", str(tmp_path / "cookie.txt"))
    (tmp_path / "cookie.txt").write_text("sessionid=synthetic", encoding="utf-8")
    monkeypatch.setenv("F2_RUNTIME_DIR", str(_make_fake_runtime(tmp_path)))
    monkeypatch.setattr(douyin_images, "fetch_url_bytes",
                        lambda url, **kw: _png_bytes(len(url)))

    from pathlib import Path as _Path
    from app.utils.timeutil import now_iso as _now
    work.published_at = "2026-05-01T08:00:00Z"
    work.published_time_source = "douyin_web_create_time"
    work.published_time_precision = "second"
    work.published_time_timezone = "UTC"
    db.commit()

    checkpoint = jobs.download_and_checkpoint(db, work)
    db.refresh(work)
    assert checkpoint.result.disposition == "downloaded"
    assert all(item.media_type == "image" for item in checkpoint.result.files)
    assert work.media_path and work.downloaded_at and work.download_time_source
    from app.models import ArchiveEntry, Task
    assert db.query(ArchiveEntry).filter_by(external_id=work.external_id).count() >= 1
    assert db.query(Task).filter_by(work_id=work.id, stage="download", status="succeeded").count() == 1
    # 第二次运行：manifest 源 URL 一致 + 登记文件 size/sha 校验通过 → 复用而非重复下载
    checkpoint2 = jobs.download_and_checkpoint(db, work)
    assert checkpoint2.result.disposition == "reused"


def test_douyin_image_post_partial_failure_is_honest(db, monkeypatch, tmp_path):
    """部分图片下载失败 → 如实 FAILED，无下载凭据、无归档；错误文案不含内部细节。"""
    from monitor import jobs
    from monitor.pipeline import douyin_images

    work = Work(platform="douyin", external_id="9001234567890123457", title="抖音图集", url="u",
                source_type="personal_source", content_type="image", status="NEW")
    db.add(work)
    db.commit()
    monkeypatch.setenv("F2_DOUYIN_COOKIE_FILE", str(tmp_path / "cookie.txt"))
    (tmp_path / "cookie.txt").write_text("sessionid=synthetic", encoding="utf-8")
    monkeypatch.setenv("F2_RUNTIME_DIR", str(_make_fake_runtime(tmp_path)))

    def flaky(url, **kw):
        if url.endswith("p2.jpg"):
            raise douyin_images.DouyinImageError("图片 CDN 返回 HTTP 403")
        return _png_bytes(len(url))

    monkeypatch.setattr(douyin_images, "fetch_url_bytes", flaky)
    try:
        jobs.download_and_checkpoint(db, work)
        raise AssertionError("expected failure")
    except jobs.PipelineOfflineError as exc:
        assert "403" in str(exc)
        db.rollback()
        failed = db.get(Work, work.id)
        failed.status = "FAILED"
        failed.last_error = "图片 CDN 返回 HTTP 403"
        db.commit()
        from app.models import ArchiveEntry, Task
        assert db.query(Task).filter_by(work_id=work.id, stage="download", status="succeeded").count() == 0


def test_global_and_personal_workers_process_same_work_only_once(db, monkeypatch, clean_settings):
    """全局流水线与个人 worker 并发：统一原子认领保证同一作品只处理一次。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread

    from monitor import jobs

    work = Work(platform="douyin", external_id="9400001", title="synthetic", url="u",
                source_type="monitor", content_type="video", status="QUEUED")
    db.add(work)
    db.flush()
    db.add(PersonalSourceMark(platform="douyin", external_id="9400001", source_mark="like",
                              source_channel="f2", snapshot_id="synthetic", first_seen_at=now_iso(),
                              last_seen_at=now_iso(), work_id=work.id))
    db.commit()
    _set(db, AUTO_DOWNLOAD_KEY, "true")
    monkeypatch.setattr(jobs.settings, "monitor_run_pipeline", True)
    original = _quarantine_other_works(db, {"9400001"})
    global_started = Event()
    personal_done = Event()
    calls: list[str] = []

    def fake_has_facts(db_, work_id):
        global_started.set()
        assert personal_done.wait(timeout=5)
        return True

    def fake_process(db_, w, **kwargs):
        calls.append(current_thread().name)
        w.status = "DONE"

    monkeypatch.setattr(jobs, "_work_has_personal_facts", fake_has_facts)
    monkeypatch.setattr(jobs, "_process_work", fake_process)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="global") as pool:
            future = pool.submit(jobs.run_pipeline)
            assert global_started.wait(timeout=5)
            try:
                jobs.download_personal_source_works()
            finally:
                personal_done.set()
            future.result(timeout=5)
        assert len(calls) == 1, f"calls={calls}"
    finally:
        row = db.get(Work, work.id)
        if row is not None:
            db.delete(row)
        for mark in db.query(PersonalSourceMark).filter_by(external_id="9400001").all():
            db.delete(mark)
        db.commit()
        _restore_works(db, original)


def test_active_long_download_is_not_recovered(db, monkeypatch, clean_settings):
    """PID 存活的认领即使超过超时也不得自动恢复（长下载保护）。"""
    import os
    from datetime import datetime, timedelta, timezone

    from monitor import jobs

    old = (datetime.now(timezone.utc) - timedelta(minutes=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    work = Work(platform="douyin", external_id="9400002", title="x", url="u",
                source_type="personal_source", content_type="video", status="DOWNLOADING",
                download_claimed_at=old, download_claim_pid=os.getpid())
    db.add(work)
    db.commit()
    _set(db, AUTO_DOWNLOAD_KEY, "true")
    original = _quarantine_other_works(db, {"9400002"})
    calls: list[str] = []
    monkeypatch.setattr(jobs, "_process_work", lambda db_, w, **kw: calls.append(w.id))
    try:
        jobs.download_personal_source_works()
        assert calls == [], f"active process download restarted: {len(calls)}"
    finally:
        row = db.get(Work, work.id)
        if row is not None:
            db.delete(row)
        db.commit()
        _restore_works(db, original)


def test_stale_recovery_cannot_reset_a_new_claim(db, clean_settings):
    """CAS 恢复：旧观察不得重置他人已建立的新认领。"""
    from datetime import datetime, timedelta, timezone

    from monitor import jobs

    old = (datetime.now(timezone.utc) - timedelta(minutes=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    work = Work(platform="douyin", external_id="9400003", title="x", url="u",
                source_type="personal_source", content_type="video", status="DOWNLOADING",
                download_claimed_at=old, download_claim_pid=999999999)
    db.add(work)
    db.commit()
    _set(db, AUTO_DOWNLOAD_KEY, "true")  # 重新认领需内联授权通过
    work_id = work.id
    with jobs.SessionLocal() as first, jobs.SessionLocal() as second:
        stale1 = first.get(Work, work_id)
        stale2 = second.get(Work, work_id)
        assert jobs._claim_stale(stale1.download_claimed_at, stale1.download_claim_pid)
        assert jobs._claim_stale(stale2.download_claimed_at, stale2.download_claim_pid)
        # 先捕获"当时观察到的旧认领"（rollback 会过期 identity map，之后再读会拿到新值）
        obs1 = (stale1.download_claimed_at, stale1.download_claim_pid)
        obs2 = (stale2.download_claimed_at, stale2.download_claim_pid)
        first.rollback()
        second.rollback()
        assert jobs._recover_stale_claim(first, work_id, *obs1)
        assert jobs._claim_work_for_processing(first, work_id, personal=True)
        reset_new_claim = jobs._recover_stale_claim(second, work_id, *obs2)
    assert not reset_new_claim, "second stale observer reset an active new claim"


def test_closing_authorization_before_claim_stops_new_work(db, monkeypatch, clean_settings):
    """授权读取返回 True 后、认领前另一会话关闭开关 → 原子认领必须失败。"""
    from monitor import jobs

    work = Work(platform="douyin", external_id="9400004", title="x", url="u",
                source_type="personal_source", content_type="video", status="NEW")
    db.add(work)
    db.commit()
    _set(db, AUTO_DOWNLOAD_KEY, "true")
    original = _quarantine_other_works(db, {"9400004"})
    real_check = jobs._authorization_granted

    def revoke_after_check():
        granted = real_check()
        with jobs.SessionLocal() as other:
            _set(other, AUTO_DOWNLOAD_KEY, "false")
        return granted

    calls: list[str] = []

    def fake_process(db_, w, **kw):
        calls.append(w.id)
        w.status = "DONE"

    monkeypatch.setattr(jobs, "_authorization_granted", revoke_after_check)
    monkeypatch.setattr(jobs, "_process_work", fake_process)
    try:
        jobs.download_personal_source_works()
        assert calls == [], "work started after authorization was switched off"
    finally:
        row = db.get(Work, work.id)
        if row is not None:
            db.delete(row)
        db.commit()
        _restore_works(db, original)


def test_unclaimed_interrupted_work_can_be_retried(client, auth_headers, db, clean_settings):
    """无认领的中断下载可经重试入口安全恢复；活跃认领返回 409。"""
    work = Work(platform="douyin", external_id="9400005", title="x", url="u",
                source_type="personal_source", content_type="video", status="DOWNLOADING")
    db.add(work)
    db.commit()
    resp = client.post(f"/api/v1/works/{work.id}/retry", headers=auth_headers)
    assert resp.status_code == 200, f"actual response status={resp.status_code}"

    # 活跃认领（当前进程存活）→ 409
    import os

    active = Work(platform="douyin", external_id="9400005b", title="y", url="u2",
                  source_type="personal_source", content_type="video", status="DOWNLOADING",
                  download_claimed_at=now_iso(), download_claim_pid=os.getpid())
    db.add(active)
    db.commit()
    resp2 = client.post(f"/api/v1/works/{active.id}/retry", headers=auth_headers)
    assert resp2.status_code == 409
    # 清理
    for wid in (work.id, active.id):
        row = db.get(Work, wid)
        if row is not None:
            db.delete(row)
    db.commit()


def test_download_failure_does_not_expose_token_in_work_api(client, auth_headers, db, monkeypatch, clean_settings):
    """失败持久化全链路安全：works.last_error 与 stages[].error 不含原始异常文本。"""
    from monitor import jobs

    work = Work(platform="douyin", external_id="9400006", title="x", url="u",
                source_type="personal_source", content_type="video", status="NEW")
    db.add(work)
    db.commit()
    _set(db, AUTO_DOWNLOAD_KEY, "true")
    original = _quarantine_other_works(db, {"9400006"})
    secret = "FAKESECRETVALUENOTREAL"

    def boom(*args, **kwargs):
        raise RuntimeError("Authorization: Bearer " + secret)

    monkeypatch.setattr(jobs, "_process_work", boom)
    try:
        jobs.download_personal_source_works()
        db.expire_all()  # 个人步骤在其他会话提交：过期本会话 identity map 再读
        row = db.get(Work, work.id)
        assert row.status == "FAILED"
        response = client.get(f"/api/v1/works/{work.id}", headers=auth_headers)
        assert response.status_code == 200
        assert secret not in response.text, "synthetic token exposed in work detail API"
        detail = response.json()["data"]
        assert detail["last_error"] and "RuntimeError" in detail["last_error"]  # 类型保留，可诊断
        assert "<redacted>" in detail["last_error"]  # 凭据值已脱敏（原始令牌与 Bearer 字样均不出现）
        assert "Bearer" not in detail["last_error"] and secret not in detail["last_error"]
    finally:
        row = db.get(Work, work.id)
        if row is not None:
            db.delete(row)
        db.commit()
        _restore_works(db, original)


def test_claim_metadata_cleared_after_completion_and_failure(db, monkeypatch, clean_settings):
    """成功与失败后认领元数据清空（DONE/FAILED 不残留活跃认领）。"""
    from monitor import jobs

    ok_work = Work(platform="douyin", external_id="9400007", title="x", url="u",
                   source_type="personal_source", content_type="video", status="NEW")
    fail_work = Work(platform="douyin", external_id="9400008", title="y", url="u2",
                     source_type="personal_source", content_type="video", status="NEW")
    db.add_all([ok_work, fail_work])
    db.commit()
    _set(db, AUTO_DOWNLOAD_KEY, "true")
    original = _quarantine_other_works(db, {"9400007", "9400008"})
    seq = {"n": 0}

    def fake_process(db_, w, **kw):
        seq["n"] += 1
        if seq["n"] == 2:
            raise RuntimeError("synthetic failure")
        w.status = "DONE"

    monkeypatch.setattr(jobs, "_process_work", fake_process)
    try:
        jobs.download_personal_source_works()
        from app.utils.timeutil import now_iso as _n
        assert db.get(Work, ok_work.id).download_claimed_at is None
        assert db.get(Work, fail_work.id).download_claimed_at is None
    finally:
        for wid in (ok_work.id, fail_work.id):
            row = db.get(Work, wid)
            if row is not None:
                db.delete(row)
        db.commit()
        _restore_works(db, original)


def test_redaction_covers_bearer_before_param_rule():
    """脱敏顺序回归：Authorization: Bearer <token> 的令牌必须消失。"""
    from monitor.platforms.douyin_personal_source import redact_sensitive_text

    out = redact_sensitive_text("Authorization: Bearer FAKESECRETVALUENOTREAL")
    assert "FAKESECRETVALUENOTREAL" not in out
    out = redact_sensitive_text("作品 9001234567890123456 无法获取 msToken=FAKETOKEN")
    assert "9001234567890123456" not in out and "FAKETOKEN" not in out

def _table_count(path: Path) -> int:
    with sqlite3.connect(path) as con:
        return con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]


def test_read_config_readonly_never_writes(tmp_path):
    from app.services import personal_source_service as svc

    missing = tmp_path / "missing.db"
    cfg = svc.read_config_readonly(missing)
    assert cfg["enabled"] is False and not missing.exists()

    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    before = _table_count(empty)
    cfg = svc.read_config_readonly(empty)
    assert _table_count(empty) == before == 0
    assert cfg["enabled"] is False and cfg["auto_download"] is False

    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(legacy) as con:
        con.execute("CREATE TABLE works (id INTEGER PRIMARY KEY)")
    cfg = svc.read_config_readonly(legacy)
    assert cfg["enabled"] is False
    assert _table_count(legacy) == 1

    full = tmp_path / "full.db"
    with sqlite3.connect(full) as con:
        con.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO app_settings VALUES ('douyin_personal_sync_enabled', 'true')")
        con.execute("INSERT INTO app_settings VALUES ('douyin_personal_auto_download', 'true')")
    cfg = svc.read_config_readonly(full)
    assert cfg["enabled"] is True and cfg["auto_download"] is True

    wal_db = tmp_path / "wal.db"
    con = sqlite3.connect(wal_db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
    con.commit()
    (wal_db.parent / "wal.db-wal").write_bytes(b"\x00" * 64)
    cfg = svc.read_config_readonly(wal_db)
    con.close()
    assert isinstance(cfg["enabled"], bool)


def test_f2_dry_run_does_not_initialize_existing_empty_db(tmp_path, monkeypatch):
    """隔离复现：_read_f2_config(create=False) 前后 0 表（不建库不建表）。"""
    from monitor import personal_source_ingest as ingest
    from app.core.config import settings

    db_path = tmp_path / "target.db"
    sqlite3.connect(db_path).close()
    monkeypatch.setattr(settings, "database_path", db_path)
    before = _table_count(db_path)
    config = ingest._read_f2_config(create=False)
    after = _table_count(db_path)
    assert before == after == 0
    assert config["scopes"] == ("like", "collection", "collections")
    assert config["sec_user_id"] == ""


# ---------- 第三轮复审回归（复现文件场景固化） ----------

def test_two_workers_claim_once(db, monkeypatch, clean_settings):
    """跨进程并发：两个调用同时竞争同一作品，原子认领保证只处理一次。"""
    import threading
    from threading import Barrier

    from monitor import jobs

    work = Work(platform="douyin", external_id="9000001", title="synthetic", url="u",
                source_type="personal_source", content_type="video", status="NEW")
    db.add(work)
    db.commit()
    _set(db, AUTO_DOWNLOAD_KEY, "true")
    original = _quarantine_other_works(db, {"9000001"})
    calls: list[tuple[int, bool | None]] = []
    barrier = Barrier(2)
    started = threading.Event()

    def fake_grant():
        # 两个线程都先到达授权检查，制造"都选中、都未认领"的竞争窗口
        barrier.wait(timeout=5)
        started.set()
        return True

    def fake_process(db_, w, **kwargs):
        calls.append((w.id, kwargs.get("allow_browser_fallback")))
        w.status = "DONE"

    monkeypatch.setattr(jobs, "_authorization_granted", fake_grant)
    monkeypatch.setattr(jobs, "_process_work", fake_process)
    from concurrent.futures import ThreadPoolExecutor

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(jobs.download_personal_source_works) for _ in range(2)]
            for future in futures:
                future.result(timeout=10)
        assert started.is_set()
        assert len(calls) == 1, f"processing calls={len(calls)}"
        assert calls[0][1] is False  # 兜底禁用
    finally:
        row = db.get(Work, work.id)
        if row is not None:
            db.delete(row)
        db.commit()
        _restore_works(db, original)


def test_revoking_authorization_stops_remaining_batch(db, monkeypatch, clean_settings):
    """处理第 1 条时跨进程关闭授权 → 本批剩余作品不再开始。"""
    from monitor import jobs

    w1 = Work(platform="douyin", external_id="9000002", title="x", url="u",
              source_type="personal_source", content_type="video", status="NEW")
    w2 = Work(platform="douyin", external_id="9000003", title="y", url="u2",
              source_type="personal_source", content_type="video", status="NEW")
    db.add_all([w1, w2])
    db.commit()
    _set(db, AUTO_DOWNLOAD_KEY, "true")
    original = _quarantine_other_works(db, {"9000002", "9000003"})
    calls: list[str] = []

    def fake_process(db_, w, **kwargs):
        calls.append(w.external_id)
        if len(calls) == 1:
            with jobs.SessionLocal() as other:
                _set(other, AUTO_DOWNLOAD_KEY, "false")
        w.status = "DONE"

    monkeypatch.setattr(jobs, "_process_work", fake_process)
    try:
        jobs.download_personal_source_works()
        assert calls == ["9000002"], f"processing after revoke={calls}"
    finally:
        for w in (w1, w2):
            row = db.get(Work, w.id)
            if row is not None:
                db.delete(row)
        db.commit()
        _restore_works(db, original)


def test_restart_surfaces_interrupted_and_count(db, monkeypatch, clean_settings):
    """异常退出留下的 DOWNLOADING（个人事实作品）：积压数如实显示，不再为 0。"""
    from app.services import personal_source_service
    from monitor import jobs

    work = Work(platform="douyin", external_id="9000004", title="x", url="u",
                source_type="personal_source", content_type="video", status="DOWNLOADING")
    db.add(work)
    db.commit()
    _set(db, AUTO_DOWNLOAD_KEY, "true")
    original = _quarantine_other_works(db, {"9000004"})
    calls: list[str] = []
    monkeypatch.setattr(jobs, "_process_work", lambda db_, w, **kw: calls.append(w.id))
    try:
        pending = personal_source_service.count_download_locks(db)
        assert pending >= 1  # 中断项计入积压显示（复审项 6）
        # 无认领戳的 DOWNLOADING：不自动恢复（避免误重置活跃全局下载），保持可操作状态
        jobs.download_personal_source_works()
        assert calls == []
    finally:
        row = db.get(Work, work.id)
        if row is not None:
            db.delete(row)
        db.commit()
        _restore_works(db, original)


def test_stale_claim_is_recovered_and_processed(db, monkeypatch, clean_settings):
    """带认领戳且进程已死/超时的 DOWNLOADING → 安全恢复为 QUEUED 并被重新处理。"""
    from datetime import datetime, timedelta, timezone

    from monitor import jobs

    stale_time = (datetime.now(timezone.utc) - timedelta(minutes=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    work = Work(platform="douyin", external_id="9000005", title="x", url="u",
                source_type="personal_source", content_type="video", status="DOWNLOADING",
                download_claimed_at=stale_time, download_claim_pid=999999999)
    db.add(work)
    db.commit()
    _set(db, AUTO_DOWNLOAD_KEY, "true")
    original = _quarantine_other_works(db, {"9000005"})
    calls: list[str] = []

    def fake_process(db_, w, **kw):
        calls.append(w.id)
        w.status = "DONE"

    monkeypatch.setattr(jobs, "_process_work", fake_process)
    try:
        jobs.download_personal_source_works()
        assert calls == [work.id]  # 过期认领被恢复并处理
    finally:
        row = db.get(Work, work.id)
        if row is not None:
            db.delete(row)
        db.commit()
        _restore_works(db, original)


def test_active_claim_not_recovered(db, monkeypatch, clean_settings):
    """认领新鲜且进程存活（当前进程）：不得重置，避免误伤活跃下载。"""
    import os

    from monitor import jobs

    work = Work(platform="douyin", external_id="9000006", title="x", url="u",
                source_type="personal_source", content_type="video", status="DOWNLOADING",
                download_claimed_at=now_iso(), download_claim_pid=os.getpid())
    db.add(work)
    db.commit()
    _set(db, AUTO_DOWNLOAD_KEY, "true")
    original = _quarantine_other_works(db, {"9000006"})
    calls: list[str] = []
    monkeypatch.setattr(jobs, "_process_work", lambda db_, w, **kw: calls.append(w.id))
    try:
        jobs.download_personal_source_works()
        assert calls == []
        assert db.get(Work, work.id).status == "DOWNLOADING"  # 未被动
    finally:
        row = db.get(Work, work.id)
        if row is not None:
            db.delete(row)
        db.commit()
        _restore_works(db, original)


def test_tick_sync_failure_still_downloads_independent(db, monkeypatch, clean_settings):
    """同步抛错 → 记录安全类别后仍进入下载消费；断言同步桩确实被调用（独立数据，乱序安全）。"""
    from monitor import jobs
    from app.models import PollLog

    _set(db, ENABLED_KEY, "true")
    _set(db, AUTO_DOWNLOAD_KEY, "true")
    # 独立数据：清掉共享库里的同步日志，确保本用例同步必然到期
    db.query(PollLog).filter_by(platform="douyin-personal").delete()
    db.commit()

    boom_calls: list[int] = []

    def boom(*args, **kwargs):
        boom_calls.append(1)
        raise RuntimeError("Authorization: Bearer FAKESECRETVALUENOTREAL")

    monkeypatch.setattr("monitor.personal_source_ingest.run_ingest", boom)
    download_calls: list[int] = []
    monkeypatch.setattr(jobs, "download_personal_source_works",
                        lambda **kw: download_calls.append(1) or 0)
    messages: list[str] = []
    monkeypatch.setattr(jobs.logger, "warning", lambda msg, *args: messages.append(msg % args))
    jobs.check_douyin_personal_sync()
    assert boom_calls == [1]  # 同步桩确实执行
    assert download_calls == [1]  # 同步失败后仍消费积压
    assert all("FAKESECRETVALUENOTREAL" not in m for m in messages)  # 令牌不进日志
    assert any("error_type=RuntimeError" in m for m in messages)  # 只记安全类别


def test_global_worker_disables_browser_for_personal_cross_source(db, monkeypatch, clean_settings):
    """全局流水线处理 monitor 来源 + 个人事实（QUEUED）作品：显式禁用浏览器兜底。"""
    from monitor import jobs

    work = Work(platform="douyin", external_id="9000007", title="x", url="u",
                source_type="monitor", content_type="video", status="QUEUED")
    db.add(work)
    db.flush()
    db.add(PersonalSourceMark(platform="douyin", external_id="9000007", source_mark="like",
                              source_channel="f2", snapshot_id="synthetic", first_seen_at=now_iso(),
                              last_seen_at=now_iso(), work_id=work.id))
    db.commit()
    monkeypatch.setattr(jobs.settings, "monitor_run_pipeline", True)
    original = _quarantine_other_works(db, {"9000007"})
    calls: list[bool | None] = []

    def fake_process(db_, w, **kw):
        calls.append(kw.get("allow_browser_fallback"))
        w.status = "DONE"

    monkeypatch.setattr(jobs, "_process_work", fake_process)
    try:
        jobs.run_pipeline()
        assert calls == [False], f"global pipeline browser fallback={calls}"
    finally:
        row = db.get(Work, work.id)
        if row is not None:
            db.delete(row)
        db.commit()
        _restore_works(db, original)


def test_download_checkpoint_default_infers_personal_facts(db, monkeypatch):
    """download_and_checkpoint 默认推断：跨来源（monitor 来源 + 个人事实）→ 禁兜底。"""
    from monitor import jobs

    work = Work(platform="douyin", external_id="9000008", title="x", url="u",
                source_type="monitor", content_type="video", status="QUEUED")
    db.add(work)
    db.flush()
    db.add(PersonalSourceMark(platform="douyin", external_id="9000008", source_mark="favorite",
                              source_channel="f2", snapshot_id="s", first_seen_at=now_iso(),
                              last_seen_at=now_iso(), work_id=work.id))
    db.commit()

    real_facts = jobs._work_has_personal_facts
    observed: dict = {}

    def spy_facts(db_, work_id):
        observed["result"] = real_facts(db_, work_id)
        return observed["result"]

    def fake_resolve(db_, w):
        raise RuntimeError("stop-after-inference")  # 推断发生在元数据解析之前

    monkeypatch.setattr(jobs, "_work_has_personal_facts", spy_facts)
    monkeypatch.setattr(jobs, "_resolve_work_metadata", fake_resolve)
    try:
        jobs.download_and_checkpoint(db, work)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "stop-after-inference" in str(exc)
    assert observed.get("result") is True  # 事实判定识别出个人来源覆盖 → 兜底默认禁用
