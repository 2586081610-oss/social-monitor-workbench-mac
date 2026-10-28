"""个人来源服务（F2 通道）：定时配置（默认关）、来源状态、手动同步、自动下载授权。

约定：
- 定时配置存 app_settings（前端可改，监控层每分钟读取生效），默认关闭，
  不会隐式启动；实际启用前由用户在页面明确设置频率与范围。
- 自动下载是**独立授权**（douyin_personal_auto_download，默认关）：只处理
  个人来源事实覆盖的作品，绝不开启或扩大全局下载队列。
- 登录态（cookie 文件路径）只走环境变量/本地文件，不入库、不回显。
- 手动同步与既有一次性爬取一致：拉起子进程，失败原因写日志与 poll_logs。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import validation
from app.repositories import app_setting_repo, health_repo
from app.services.health_service import is_monitor_online
from app.utils.timeutil import now_iso

SYNC_ENABLED_KEY = "douyin_personal_sync_enabled"
SYNC_INTERVAL_KEY = "douyin_personal_sync_interval_minutes"
SYNC_SCOPES_KEY = "douyin_personal_sync_scopes"
SEC_USER_ID_KEY = "douyin_personal_sec_user_id"
AUTO_DOWNLOAD_KEY = "douyin_personal_auto_download"

# 与 monitor/platforms/f2_fetch.py 的 scope 集合一致
SYNC_SCOPES = ("like", "collection", "collections")
DEFAULT_INTERVAL_MINUTES = 360
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 60 * 24 * 7

SYNC_POLL_LOG_PLATFORM = "douyin-personal"
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 定时配置的全部键（只读访问用）
_CONFIG_KEYS = (SYNC_ENABLED_KEY, SYNC_INTERVAL_KEY, SYNC_SCOPES_KEY, SEC_USER_ID_KEY, AUTO_DOWNLOAD_KEY)


@dataclass
class PersonalSourceConfig:
    enabled: bool
    interval_minutes: int
    scopes: list[str]
    sec_user_id: str
    auto_download: bool

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "interval_minutes": self.interval_minutes,
            "scopes": list(self.scopes),
            "sec_user_id": self.sec_user_id,
            "auto_download": self.auto_download,
        }


def _parse_bool(raw) -> bool:
    return (raw or "").strip().lower() == "true"


def _parse_interval(raw) -> int:
    try:
        value = int((raw or "").strip())
    except (ValueError, TypeError):
        return DEFAULT_INTERVAL_MINUTES
    if value < MIN_INTERVAL_MINUTES or value > MAX_INTERVAL_MINUTES:
        return DEFAULT_INTERVAL_MINUTES
    return value


def _parse_scopes(raw) -> list[str]:
    scopes = [s.strip() for s in (raw or "").split(",") if s.strip() in SYNC_SCOPES]
    return scopes or list(SYNC_SCOPES)


def config_from_raw(raw: dict) -> dict:
    """纯函数：键值行 → 配置字典（非法值回退默认）。只读路径与 ORM 路径共用。"""
    return {
        "enabled": _parse_bool(raw.get(SYNC_ENABLED_KEY)),
        "interval_minutes": _parse_interval(raw.get(SYNC_INTERVAL_KEY)),
        "scopes": _parse_scopes(raw.get(SYNC_SCOPES_KEY)),
        "sec_user_id": (raw.get(SEC_USER_ID_KEY) or "").strip(),
        "auto_download": _parse_bool(raw.get(AUTO_DOWNLOAD_KEY)),
    }


def read_config(db: Session) -> PersonalSourceConfig:
    raw = {key: app_setting_repo.get_value(db, key) for key in _CONFIG_KEYS}
    data = config_from_raw(raw)
    return PersonalSourceConfig(**data)


def read_config_readonly(db_path: Path) -> dict:
    """真只读读取配置：mode=ro 打开；库/表缺失或任何 SQLite 错误 → 默认值。

    用于 dry-run 等绝不触发建库/建表的路径；不产生任何写入。
    """
    try:
        if not Path(db_path).exists():
            return config_from_raw({})
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        try:
            tables = {
                row[0]
                for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "app_settings" not in tables:
                return config_from_raw({})
            rows = {
                key: value
                for key, value in con.execute(
                    f"SELECT key, value FROM app_settings WHERE key IN ({','.join('?' * len(_CONFIG_KEYS))})",
                    _CONFIG_KEYS,
                ).fetchall()
            }
        finally:
            con.close()
        return config_from_raw(rows)
    except (sqlite3.Error, OSError):
        return config_from_raw({})


def update_schedule(
    db: Session,
    *,
    enabled: bool,
    interval_minutes: int,
    scopes: list[str],
    sec_user_id: str | None = None,
    auto_download: bool | None = None,
) -> PersonalSourceConfig:
    if not isinstance(enabled, bool):
        raise validation("enabled 必须为布尔值")
    if not isinstance(interval_minutes, int) or not (
        MIN_INTERVAL_MINUTES <= interval_minutes <= MAX_INTERVAL_MINUTES
    ):
        raise validation(f"interval_minutes 需在 {MIN_INTERVAL_MINUTES}~{MAX_INTERVAL_MINUTES} 之间")
    unknown = [s for s in scopes if s not in SYNC_SCOPES]
    if unknown:
        raise validation(f"scopes 含未知项: {unknown}（支持 {list(SYNC_SCOPES)}）")
    if not scopes:
        raise validation("scopes 不能为空")
    app_setting_repo.set_value(db, SYNC_ENABLED_KEY, "true" if enabled else "false")
    app_setting_repo.set_value(db, SYNC_INTERVAL_KEY, str(interval_minutes))
    app_setting_repo.set_value(db, SYNC_SCOPES_KEY, ",".join(scopes))
    if sec_user_id is not None:
        app_setting_repo.set_value(db, SEC_USER_ID_KEY, sec_user_id.strip())
    if auto_download is not None:
        if not isinstance(auto_download, bool):
            raise validation("auto_download 必须为布尔值")
        app_setting_repo.set_value(db, AUTO_DOWNLOAD_KEY, "true" if auto_download else "false")
    db.commit()
    return read_config(db)


def _runtime_dir() -> Path:
    # 与 monitor/platforms/douyin_f2_source.resolve_runtime_dir 的解析保持一致
    raw = (os.getenv("F2_RUNTIME_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return settings.workspace_root / "tools" / "f2-runtime"


def _runtime_python_exists() -> bool:
    """F2 隔离运行时解释器是否存在（纯路径检查，不导入 monitor 包）。"""
    for relative in (".venv/bin/python", ".venv/Scripts/python.exe"):
        if (_runtime_dir() / relative).exists():
            return True
    return False


def _cookie_file_path() -> Path:
    # 与 monitor/platforms/douyin_f2_source.resolve_cookie_file 的解析保持一致
    raw = (os.getenv("F2_DOUYIN_COOKIE_FILE") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return settings.workspace_root / "config" / "f2" / "douyin_cookie.txt"


def _cookie_file_set() -> bool:
    try:
        return _cookie_file_path().is_file()
    except OSError:
        return False


def status(db: Session) -> dict:
    config = read_config(db)
    last = health_repo.latest_poll_logs(db).get(SYNC_POLL_LOG_PLATFORM)
    monitor_online = is_monitor_online()
    return {
        "channel": "f2",
        "schedule": config.to_dict(),
        # 定时是否真的会执行 = 开关开启 且 监控层在线（start_all.sh 默认不含监控层）
        "monitor_online": monitor_online,
        "schedule_active": config.enabled and monitor_online,
        "sec_user_id_set": bool(config.sec_user_id),
        "cookie_file_set": _cookie_file_set(),
        "runtime_available": _runtime_python_exists(),
        # 全局流水线是既有环境开关（MONITOR_RUN_PIPELINE），与个人来源自动下载相互独立
        "pipeline_enabled": settings.monitor_run_pipeline,
        "last_sync": (
            {
                "finished_at": last.finished_at,
                "status": last.status,
                "detail": last.detail,
                "new_works": last.new_works,
            }
            if last is not None
            else None
        ),
    }


def trigger_manual_sync() -> dict:
    """手动同步：拉起一次性 ingest 子进程（--source f2），立即返回 pid。"""
    if not _runtime_python_exists():
        raise validation("F2 隔离运行时未安装（tools/f2-runtime/.venv）")
    if not _cookie_file_set():
        raise validation("未配置抖音登录态文件（F2_DOUYIN_COOKIE_FILE 或 config/f2/douyin_cookie.txt）")
    workbuddy = settings.workbuddy_dir / "f2"
    workbuddy.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "monitor.personal_source_ingest", "--source", "f2"]
    with open(workbuddy / "manual_sync.log", "ab") as log_fh:
        proc = subprocess.Popen(
            cmd,
            cwd=str(settings.workspace_root),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
    return {"triggered": True, "pid": proc.pid, "triggered_at": now_iso()}


def last_sync_started_at(db: Session) -> str | None:
    """最近一次同步（含失败）的开始时间，供监控层判断间隔是否到期。"""
    last = health_repo.latest_poll_logs(db).get(SYNC_POLL_LOG_PLATFORM)
    return last.started_at if last is not None else None
