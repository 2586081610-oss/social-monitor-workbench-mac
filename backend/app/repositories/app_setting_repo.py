"""app_settings 数据访问（get/set，值均为字符串，调用方负责类型转换）。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AppSetting
from app.utils.timeutil import now_iso


def get_value(db: Session, key: str, default: str | None = None) -> str | None:
    row = db.get(AppSetting, key)
    return row.value if row is not None else default


def set_value(db: Session, key: str, value: str | None) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value, created_at=now_iso(), updated_at=now_iso()))
    else:
        row.value = value
        row.updated_at = now_iso()
    db.flush()
