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


def test_douyin_image_post_fails_honestly_end_to_end(db, monkeypatch):
    """合成端到端：图文作品登记 → 下载 → 如实 FAILED，无归档、无浏览器兜底、无 Twitter 下载器。"""
    from monitor import jobs

    work = Work(platform="douyin", external_id="8800031", title="抖音图文", url="u",
                source_type="personal_source", content_type="image", status="NEW")
    db.add(work)
    db.commit()

    def no_twitter(*args, **kwargs):
        raise AssertionError("twitter image downloader must not be used for douyin image posts")

    def no_browser(*args, **kwargs):
        raise AssertionError("browser fallback must not be used")

    monkeypatch.setattr("monitor.pipeline.twitter_image.download_images_with_result", no_twitter)
    monkeypatch.setattr("monitor.pipeline.download._download_douyin_browser", no_browser, raising=False)
    try:
        jobs._process_work(db, work, write_pending=False, allow_browser_fallback=False)
        raise AssertionError("expected PipelineOfflineError")
    except jobs.PipelineOfflineError as exc:
        assert "douyin image" in str(exc)
        db.rollback()
        failed = db.get(Work, work.id)
        failed.status = "FAILED"
        failed.last_error = str(exc)
        db.commit()
        assert db.query(ArchiveEntry).filter_by(external_id="8800031").count() == 0
    finally:
        row = db.get(Work, work.id)
        if row is not None:
            db.delete(row)
        db.commit()


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
