"""小红书评论采集接口的输入契约。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class XhsCommentJobCreate(BaseModel):
    """采集单条小红书作品的评论。"""

    url: str = Field(min_length=1, max_length=2000)
    include_images: bool = True
    include_voice: bool = False
    voice_authorized: bool = Field(
        default=False,
        description="语音转写需要打开作品页并使用已登录会话，必须由用户明确授权本次操作",
    )
    voice_allow_open_page: bool = Field(
        default=False,
        description="未找到已打开的作品页时，是否允许在该浏览器中新开一个标签页；必须同时授权语音提取",
    )


class XhsCommentJobRetry(BaseModel):
    """重新采集；若原任务包含语音提取，需要重新授权这一次操作。"""

    voice_authorized: bool = False
    voice_allow_open_page: bool | None = None
