"""手动链接分析 API 的输入输出契约。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.work import Work


LinkPlatform = Literal["douyin", "bilibili", "xhs", "twitter"]
LinkAnalysisMode = Literal["one_time", "monitor"]


class LinkAnalysisCreate(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    platform: LinkPlatform
    external_id: str = Field(min_length=1, max_length=200)
    mode: LinkAnalysisMode = "one_time"


class LinkAnalysisResult(BaseModel):
    work: Work
    created: bool
    mode: LinkAnalysisMode


class LinkAnalysisIdentifyRequest(BaseModel):
    """仅识别：解析平台与作品 ID，不产生任何副作用。"""

    url: str = Field(min_length=1, max_length=2000)


class LinkAnalysisIdentifyResult(BaseModel):
    platform: LinkPlatform
    external_id: str


class LinkAnalysisDownloadRequest(BaseModel):
    """单链接最低可用下载：只识别、只下载这一条作品。"""

    url: str = Field(min_length=1, max_length=2000)


class LinkAnalysisDownloadResult(BaseModel):
    work: Work
    created: bool
    platform: LinkPlatform
    external_id: str
    content_type: str
    processing: bool
    message: str


class NoteCompletionRequest(BaseModel):
    note_path: str = Field(min_length=1, max_length=500)
