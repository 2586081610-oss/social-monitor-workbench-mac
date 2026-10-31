"""health 路由：/api/v1/health/*。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.core.errors import ok
from app.services import account_service
from app.services.health_service import health_service

router = APIRouter(prefix="/api/v1/health", tags=["health"], dependencies=[Depends(require_admin)])


class CookieUpdate(BaseModel):
    cookie: str = Field(min_length=20, max_length=32768)


@router.get("/cookie", status_code=200)
def get_cookie_health(db: Session = Depends(get_db)):
    return ok({"items": [c.model_dump() for c in health_service.cookie_health(db)]})


@router.post("/cookie/check-all", status_code=200)
def check_all_cookie_health(db: Session = Depends(get_db)):
    """四平台登录态逐一在线校验（探测不到的如实返回 WARN/UNKNOWN，不编造）。"""
    return ok({"items": [c.model_dump() for c in account_service.check_all_platforms(db)]})


@router.post("/cookie/{platform}/check", status_code=200)
def check_cookie_health(platform: str, db: Session = Depends(get_db)):
    return ok(account_service.check_platform(db, platform).model_dump())


@router.put("/cookie/{platform}", status_code=200)
def switch_account_cookie(platform: str, payload: CookieUpdate, db: Session = Depends(get_db)):
    """切换账号：覆写该平台 Cookie 存储（douyin→f2/douyin_cookie.txt，其余→platform_cookies.json）并立即校验。"""
    return ok(account_service.switch_account(db, platform, payload.cookie).model_dump())


@router.get("/system", status_code=200)
def get_system_health():
    return ok({"items": [c.model_dump() for c in health_service.system_health()]})
