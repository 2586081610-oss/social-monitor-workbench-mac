"""settings 路由：下载清晰度（B 站/抖音）读取与修改。

设置存 app_settings（与个人来源定时配置同一存储机制）；档位枚举与校验
在 download_quality_service。B 站登录态只报告存在性，不回显 Cookie。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.core.errors import ok
from app.services import download_quality_service

router = APIRouter(prefix="/api/v1/settings", tags=["settings"], dependencies=[Depends(require_admin)])


class DownloadQualityPayload(BaseModel):
    bilibili: str | None = Field(default=None, description="B 站清晰度代码：16/32/64/80")
    douyin: str | None = Field(default=None, description="抖音清晰度：480p/720p/1080p")


def _payload(state: dict) -> dict:
    data = {
        "bilibili": state["bilibili"],
        "douyin": state["douyin"],
        "bilibili_explicit": state["bilibili_explicit"],
        "douyin_explicit": state["douyin_explicit"],
    }
    data.update(download_quality_service.quality_options_payload())
    data["bilibili_login_available"] = download_quality_service.bilibili_login_available()
    return data


@router.get("/download-quality", status_code=200)
def get_download_quality(db: Session = Depends(get_db)):
    """当前下载清晰度 + 可选档位 + B 站登录态存在性。"""
    return ok(_payload(download_quality_service.read_quality(db)))


@router.put("/download-quality", status_code=200)
def put_download_quality(payload: DownloadQualityPayload, db: Session = Depends(get_db)):
    """保存清晰度（非法档位 422 拒绝）；仅对之后的下载生效，不重下已有文件。"""
    state = download_quality_service.update_quality(db, bilibili=payload.bilibili, douyin=payload.douyin)
    return ok(_payload(state))
