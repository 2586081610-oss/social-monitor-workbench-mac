"""小红书单作品评论采集：任务、进度、完整结果、报告与评论图片。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from app.api.deps import require_admin
from app.core.errors import ok
from app.schemas.xhs_comments import XhsCommentJobCreate, XhsCommentJobRetry
from app.services.xhs_comment_service import xhs_comment_service

router = APIRouter(
    prefix="/api/v1/xhs-comments",
    tags=["xhs-comments"],
    dependencies=[Depends(require_admin)],
)


@router.post("/jobs", status_code=202)
def create_comment_job(payload: XhsCommentJobCreate):
    """只采集这一条作品的评论与回复；同一作品已有进行中任务时返回该任务。"""
    job = xhs_comment_service.create_job(
        url_input=payload.url,
        include_images=payload.include_images,
        include_voice=payload.include_voice,
        voice_authorized=payload.voice_authorized,
        voice_allow_open_page=payload.voice_allow_open_page,
    )
    return ok(job)


@router.get("/jobs/{job_id}", status_code=200)
def get_comment_job(job_id: str):
    return ok(xhs_comment_service.get_job(job_id))


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry_comment_job(job_id: str, payload: XhsCommentJobRetry | None = None):
    """按原作品与选项重新采集；结果按评论 ID 合并，不重复增加评论。

    原任务包含语音提取时，需要重新授权这一次打开作品页的操作。
    """
    return ok(
        xhs_comment_service.retry_job(
            job_id,
            voice_authorized=bool(payload and payload.voice_authorized),
            voice_allow_open_page=payload.voice_allow_open_page if payload else None,
        )
    )


@router.get("/notes", status_code=200)
def list_comment_notes(limit: int = Query(default=50, ge=1, le=200)):
    """已保存的评论结果列表（刷新后仍可重新打开）。"""
    return ok({"items": xhs_comment_service.list_notes(limit=limit)})


@router.get("/notes/{note_id}", status_code=200)
def get_comment_result(note_id: str):
    return ok(xhs_comment_service.get_result(note_id))


@router.get("/notes/{note_id}/report.md", status_code=200)
def get_comment_report(note_id: str):
    path = xhs_comment_service.report_path(note_id)
    return FileResponse(
        path,
        media_type="text/markdown; charset=utf-8",
        filename=f"xhs_comments_{note_id}.md",
        content_disposition_type="inline",
    )


@router.get("/notes/{note_id}/images/{filename}", status_code=200)
def get_comment_image(note_id: str, filename: str):
    path, media_type = xhs_comment_service.image_file(note_id, filename)
    return FileResponse(path, media_type=media_type, filename=filename, content_disposition_type="inline")
