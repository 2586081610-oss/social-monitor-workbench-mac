"""下载清晰度设置（app_settings 持久化）：B 站 yutto -q / 抖音 jiji video_quality。

默认值与改造前硬编码行为完全一致（B 站 16=360P、抖音 480p）；未设置或
非法值一律回退默认，脏值不流向下载器。B 站登录态只报告存在性，绝不回显
Cookie 内容。
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import validation
from app.repositories import app_setting_repo

BILIBILI_QUALITY_KEY = "download_quality_bilibili"
DOUYIN_QUALITY_KEY = "download_quality_douyin"

# yutto -q 清晰度代码（代码 → 中文标签）。默认 16=360P 与改造前写死的 -q 16 一致。
BILIBILI_QUALITY_OPTIONS: dict[str, str] = {
    "16": "360P（流畅）",
    "32": "480P（标清）",
    "64": "720P",
    "80": "1080P",
}
# jiji262 video_quality 档位。480p 是本项目实测的最低含音轨档（lowest 无音轨）。
DOUYIN_QUALITY_OPTIONS: dict[str, str] = {
    "480p": "480p",
    "720p": "720p",
    "1080p": "1080p",
}
DEFAULT_BILIBILI_QUALITY = "16"
DEFAULT_DOUYIN_QUALITY = "480p"

_CONFIG_KEYS = (BILIBILI_QUALITY_KEY, DOUYIN_QUALITY_KEY)


def _normalize_bilibili(raw: str | None) -> tuple[str, bool]:
    value = (raw or "").strip()
    return (value, True) if value in BILIBILI_QUALITY_OPTIONS else (DEFAULT_BILIBILI_QUALITY, False)


def _normalize_douyin(raw: str | None) -> tuple[str, bool]:
    value = (raw or "").strip().lower()
    return (value, True) if value in DOUYIN_QUALITY_OPTIONS else (DEFAULT_DOUYIN_QUALITY, False)


def quality_from_raw(raw: dict) -> dict:
    """纯函数：键值行 → 清晰度状态（非法/缺失回退默认且 explicit=False）。"""
    bilibili, bilibili_explicit = _normalize_bilibili(raw.get(BILIBILI_QUALITY_KEY))
    douyin, douyin_explicit = _normalize_douyin(raw.get(DOUYIN_QUALITY_KEY))
    return {
        "bilibili": bilibili,
        "douyin": douyin,
        "bilibili_explicit": bilibili_explicit,
        "douyin_explicit": douyin_explicit,
    }


def read_quality(db: Session) -> dict:
    raw = {key: app_setting_repo.get_value(db, key) for key in _CONFIG_KEYS}
    return quality_from_raw(raw)


def read_quality_readonly(db_path: Path) -> dict:
    """真只读读取（mode=ro）：库/表缺失或任何 SQLite 错误 → 默认值，绝不写入。

    供监控层下载路径使用；explicit 标记用于区分"显式配置"与"默认回退"，
    前者才会同步抖音 config.yml。
    """
    try:
        if not Path(db_path).exists():
            return quality_from_raw({})
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        try:
            tables = {
                row[0]
                for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "app_settings" not in tables:
                return quality_from_raw({})
            rows = {
                key: value
                for key, value in con.execute(
                    f"SELECT key, value FROM app_settings WHERE key IN ({','.join('?' * len(_CONFIG_KEYS))})",
                    _CONFIG_KEYS,
                ).fetchall()
            }
        finally:
            con.close()
        return quality_from_raw(rows)
    except (sqlite3.Error, OSError):
        return quality_from_raw({})


def update_quality(db: Session, *, bilibili: str | None = None, douyin: str | None = None) -> dict:
    """保存清晰度设置：非法档位拒绝（422），合法值持久化到 app_settings。"""
    if bilibili is not None:
        value = str(bilibili).strip()
        if value not in BILIBILI_QUALITY_OPTIONS:
            raise validation(f"bilibili 清晰度需为 {list(BILIBILI_QUALITY_OPTIONS)} 之一")
        app_setting_repo.set_value(db, BILIBILI_QUALITY_KEY, value)
    if douyin is not None:
        value = str(douyin).strip().lower()
        if value not in DOUYIN_QUALITY_OPTIONS:
            raise validation(f"douyin 清晰度需为 {list(DOUYIN_QUALITY_OPTIONS)} 之一")
        app_setting_repo.set_value(db, DOUYIN_QUALITY_KEY, value)
    db.commit()
    return read_quality(db)


def bilibili_login_available() -> bool:
    """B 站登录态存在性：platform_cookies.json 的 bilibili cookie 含 SESSDATA。

    只返回布尔（不回显内容）；720P/1080P 等高清档实际需要登录凭据。
    """
    configured = os.getenv("PLATFORM_COOKIES_FILE", "").strip()
    path = Path(configured).expanduser() if configured else settings.workspace_root / "config" / "platform_cookies.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    section = payload.get("bilibili") if isinstance(payload, dict) else None
    cookie = section.get("cookie", "") if isinstance(section, dict) else ""
    return isinstance(cookie, str) and "SESSDATA" in cookie


def quality_options_payload() -> dict:
    """前端下拉选项（value/label，与归档策略 platforms 同构）。"""
    return {
        "bilibili_options": [
            {"value": value, "label": label} for value, label in BILIBILI_QUALITY_OPTIONS.items()
        ],
        "douyin_options": [
            {"value": value, "label": label} for value, label in DOUYIN_QUALITY_OPTIONS.items()
        ],
    }
