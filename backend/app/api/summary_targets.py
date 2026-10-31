"""summary-targets 路由：AI 总结交付目标查询与默认工具设置（方案 A）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import require_admin
from app.core.errors import ok
from app.services import summary_target_service

router = APIRouter(
    prefix="/api/v1/summary-targets",
    tags=["summary-targets"],
    dependencies=[Depends(require_admin)],
)


class DefaultTargetUpdate(BaseModel):
    target: str = Field(min_length=1, max_length=60)


@router.get("")
def list_summary_targets():
    return ok(summary_target_service.list_targets())


@router.put("/default")
def set_default_summary_target(payload: DefaultTargetUpdate):
    data = summary_target_service.set_default_target(payload.target)
    return ok({"default_target": data["default_target"], "targets": data["targets"]})
