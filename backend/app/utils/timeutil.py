"""时间工具：统一 ISO-8601 UTC 字符串存储。"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

#: 项目展示口径时区（AGENTS.md：网页与 CSV 时间显示为北京时间）
LOCAL_TZ = "Asia/Shanghai"


def now_iso() -> str:
    """当前 UTC 时间，ISO-8601 格式。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def to_date(value: str | datetime | None, tz: str | None = None) -> str | None:
    """取日期部分 YYYY-MM-DD；无法解析返回 None。

    tz=None 保持原行为（按输入自带时区取日，UTC 存储即 UTC 日期）；
    传入时区名（如 "Asia/Shanghai"）时先转换到该时区再取日期。
    无 tzinfo 的时间按存储约定视为 UTC。
    """
    dt = value if isinstance(value, datetime) else parse_iso(value)
    if dt is None:
        return None
    if tz:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(ZoneInfo(tz))
    return dt.strftime("%Y-%m-%d")


def to_date_local(value: str | datetime | None, tz: str = LOCAL_TZ) -> str | None:
    """按本地时区（默认北京时间 Asia/Shanghai）取日期 YYYY-MM-DD。"""
    return to_date(value, tz=tz)
