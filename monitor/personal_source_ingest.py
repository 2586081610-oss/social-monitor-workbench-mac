"""本人来源导入编排（C3 + F2 通道）：快照 → marks/归属表 + 守卫式 works 登记。

设计：`preflight/favorites-ingest-design.md`。要点：
- (platform, external_id, source_mark) 幂等；同作品多来源多夹并存。
- F2 通道（--source f2）额外支持平台多夹：归属事实写 personal_source_folder_items
  （一作多夹），快照缺项不删除；夹内-only 作品只登记不断言收藏状态。
- 已存在作品零触碰，只允许"补未知"：published 四件套全为 NULL 且本次快照
  带完整可证发布时间时才补填，其余字段（标题/URL/封面/合集/状态）一律不动。
- 快照缺数据不抹既有事实：folders 数据不可用时保留 marks 已有 folder 信息。
- 默认 DOUYIN_PERSONAL_SOURCE=off——不 env 显式开启就不读任何来源。
- 用法：`python -m monitor.personal_source_ingest [--source file|api|f2] [--dry-run]`
- 非 dry-run 时同步结果（含失败原因）写入 poll_logs（platform=douyin-personal）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_BACKEND = _ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from monitor.platforms.douyin_f2_source import (  # noqa: E402
    confirm_f2_progress,
    f2_sync_lock,
    read_snapshot_f2,
)
from monitor.platforms.douyin_personal_source import (  # noqa: E402
    PersonalItem,
    PersonalSnapshot,
    PersonalSourceError,
    read_snapshot_api,
    read_snapshot_files,
    redact_sensitive_text,
    resolve_data_dirs,
    snapshot_utc_now,
)

SOURCE_ENV = "DOUYIN_PERSONAL_SOURCE"
API_BASE_ENV = "DDR_API_BASE"
DEFAULT_API_BASE = "http://127.0.0.1:4765"
# F2 通道设置（存储在 app_settings 表，前端定时配置页维护；env 不可用时为默认值）
SUPPORTED_SOURCES = ("file", "api", "f2")
FOLDER_MARK_SNAPSHOT_ONLY = "favorite_folder"  # 仅登记作品，不写 marks 行
SYNC_POLL_LOG_PLATFORM = "douyin-personal"  # 与 app.services.personal_source_service 一致


@dataclass
class IngestReport:
    source: str
    snapshot_id: str = ""
    items_in_snapshot: int = 0
    marks_created: int = 0
    marks_updated: int = 0
    works_created: int = 0
    works_publication_filled: list[str] = field(default_factory=list)
    folder_items_created: int = 0
    folder_items_updated: int = 0
    incomplete_lists: list[str] = field(default_factory=list)
    skipped_invalid: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    direct_sync: dict = field(default_factory=dict)
    folders_available: bool = False
    snapshot_updated_at: str | None = None
    ingested_at: str = ""
    dry_run: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    def to_safe_dict(self) -> dict:
        """日志/CLI 输出用：只含类别与数量，不含作品/收藏夹/账号标识。

        - works_publication_filled（作品 ID 列表）→ 数量
        - skipped_invalid（含原始 ID 片段）→ 数量
        - incomplete_lists（folder:<真实夹 ID>）→ 夹计数
        """
        data = self.to_dict()
        data["works_publication_filled"] = len(self.works_publication_filled)
        data["skipped_invalid_count"] = len(self.skipped_invalid)
        data.pop("skipped_invalid", None)
        folder_incomplete = sum(1 for i in self.incomplete_lists if i.startswith("folder"))
        list_incomplete = [i for i in self.incomplete_lists if not i.startswith("folder")]
        data["incomplete_lists"] = list_incomplete + ([f"folder×{folder_incomplete}"] if folder_incomplete else [])
        data["download_processed"] = getattr(self, "download_processed", None)
        return data


def _guarded_publication_fill(work, item: PersonalItem) -> bool:
    """已存在作品只允许补全空缺的发布时间四件套；任何已有可信值都不碰。"""
    if not item.published_at:
        return False
    if any(getattr(work, key, None) for key in (
        "published_at", "published_time_source", "published_time_precision", "published_time_timezone",
    )):
        return False
    work.published_at = item.published_at
    work.published_time_source = "douyin_web_create_time"
    work.published_time_precision = "second"
    work.published_time_timezone = "UTC"
    return True


def ingest_snapshot(snapshot: PersonalSnapshot, db, *, dry_run: bool = False) -> IngestReport:
    """把一个快照写入当前会话（不 commit，由调用方决定事务边界）。"""
    from app.models import PersonalSourceMark, PersonalSourceFolderItem
    from app.repositories import work_repo
    from app.utils.timeutil import now_iso

    report = IngestReport(
        source=snapshot.source,
        snapshot_id=snapshot.snapshot_id,
        items_in_snapshot=len(snapshot.items),
        skipped_invalid=list(snapshot.skipped_invalid),
        warnings=list(snapshot.warnings),
        direct_sync=dict(snapshot.direct_sync),
        folders_available=snapshot.folders_available,
        # 完整性显式上报：列表级（direct_sync=False）+ 夹级（folder_incomplete）
        incomplete_lists=(
            [scope for scope, complete in snapshot.direct_sync.items() if not complete]
            + list(snapshot.folder_incomplete)
        ),
        snapshot_updated_at=snapshot.snapshot_updated_at,
        ingested_at=now_iso(),
        dry_run=dry_run,
    )

    for item in snapshot.items:
        # favorite_folder（F2 夹内作品）：只登记作品与归属事实，不断言点赞/收藏状态
        if item.source_mark != FOLDER_MARK_SNAPSHOT_ONLY:
            mark = db.query(PersonalSourceMark).filter_by(
                platform="douyin", external_id=item.external_id, source_mark=item.source_mark,
            ).one_or_none()
            if mark is None:
                mark = PersonalSourceMark(
                    platform="douyin",
                    external_id=item.external_id,
                    source_mark=item.source_mark,
                    source_channel=snapshot.source,
                    snapshot_id=snapshot.snapshot_id,
                    occurred_at=item.occurred_at,
                    occurred_at_source=item.occurred_at_source,
                    first_seen_at=report.ingested_at,
                    last_seen_at=report.ingested_at,
                )
                db.add(mark)
                db.flush()
                report.marks_created += 1
            else:
                report.marks_updated += 1
                if mark.occurred_at is None and item.occurred_at:
                    mark.occurred_at = item.occurred_at
                    mark.occurred_at_source = item.occurred_at_source
                mark.last_seen_at = report.ingested_at
                mark.snapshot_id = snapshot.snapshot_id
                mark.source_channel = snapshot.source
            # DDR 单夹事实只在本次快照确实携带 folders 数据时更新（缺数据不抹既有事实）
            if snapshot.folder_mode == "single" or (snapshot.folder_mode is None and snapshot.folders_available):
                assigned = snapshot.folder_assignments.get(item.external_id)
                if assigned:
                    mark.folder_id = assigned
                    mark.folder_name = snapshot.folders.get(assigned)
                else:
                    mark.folder_id = None
                    mark.folder_name = None

        work = work_repo.get_by_platform_external(db, "douyin", item.external_id)
        if work is None:
            work = work_repo.upsert(
                db,
                "douyin",
                item.external_id,
                title=item.title or "未命名视频",
                url=item.url,
                source_type="personal_source",
                content_type=item.media_type or "video",
                cover_url=item.cover_url,
                published_at=item.published_at,
                published_time_source=("douyin_web_create_time" if item.published_at else None),
                published_time_precision=("second" if item.published_at else None),
                published_time_timezone=("UTC" if item.published_at else None),
            )
            report.works_created += 1
        elif _guarded_publication_fill(work, item):
            report.works_publication_filled.append(item.external_id)
        if item.source_mark != FOLDER_MARK_SNAPSHOT_ONLY:
            mark.work_id = work.id

        # F2 平台多夹归属事实：upsert、快照缺项不删除（不推断取消归属）
        if snapshot.folder_mode == "multi":
            folder_ids = snapshot.folder_memberships.get(item.external_id) or []
            if folder_ids:
                existing = {
                    row.folder_id: row
                    for row in db.query(PersonalSourceFolderItem).filter_by(
                        platform="douyin", external_id=item.external_id,
                    ).all()
                }
                for folder_id in folder_ids:
                    row = existing.get(folder_id)
                    if row is None:
                        db.add(PersonalSourceFolderItem(
                            platform="douyin",
                            external_id=item.external_id,
                            folder_id=folder_id,
                            folder_name=snapshot.folders.get(folder_id) or None,
                            source_channel=snapshot.source,
                            snapshot_id=snapshot.snapshot_id,
                            work_id=work.id,
                            first_seen_at=report.ingested_at,
                            last_seen_at=report.ingested_at,
                        ))
                        report.folder_items_created += 1
                    else:
                        row.last_seen_at = report.ingested_at
                        row.snapshot_id = snapshot.snapshot_id
                        row.source_channel = snapshot.source
                        row.work_id = work.id
                        row.folder_name = snapshot.folders.get(folder_id) or row.folder_name
                        report.folder_items_updated += 1

    if not dry_run:
        db.flush()
    return report


def _read_f2_config(*, create: bool = True) -> dict:
    """从 app_settings 读取 F2 通道配置（与后端 /personal-source/schedule 同一存储）。

    create=True：显式 init_db（幂等）保证空库/新机也能同步。
    create=False（dry-run）：**真只读**访问——mode=ro 打开、库/表缺失或任何
    SQLite 错误都回退默认值，绝不建库建表（回归保护：既有库 0 表必须保持 0 表）。
    """
    from app.core.config import settings
    from app.services import personal_source_service

    if not create:
        readonly = personal_source_service.read_config_readonly(settings.database_path)
        return {
            "scopes": tuple(readonly["scopes"]),
            "sec_user_id": readonly["sec_user_id"],
        }
    from app.core.db import init_db
    from monitor.store.shared_db import SessionLocal

    init_db()
    with SessionLocal() as db:
        config = personal_source_service.read_config(db)
    return {
        "scopes": tuple(config.scopes),
        "sec_user_id": config.sec_user_id,
    }


def _load_snapshot(
    source: str,
    account: str | None,
    base_dir: str | None,
    *,
    f2_config: dict | None = None,
    dry_run: bool = False,
) -> PersonalSnapshot:
    if source == "file":
        dirs = resolve_data_dirs(base_dir)
        if not dirs:
            raise PersonalSourceError(
                "missing_file",
                f"未找到 DouyinDataReview records.json（base={base_dir or '默认路径，可用 DDR_DATA_DIR 覆盖'}）",
            )
        if len(dirs) > 1 and account is None:
            names = ", ".join(d.parent.name for d in dirs)
            raise PersonalSourceError(
                "ambiguous_account",
                f"检测到多个账号目录，请用 --account 指定: {names}",
            )
        chosen = dirs[0]
        if account is not None:
            matches = [d for d in dirs if d.parent.name == account] or \
                      [d for d in dirs if d.name == account]
            if not matches:
                raise PersonalSourceError("missing_file", f"账号目录不存在: {account}")
            chosen = matches[0]
        return read_snapshot_files(chosen / "records.json", chosen / "folders.json")
    if source == "api":
        return read_snapshot_api(os.getenv(API_BASE_ENV, DEFAULT_API_BASE))
    if source == "f2":
        config = f2_config or {}
        return read_snapshot_f2(
            scopes=tuple(config.get("scopes") or ()),
            sec_user_id=str(config.get("sec_user_id") or ""),
            # dry-run 不读写续传状态，避免只预演却推进游标
            progress_file="" if dry_run else None,
        )
    raise PersonalSourceError("bad_source", f"未知来源: {source}（支持 {'|'.join(SUPPORTED_SOURCES)}）")


def _record_sync_poll_log(
    status: str,
    detail: str | None,
    *,
    started_at: str,
    new_works: int = 0,
) -> None:
    """同步结果落 poll_logs（platform=douyin-personal），供来源状态页与定时判到期。

    失败也如实记录；记录失败本身不得掩盖原始错误。
    """
    try:
        from app.core.db import init_db
        from app.repositories import health_repo
        from monitor.store.shared_db import SessionLocal

        init_db()
        with SessionLocal() as db:
            health_repo.create_poll_log(
                db,
                SYNC_POLL_LOG_PLATFORM,
                status,
                detail=redact_sensitive_text(detail) if detail else None,
                started_at=started_at,
                new_works=new_works,
            )
            db.commit()
    except Exception:  # noqa: BLE001 - 状态记录失败不掩盖同步本身的错误
        pass


def run_ingest(
    source: str | None = None,
    *,
    account: str | None = None,
    base_dir: str | None = None,
    dry_run: bool = False,
) -> IngestReport:
    """入口：检查 env 开关 → 读快照 → 入库。off（默认）时显式拒绝。

    非 dry-run 时同步结果（成功或失败原因）写入 poll_logs；
    dry-run 只预演，不写任何存储。
    F2 通道（非 dry-run）在 f2_sync_lock 内完成取数 → 数据库提交 → 续传确认
    （提交协议）：业务数据持久提交后 confirm_f2_progress 才推进/清除游标；
    任何失败（含占锁失败、取数失败、提交失败）都不删除、不推进续传状态——
    已确认游标只指向已提交的数据，重试从游标处幂等重放。
    """
    started_at = snapshot_utc_now()
    enabled = (source or os.getenv(SOURCE_ENV, "off")).lower()
    if enabled == "off":
        raise PersonalSourceError(
            "disabled",
            f"{SOURCE_ENV}=off（默认）。显式设置 {SOURCE_ENV}={'|'.join(SUPPORTED_SOURCES)} 或 --source 后才会读取本人数据",
        )
    if enabled not in SUPPORTED_SOURCES:
        raise PersonalSourceError("bad_source", f"未知来源: {enabled}（支持 {'|'.join(SUPPORTED_SOURCES)}）")
    f2_config = None
    if enabled == "f2":
        f2_config = _read_f2_config(create=not dry_run)

    def _load_snapshot_or_log_fail() -> PersonalSnapshot:
        try:
            return _load_snapshot(enabled, account, base_dir, f2_config=f2_config,
                                  dry_run=dry_run)
        except PersonalSourceError as exc:
            # 解析/取数失败如实记录；不清理续传状态（它只指向已提交的数据）
            if not dry_run:
                _record_sync_poll_log("fail", f"{exc.kind}: {exc}", started_at=started_at)
            raise

    def _commit_snapshot(snapshot: PersonalSnapshot) -> IngestReport:
        # Standalone CLI has no backend lifespan.
        from app.core.db import init_db
        from monitor.store.shared_db import SessionLocal

        init_db()
        with SessionLocal() as db:
            report = ingest_snapshot(snapshot, db)
            db.commit()
        return report

    def _log_outcome(report: IngestReport, *, confirm_failure: str = "") -> None:
        if confirm_failure:
            # 确认失败不冒充干净成功：数据已提交、旧确认状态保全，下一轮从
            # 已确认游标幂等重放；固定文案，不泄露敏感信息。
            report.warnings.append(confirm_failure)
        details = []
        if confirm_failure:
            details.append(confirm_failure)
        if report.incomplete_lists:
            # 不完整 ≠ 成功：显式 partial，让页面与日志都能看见（不冒充干净成功）
            details.append("incomplete: " + ", ".join(report.incomplete_lists))
        if details:
            _record_sync_poll_log(
                "partial",
                "; ".join(details),
                started_at=started_at,
                new_works=report.works_created,
            )
        else:
            _record_sync_poll_log(
                "ok",
                None,
                started_at=started_at,
                new_works=report.works_created,
            )

    if dry_run:
        # Parse failures must not initialize storage either.
        snapshot = _load_snapshot_or_log_fail()
        import sqlite3
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from app.core.config import settings
        from app.core.db import init_db

        # Preview all mutations on a private in-memory copy. Immutable read avoids
        # journal/SHM creation on the source; refuse live WAL rather than omit it.
        path = settings.database_path
        memory = sqlite3.connect(":memory:")
        preview_engine = create_engine("sqlite://", creator=lambda: memory, poolclass=StaticPool)
        try:
            if path.exists():
                wal = Path(str(path) + "-wal")
                if wal.exists() and wal.stat().st_size:
                    raise PersonalSourceError("database_busy", "试运行需静止数据库：请先停止服务并完成 WAL 检查点")
                with sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True) as original:
                    original.backup(memory)
            preview_sessions = sessionmaker(bind=preview_engine, autoflush=False, expire_on_commit=False)
            init_db(db_engine=preview_engine, session_factory=preview_sessions, create_dirs=False)
            with preview_sessions() as db:
                report = ingest_snapshot(snapshot, db, dry_run=True)
        finally:
            preview_engine.dispose()
    elif enabled == "f2":
        confirm_failure = ""
        try:
            # 互斥覆盖完整生命周期：其他进程取数/提交/确认期间不能插入；
            # 占锁失败在这里直接 already_running，绝不触碰持有者的进度。
            with f2_sync_lock():
                snapshot = _load_snapshot_or_log_fail()
                report = _commit_snapshot(snapshot)
                # 提交协议确认步：数据已持久提交，才允许推进/清除续传游标。
                # 确认失败（写入/替换/清理的 OSError）必须如实上报——数据库
                # 可能已提交：保留已入库事实与旧确认状态，下一轮从已确认游标
                # 幂等重放；文件系统恢复后确认成功即可重新开始新一轮。
                if getattr(snapshot, "resume_state", None) is not None \
                        and not confirm_f2_progress(snapshot):
                    confirm_failure = (
                        "resume_confirm_failed: 进度确认未写入或清理成功，"
                        "已提交数据保全，下一轮将从已确认游标重放")
        except PersonalSourceError as exc:
            if exc.kind == "already_running":
                # 占锁失败：本次同步放弃，持有者的进度与数据不受影响
                _record_sync_poll_log("fail", f"{exc.kind}: {exc}", started_at=started_at)
            raise
        _log_outcome(report, confirm_failure=confirm_failure)
    else:
        snapshot = _load_snapshot_or_log_fail()
        report = _commit_snapshot(snapshot)
        _log_outcome(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="导入本人抖音点赞/收藏/收藏夹快照（只读）")
    parser.add_argument("--source", choices=list(SUPPORTED_SOURCES), default=None,
                        help=f"数据来源（缺省读 {SOURCE_ENV}；两者都无 = off）")
    parser.add_argument("--account", default=None, help="多账号时的账号目录名")
    parser.add_argument("--base-dir", default=None, help="覆盖 DDR 数据目录")
    parser.add_argument("--dry-run", action="store_true", help="解析并生成报告，不写库")
    parser.add_argument("--download", action="store_true",
                        help="成功导入后消费已授权的下载积压（douyin_personal_auto_download 控制，默认零处理）")
    args = parser.parse_args()
    try:
        report = run_ingest(args.source, account=args.account, base_dir=args.base_dir, dry_run=args.dry_run)
        if args.download and not args.dry_run:
            from monitor.jobs import download_personal_source_works

            report.download_processed = download_personal_source_works()
    except PersonalSourceError as exc:
        # 消息本身按白名单构造；再过一遍脱敏兜底（不落凭据/账号标识）
        print(json.dumps({
            "status": "error",
            "kind": exc.kind,
            "message": redact_sensitive_text(str(exc)),
        }, ensure_ascii=False))
        return 1
    # 安全输出：只含类别与数量，不含作品/收藏夹/账号标识（子进程输出会被留存在手动日志）
    print(json.dumps({"status": "ok", **report.to_safe_dict()}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
