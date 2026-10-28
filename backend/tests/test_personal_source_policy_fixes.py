"""Codex 审核修复回归：开关作用范围、图文语义、跨来源兜底、只读配置。

覆盖审核报告 4/5/6/7 项：
- 状态如实反映监控在线与定时可执行性（监控未运行不宣称定时运行）；
- 个人来源自动下载为独立授权，只处理个人来源事实覆盖的作品；
- run_pipeline 不自动处理 NEW 的 personal_source 作品（QUEUED 显式重试仍处理）；
- 抖音图文如实报不支持，不路由到 Twitter 下载器、不产生归档；
- 浏览器兜底按下载任务来源禁用（跨来源去重的旧作品也禁用）；
- dry-run 配置读取真只读（空库/缺库/旧 schema/live WAL 都不写不建）。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

from app.models import ArchiveEntry, PersonalSourceMark, Work  # noqa: E402
from app.repositories import app_setting_repo  # noqa: E402
from app.utils.timeutil import now_iso  # noqa: E402

ENABLED_KEY = "douyin_personal_sync_enabled"
AUTO_DOWNLOAD_KEY = "douyin_personal_auto_download"


# ---------- 审核项 4：状态真实反映 ----------

def test_status_reports_monitor_offline_honestly(client, auth_headers):
    """测试环境无监控心跳：monitor_online 必须为 False，schedule_active 不宣称运行。"""
    status = client.get("/api/v1/personal-source/status", headers=auth_headers).json()["data"]
    assert status["monitor_online"] is False
    assert status["schedule_active"] is False


def test_schedule_active_true_when_enabled_and_online(client, auth_headers, db, monkeypatch):
    from app.services import personal_source_service

    monkeypatch.setattr(personal_source_service, "is_monitor_online", lambda: True)
    app_setting_repo.set_value(db, ENABLED_KEY, "true")
    db.commit()
    try:
        status = client.get("/api/v1/personal-source/status", headers=auth_headers).json()["data"]
        assert status["monitor_online"] is True
        assert status["schedule_active"] is True
    finally:
        row = db.get(app_setting_repo.AppSetting, ENABLED_KEY)
        if row is not None:
            row.value = "false"
            db.commit()


def test_auto_download_default_off_and_toggle(client, auth_headers, db):
    status = client.get("/api/v1/personal-source/status", headers=auth_headers).json()["data"]
    assert status["schedule"]["auto_download"] is False
    resp = client.put("/api/v1/personal-source/schedule", json={
        "enabled": False, "interval_minutes": 360, "scopes": ["like"], "auto_download": True,
    }, headers=auth_headers)
    assert resp.json()["data"]["auto_download"] is True
    # 清理
    row = db.get(app_setting_repo.AppSetting, AUTO_DOWNLOAD_KEY)
    if row is not None:
        row.value = "false"
        db.commit()


# ---------- 审核项 4：个人来源下载范围与全局流水线边界 ----------

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


def test_personal_download_step_only_processes_personal_facts(db, monkeypatch):
    from monitor import jobs

    personal_new = Work(platform="douyin", external_id="8800001", title="个人", url="u1",
                        source_type="personal_source", content_type="video", status="NEW")
    cross_source_new = Work(platform="douyin", external_id="8800002", title="跨来源", url="u2",
                            source_type="monitor", content_type="video", status="NEW")
    monitor_new = Work(platform="douyin", external_id="8800003", title="纯监控", url="u3",
                       source_type="monitor", content_type="video", status="NEW")
    personal_queued = Work(platform="douyin", external_id="8800004", title="个人已排队", url="u4",
                           source_type="personal_source", content_type="video", status="QUEUED")
    db.add_all([personal_new, cross_source_new, monitor_new, personal_queued])
    db.flush()
    db.add(PersonalSourceMark(platform="douyin", external_id="8800002", source_mark="like",
                              source_channel="f2", snapshot_id="s1",
                              first_seen_at=now_iso(), last_seen_at=now_iso(),
                              work_id=cross_source_new.id))
    db.commit()
    try:
        original = _quarantine_other_works(db, {"8800001", "8800002", "8800003", "8800004"})
        processed: list[str] = []
        monkeypatch.setattr(jobs, "_process_work", lambda db_, w, **kw: processed.append(w.external_id))
        jobs.download_personal_source_works()
        # 只处理：personal_source NEW + 有个人来源事实的跨来源 NEW；
        # 不处理：纯监控 NEW；不处理：QUEUED（显式重试归全局流水线）
        assert sorted(processed) == ["8800001", "8800002"]
    finally:
        for w in (personal_new, cross_source_new, monitor_new, personal_queued):
            row = db.get(Work, w.id)
            if row is not None:
                db.delete(row)
        for mark in db.query(PersonalSourceMark).filter_by(external_id="8800002").all():
            db.delete(mark)
        db.commit()
        _restore_works(db, original)


def test_personal_download_step_disables_browser_fallback(db, monkeypatch):
    """下载任务来源决定兜底：个人来源步骤一律禁用（含跨来源去重的旧作品）。"""
    from monitor import jobs

    work = Work(platform="douyin", external_id="8800011", title="跨来源兜底", url="u",
                source_type="monitor", content_type="video", status="NEW")
    db.add(work)
    db.flush()
    db.add(PersonalSourceMark(platform="douyin", external_id="8800011", source_mark="favorite",
                              source_channel="f2", snapshot_id="s1",
                              first_seen_at=now_iso(), last_seen_at=now_iso(), work_id=work.id))
    db.commit()
    seen: dict = {}

    def fake_process(db_, w, *, write_pending=True, allow_browser_fallback=None):
        seen["allow"] = allow_browser_fallback

    monkeypatch.setattr(jobs, "_process_work", fake_process)
    try:
        jobs.download_personal_source_works()
        assert seen["allow"] is False
    finally:
        row = db.get(Work, work.id)
        if row is not None:
            db.delete(row)
        for mark in db.query(PersonalSourceMark).filter_by(external_id="8800011").all():
            db.delete(mark)
        db.commit()


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


# ---------- 审核项 5：抖音图文如实不支持 ----------

def test_douyin_image_post_fails_honestly_end_to_end(db, monkeypatch, tmp_path):
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
        # 归档零产生
        assert db.query(ArchiveEntry).filter_by(external_id="8800031").count() == 0
    finally:
        row = db.get(Work, work.id)
        if row is not None:
            db.delete(row)
        db.commit()


# ---------- 审核项 7：dry-run 配置真只读 ----------

def _table_count(path: Path) -> int:
    with sqlite3.connect(path) as con:
        return con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]


def test_read_config_readonly_never_writes(tmp_path, monkeypatch):
    from app.services import personal_source_service as svc

    # 缺库：不创建文件
    missing = tmp_path / "missing.db"
    cfg = svc.read_config_readonly(missing)
    assert cfg["enabled"] is False and not missing.exists()

    # 空库（0 表）：读取前后都 0 表，不建表
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    before = _table_count(empty)
    cfg = svc.read_config_readonly(empty)
    assert _table_count(empty) == before == 0
    assert cfg["enabled"] is False and cfg["auto_download"] is False

    # 旧 schema（有其他表、无 app_settings）：默认值，不建表
    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(legacy) as con:
        con.execute("CREATE TABLE works (id INTEGER PRIMARY KEY)")
    cfg = svc.read_config_readonly(legacy)
    assert cfg["enabled"] is False
    assert _table_count(legacy) == 1

    # 正常库：读出已存配置
    full = tmp_path / "full.db"
    with sqlite3.connect(full) as con:
        con.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO app_settings VALUES ('douyin_personal_sync_enabled', 'true')")
        con.execute("INSERT INTO app_settings VALUES ('douyin_personal_auto_download', 'true')")
    cfg = svc.read_config_readonly(full)
    assert cfg["enabled"] is True and cfg["auto_download"] is True

    # live WAL：非空 WAL 存在时也只读不写（数据可能读不到 → 默认值，可接受）
    wal_db = tmp_path / "wal.db"
    con = sqlite3.connect(wal_db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
    con.commit()
    (wal_db.parent / "wal.db-wal").write_bytes(b"\x00" * 64)  # 伪造非空 WAL
    cfg = svc.read_config_readonly(wal_db)
    con.close()
    assert isinstance(cfg["enabled"], bool)


def test_f2_dry_run_does_not_initialize_existing_empty_db(tmp_path, monkeypatch):
    """隔离复现审核项 7：_read_f2_config(create=False) 前后 0 表。"""
    from monitor import personal_source_ingest as ingest
    from app.core.config import settings

    db_path = tmp_path / "target.db"
    sqlite3.connect(db_path).close()
    monkeypatch.setattr(settings, "database_path", db_path)
    before = _table_count(db_path)
    config = ingest._read_f2_config(create=False)
    after = _table_count(db_path)
    assert before == after == 0
    # 空库 → 默认配置（全部 scope），且绝不建表
    assert config["scopes"] == ("like", "collection", "collections")
    assert config["sec_user_id"] == ""
