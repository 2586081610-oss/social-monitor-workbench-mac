"""personal-source 路由：来源状态 / 定时配置（默认关）/ 手动同步。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.core.errors import ok
from app.services import personal_source_service

router = APIRouter(
    prefix="/api/v1/personal-source", tags=["personal-source"], dependencies=[Depends(require_admin)]
)


class ScheduleConfig(BaseModel):
    enabled: bool = Field(description="定时同步开关；默认 false，不会隐式启动")
    interval_minutes: int = Field(ge=5, le=60 * 24 * 7, description="同步间隔（分钟）")
    scopes: list[str] = Field(description="要读取的列表：like/collection/collections")
    sec_user_id: str | None = Field(default=None, description="本人 sec_user_id（点赞列表需要）")


@router.get("/status", status_code=200)
def get_status(db: Session = Depends(get_db)):
    """来源状态：配置、运行时/登录态可用性、最近一次同步结果（含失败原因）。"""
    return ok(personal_source_service.status(db))


@router.get("/schedule", status_code=200)
def get_schedule(db: Session = Depends(get_db)):
    return ok(personal_source_service.read_config(db).to_dict())


@router.put("/schedule", status_code=200)
def put_schedule(payload: ScheduleConfig, db: Session = Depends(get_db)):
    """保存定时配置。默认关闭；启用后由监控层按间隔执行（不隐式启动）。"""
    config = personal_source_service.update_schedule(
        db,
        enabled=payload.enabled,
        interval_minutes=payload.interval_minutes,
        scopes=payload.scopes,
        sec_user_id=payload.sec_user_id,
    )
    return ok(config.to_dict())


@router.post("/sync", status_code=200)
def post_sync():
    """手动同步一次（立即返回，结果经 /status 查询）。"""
    return ok(personal_source_service.trigger_manual_sync())
