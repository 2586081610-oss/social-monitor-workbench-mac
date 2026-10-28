"""监控层定时同步开关（check_douyin_personal_sync）：默认关闭、到期才执行。

配置在 app_settings；用例之间必须清理，避免影响其他文件。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

from app.repositories import app_setting_repo  # noqa: E402
from app.models import PollLog  # noqa: E402
from app.utils.timeutil import now_iso  # noqa: E402

ENABLED_KEY = "douyin_personal_sync_enabled"
INTERVAL_KEY = "douyin_personal_sync_interval_minutes"


@pytest.fixture()
def clean_settings(db):
    yield
    for key in (ENABLED_KEY, INTERVAL_KEY):
        row = db.get(app_setting_repo.AppSetting, key)
        if row is not None:
            db.delete(row)
    for log in db.query(PollLog).filter_by(platform="douyin-personal").all():
        db.delete(log)
    db.commit()


def _set_schedule(db, *, enabled: bool, interval_minutes: int = 360):
    app_setting_repo.set_value(db, ENABLED_KEY, "true" if enabled else "false")
    app_setting_repo.set_value(db, INTERVAL_KEY, str(interval_minutes))
    db.commit()


def test_disabled_by_default_never_runs(db, monkeypatch, clean_settings):
    """默认（无配置行）不启动任何同步——不会隐式读取本人数据。"""
    from monitor import jobs

    calls = []
    monkeypatch.setattr(
        "monitor.personal_source_ingest.run_ingest",
        lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("不应调用")),
    )
    jobs.check_douyin_personal_sync()
    assert calls == []


def test_enabled_but_recent_run_skips(db, monkeypatch, clean_settings):
    from monitor import jobs
    from app.models import PollLog

    _set_schedule(db, enabled=True, interval_minutes=360)
    db.add(PollLog(platform="douyin-personal", started_at=now_iso(), finished_at=now_iso(), status="ok"))
    db.commit()

    calls = []
    monkeypatch.setattr(
        "monitor.personal_source_ingest.run_ingest",
        lambda *a, **k: calls.append(a),
    )
    jobs.check_douyin_personal_sync()
    assert calls == []  # 距上次同步不足间隔


def test_enabled_and_due_runs_ingest(db, monkeypatch, clean_settings):
    from monitor import jobs
    from app.models import PollLog

    _set_schedule(db, enabled=True, interval_minutes=360)
    stale = (datetime.now(timezone.utc) - timedelta(minutes=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.add(PollLog(platform="douyin-personal", started_at=stale, finished_at=stale, status="ok"))
    db.commit()

    calls = []
    monkeypatch.setattr(
        "monitor.personal_source_ingest.run_ingest",
        lambda *a, **k: calls.append(a) or _fake_report(),
    )
    jobs.check_douyin_personal_sync()
    assert calls == [("f2",)]  # 显式 F2 通道


def _fake_report():
    from monitor.personal_source_ingest import IngestReport

    return IngestReport(source="f2")


def test_sync_failure_does_not_raise(db, monkeypatch, clean_settings):
    from monitor import jobs

    _set_schedule(db, enabled=True)
    # 无 poll_logs → 视为到期；run_ingest 抛错必须被吞掉（调度线程存活）
    def boom(*args, **kwargs):
        raise RuntimeError("platform down")

    monkeypatch.setattr("monitor.personal_source_ingest.run_ingest", boom)
    jobs.check_douyin_personal_sync()  # 不应抛出
