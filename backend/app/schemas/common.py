"""公共类型：平台/作品状态等（对齐 openapi-aggregator.yaml schemas）。"""
from __future__ import annotations

from typing import Literal

Platform = Literal["douyin", "bilibili", "xhs", "twitter", "wechat"]
WorkStatus = Literal[
    "NEW",
    "QUEUED",
    "DOWNLOADING",
    "DOWNLOADED",
    "TRANSCRIBING",
    "FRAME_EXTRACTING",
    "NOTE_GENERATING",
    "DONE",
    "FAILED",
    "UNCLASSIFIED",
]
# personal_source = 本人来源同步（F2/DDR）登记的作品（monitor/jobs 消费同一种状态机）
SourceType = Literal["monitor", "like", "favorite", "manual", "personal_source"]
TagKind = Literal["big", "small"]
LexiconAction = Literal["increase", "decrease"]
# 真实写入方：account_service 在线校验（OK/WARN/NEEDS_RELOGIN/UNKNOWN）、
# monitor/jobs 采集结果（OK/FAIL，见 monitor/jobs.py _update_cookie）。
CookieStatus = Literal["OK", "WARN", "FAIL", "NEEDS_RELOGIN", "UNKNOWN"]
StageName = Literal["parse", "download", "transcribe", "frames", "note"]
StageStatus = Literal["pending", "running", "succeeded", "failed"]

WORK_STATUSES: tuple[str, ...] = (
    "NEW",
    "QUEUED",
    "DOWNLOADING",
    "DOWNLOADED",
    "TRANSCRIBING",
    "FRAME_EXTRACTING",
    "NOTE_GENERATING",
    "DONE",
    "FAILED",
    "UNCLASSIFIED",
)
