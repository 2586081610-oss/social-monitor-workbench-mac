"""SQLAlchemy engine/session + 建表与目录初始化（幂等）。"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    pass


# URI 模式连接：本机环境普通 connect 的写打开会被 WorkBuddy 沙盒拦截为只读
# （实测 sqlite:///path 打开 CREATE TABLE 报 readonly；file:...?mode=rwc + uri=true 正常读写）。
engine = create_engine(
    f"sqlite:///file:{settings.database_path.as_posix()}?mode=rwc&uri=true",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _enable_sqlite_pragmas(dbapi_conn, _record) -> None:
    """WAL + busy_timeout：多个执行器（单链接下载/完整流水线/后端）并发读写同一库时
    读者不阻塞写者，写锁冲突等待 5 秒而不是立即 'database is locked'。"""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)

_COOKIE_PLATFORMS = ["douyin", "bilibili", "xhs", "twitter"]


def _ensure_dirs() -> None:
    for d in (settings.media_root, settings.obsidian_root, settings.pending_dir, settings.trigger_dir):
        Path(d).mkdir(parents=True, exist_ok=True)


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """幂等加列：PRAGMA table_info 检查列不存在时 ALTER TABLE ADD COLUMN（v0.4 迁移）。"""
    cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
    if column not in cols:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def init_db(*, db_engine=None, session_factory=None, create_dirs: bool = True) -> None:
    """建表 + 种子 cookie 健康度行 + 创建存储目录。幂等，聚合 API 与监控层共用。"""
    db_engine = engine if db_engine is None else db_engine
    session_factory = SessionLocal if session_factory is None else session_factory
    if create_dirs:
        _ensure_dirs()
    # 延迟导入避免循环依赖（models 依赖 db.Base）
    from app.models import (  # noqa: F401  确保全部表注册到 metadata
        CookieHealth,
    )

    Base.metadata.create_all(bind=db_engine)

    # v0.4 轻量迁移：works 新增 content_type / media_urls（幂等，已存在则跳过）
    with db_engine.begin() as conn:
        _ensure_column(conn, "works", "content_type", "VARCHAR(10) NOT NULL DEFAULT 'video'")
        _ensure_column(conn, "works", "media_urls", "TEXT")
        # v0.5 迁移：lexicon 新增 category（兴趣区间：finance/ai/fitness/business/general）
        _ensure_column(conn, "lexicon", "category", "VARCHAR(30)")
        # v0.6 迁移：poll_logs 新增 new_works（本次爬取检测到的新作品数，默认 0）
        _ensure_column(conn, "poll_logs", "new_works", "INTEGER NOT NULL DEFAULT 0")
        # v0.6 迁移：works 新增 cover_url（作品封面图，前端列表展示）
        _ensure_column(conn, "works", "cover_url", "TEXT")
        _ensure_column(conn, "works", "downloaded_at", "TEXT")
        _ensure_column(conn, "works", "download_time_source", "TEXT")
        # Archive Task 3: nullable publication provenance for future collector
        # responses; old rows intentionally remain unknown.
        _ensure_column(conn, "works", "published_time_source", "TEXT")
        _ensure_column(conn, "works", "published_time_precision", "VARCHAR(20)")
        _ensure_column(conn, "works", "published_time_timezone", "VARCHAR(64)")
        _ensure_column(conn, "works", "metadata_checked_at", "TEXT")
        # v0.7 迁移：个人来源下载的跨进程原子认领（幂等，老库补列）
        _ensure_column(conn, "works", "download_claimed_at", "TEXT")
        _ensure_column(conn, "works", "download_claim_pid", "INTEGER")
        # v0.8 迁移：creators 每博主自定义检测间隔 + 失败重试/警示状态（幂等，老库补列）
        _ensure_column(conn, "creators", "poll_interval_seconds", "INTEGER")
        _ensure_column(conn, "creators", "next_poll_at", "TEXT")
        _ensure_column(conn, "creators", "poll_fail_streak", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "creators", "last_poll_error", "TEXT")

    with session_factory() as db:
        from app.repositories.health_repo import get_cookie, upsert_cookie

        for platform in _COOKIE_PLATFORMS:
            # 仅补缺失行：重启不得清空已判定的登录态（OK/WARN/NEEDS_RELOGIN 是校验结果，不是启动状态）
            if get_cookie(db, platform) is None:
                upsert_cookie(db, platform=platform, status="UNKNOWN", note=None)
        db.commit()
