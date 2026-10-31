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
    auto_download: bool | None = Field(
        default=None,
        description="个人来源自动下载授权（默认 false，独立于全局流水线，只处理个人来源作品）",
    )


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
        auto_download=payload.auto_download,
    )
    return ok(config.to_dict())


@router.post("/sync", status_code=200)
def post_sync():
    """手动同步一次（立即返回，结果经 /status 查询）。"""
    return ok(personal_source_service.trigger_manual_sync())


class SyncLoopStart(BaseModel):
    max_rounds: int = Field(default=60, ge=1, le=200, description="最大轮数；每轮预算 900s 自动熔断")


@router.post("/sync-loop", status_code=200)
def post_sync_loop(payload: SyncLoopStart):
    """启动"连续同步到完整"循环：只读列表元数据，绝不下载。

    每轮复用预算机制；停止条件：用户停止 / 全部范围完整 / 连续 3 轮失败 / 达到轮数。
    """
    return ok(personal_source_service.start_sync_loop(payload.max_rounds))


@router.post("/sync-loop/stop", status_code=200)
def post_sync_loop_stop():
    """请求停止连续同步（当前轮结束后安全退出，不中断进行中的轮）。"""
    return ok(personal_source_service.request_sync_loop_stop())


@router.get("/sync-loop/status", status_code=200)
def get_sync_loop_status():
    """连续同步循环状态：运行中/轮次/最新一轮结果/作品总数。"""
    return ok(personal_source_service.sync_loop_status())
