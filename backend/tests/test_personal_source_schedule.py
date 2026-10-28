"""个人来源定时配置（/api/v1/personal-source）：默认关闭、校验与状态展示。

关键约定：定时同步默认 enabled=false，不会隐式启动；登录态只报告存在性，
内容绝不回显。
"""
from __future__ import annotations

import sys
from pathlib import Path

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

from app.models import AppSetting, PollLog  # noqa: E402
from app.utils.timeutil import now_iso  # noqa: E402

SCHEDULE_URL = "/api/v1/personal-source/schedule"
STATUS_URL = "/api/v1/personal-source/status"


def test_default_schedule_is_disabled(client, auth_headers):
    resp = client.get(STATUS_URL, headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["schedule"]["enabled"] is False
    assert data["schedule"]["interval_minutes"] > 0
    assert data["schedule"]["scopes"]
    assert data["last_sync"] is None  # 尚未同步过
    # 登录态只允许存在性布尔，不允许出现内容
    assert data["cookie_file_set"] in (True, False)
    body_text = str(resp.json())
    assert "sessionid" not in body_text and "Cookie:" not in body_text


def test_put_schedule_persists_and_status_reflects(client, auth_headers, db):
    payload = {
        "enabled": True,
        "interval_minutes": 720,
        "scopes": ["like", "collection"],
        "sec_user_id": "MS4wLjABAAAA-test",
    }
    resp = client.put(SCHEDULE_URL, json=payload, headers=auth_headers)
    assert resp.status_code == 200
    saved = resp.json()["data"]
    assert saved["enabled"] is True and saved["interval_minutes"] == 720
    assert saved["scopes"] == ["like", "collection"]
    assert saved["sec_user_id"] == "MS4wLjABAAAA-test"

    status = client.get(STATUS_URL, headers=auth_headers).json()["data"]
    assert status["schedule"]["enabled"] is True
    assert status["sec_user_id_set"] is True
    assert status["channel"] == "f2"
    assert status["last_sync"] is None

    # 清理，避免影响其他用例
    row = db.get(AppSetting, "douyin_personal_sync_enabled")
    if row is not None:
        row.value = "false"
        db.commit()


def test_put_schedule_validation_rejects_bad_input(client, auth_headers):
    cases = [
        {"enabled": True, "interval_minutes": 1, "scopes": ["like"]},  # 间隔过小
        {"enabled": True, "interval_minutes": 999999, "scopes": ["like"]},
        {"enabled": True, "interval_minutes": 60, "scopes": ["like", "bogus"]},
        {"enabled": True, "interval_minutes": 60, "scopes": []},
    ]
    for payload in cases:
        resp = client.put(SCHEDULE_URL, json=payload, headers=auth_headers)
        assert resp.status_code == 422, f"{payload} 应被拒绝"


def test_status_surfaces_last_sync_from_poll_logs(client, auth_headers, db):
    db.add(PollLog(
        platform="douyin-personal",
        started_at=now_iso(),
        finished_at=now_iso(),
        status="fail",
        detail="login_expired: 登录已过期",
        new_works=0,
    ))
    db.commit()
    try:
        status = client.get(STATUS_URL, headers=auth_headers).json()["data"]
        assert status["last_sync"]["status"] == "fail"
        assert "login_expired" in (status["last_sync"]["detail"] or "")
    finally:
        log = db.query(PollLog).filter_by(platform="douyin-personal").first()
        if log is not None:
            db.delete(log)
            db.commit()


def test_manual_sync_requires_runtime_and_cookie(client, auth_headers, db):
    """运行时/登录态未就绪时，手动同步给出明确校验错误而不是静默失败。"""
    resp = client.post("/api/v1/personal-source/sync", headers=auth_headers)
    body = resp.json()
    # 测试环境两者皆缺：422 且信息包含缺失项
    assert resp.status_code == 422
    assert ("运行时" in body["message"]) or ("登录态" in body["message"])
