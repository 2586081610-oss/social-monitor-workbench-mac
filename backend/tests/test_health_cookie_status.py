"""cookies 表真实状态值必须全部能通过 CookieHealth 校验并正常出参。

背景：monitor/jobs.py 采集结果会写入 status="FAIL"（douyin 轮询、xhs 异常），
而 CookieHealth.status 曾只允许 OK/WARN/NEEDS_RELOGIN/UNKNOWN，导致
GET /api/v1/health/cookie 对带 FAIL 行的库直接 500（账号健康面板报错）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

from app.models import CookieHealth as CookieHealthRow  # noqa: E402
from app.schemas.health import CookieHealth  # noqa: E402


REAL_STATUSES = ("OK", "WARN", "FAIL", "NEEDS_RELOGIN", "UNKNOWN")


def test_cookie_health_schema_accepts_every_real_status():
    for status in REAL_STATUSES:
        model = CookieHealth(
            platform="douyin",
            status=status,
            last_checked_at=None,
            expires_at=None,
            note=None,
        )
        assert model.status == status


def test_health_cookie_endpoint_returns_fail_row(client, auth_headers, db):
    """临时库写入 FAIL 行后，接口必须 200 且原样返回该状态（不再 500）。"""
    from app.repositories import health_repo

    existing = db.query(CookieHealthRow).filter_by(platform="douyin").one_or_none()
    snapshot = (existing.status, existing.note) if existing is not None else None
    health_repo.upsert_cookie(db, "douyin", "FAIL", note="MediaCrawler 采集失败")
    db.commit()
    try:
        resp = client.get("/api/v1/health/cookie", headers=auth_headers)
        assert resp.status_code == 200
        items = {item["platform"]: item for item in resp.json()["data"]["items"]}
        assert items["douyin"]["status"] == "FAIL"
        assert items["douyin"]["note"] == "MediaCrawler 采集失败"
    finally:
        if snapshot is None:
            row = db.query(CookieHealthRow).filter_by(platform="douyin").one_or_none()
            if row is not None:
                db.delete(row)
        else:
            health_repo.upsert_cookie(db, "douyin", snapshot[0], note=snapshot[1])
        db.commit()
