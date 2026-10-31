"""手动链接入口：契约创建 / 仅识别 / 单链接最低可用下载。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.core.errors import ok
from app.schemas.link_analysis import (
    LinkAnalysisCreate,
    LinkAnalysisDownloadRequest,
    LinkAnalysisIdentifyRequest,
)
from app.services.link_analysis_service import link_analysis_service

router = APIRouter(
    prefix="/api/v1/link-analyses",
    tags=["link-analyses"],
    dependencies=[Depends(require_admin)],
)


@router.post("", status_code=201)
def create_link_analysis(payload: LinkAnalysisCreate, db: Session = Depends(get_db)):
    return ok(link_analysis_service.ingest(db, payload).model_dump())


@router.post("/identify", status_code=200)
def identify_link(payload: LinkAnalysisIdentifyRequest):
    """仅识别：解析平台与作品 ID，不建作品、不下载。"""
    return ok(link_analysis_service.identify(payload.url).model_dump())


@router.post("/download", status_code=200)
def download_single_link(payload: LinkAnalysisDownloadRequest, db: Session = Depends(get_db)):
    """单链接最低可用下载：只识别并只下载这一条作品，不触碰全局队列。"""
    return ok(link_analysis_service.quick_download(db, payload.url).model_dump())
