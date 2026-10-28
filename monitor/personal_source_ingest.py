"""本人点赞/收藏导入编排（C3）：快照 → marks 表 + 守卫式 works 登记。

设计：`preflight/favorites-ingest-design.md`。要点：
- (platform, external_id, source_mark) 幂等；同作品多来源多夹并存。
- 已存在作品零触碰，只允许"补未知"：published 四件套全为 NULL 且本次快照
  带完整可证发布时间时才补填，其余字段（标题/URL/封面/合集/状态）一律不动。
- 快照缺数据不抹既有事实：folders.json 不可用时保留 marks 已有 folder 信息。
- 默认 DOUYIN_PERSONAL_SOURCE=off——不 env 显式开启就不读任何来源。
- 用法：`python -m monitor.personal_source_ingest [--source file|api] [--dry-run]`
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

from monitor.platforms.douyin_personal_source import (  # noqa: E402
    PersonalItem,
    PersonalSnapshot,
    PersonalSourceError,
    read_snapshot_api,
    read_snapshot_files,
    resolve_data_dirs,
    snapshot_utc_now,
)

SOURCE_ENV = "DOUYIN_PERSONAL_SOURCE"
API_BASE_ENV = "DDR_API_BASE"
DEFAULT_API_BASE = "http://127.0.0.1:4765"


@dataclass
class IngestReport:
    source: str
    snapshot_id: str = ""
    items_in_snapshot: int = 0
    marks_created: int = 0
    marks_updated: int = 0
    works_created: int = 0
    works_publication_filled: list[str] = field(default_factory=list)
    skipped_invalid: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    direct_sync: dict = field(default_factory=dict)
    folders_available: bool = False
    snapshot_updated_at: str | None = None
    ingested_at: str = ""
    dry_run: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)


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
    from app.models import PersonalSourceMark
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
        snapshot_updated_at=snapshot.snapshot_updated_at,
        ingested_at=now_iso(),
        dry_run=dry_run,
    )

    for item in snapshot.items:
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
        # folder 事实只在本次快照确实携带 folders 数据时更新（缺数据不抹既有事实）
        if snapshot.folders_available:
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
        mark.work_id = work.id

    if not dry_run:
        db.flush()
    return report


def _load_snapshot(source: str, account: str | None, base_dir: str | None) -> PersonalSnapshot:
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
    raise PersonalSourceError("bad_source", f"未知来源: {source}（支持 file|api）")


def run_ingest(
    source: str | None = None,
    *,
    account: str | None = None,
    base_dir: str | None = None,
    dry_run: bool = False,
) -> IngestReport:
    """入口：检查 env 开关 → 读快照 → 入库。off（默认）时显式拒绝。"""
    enabled = (source or os.getenv(SOURCE_ENV, "off")).lower()
    if enabled == "off":
        raise PersonalSourceError(
            "disabled",
            f"{SOURCE_ENV}=off（默认）。显式设置 {SOURCE_ENV}=file|api 或 --source 后才会读取本人数据",
        )
    from app.core.db import init_db
    from monitor.store.shared_db import SessionLocal

    # 独立 CLI 进程没有 backend lifespan：先幂等建表（含 personal_source_marks）
    init_db()
    snapshot = _load_snapshot(enabled, account, base_dir)
    with SessionLocal() as db:
        report = ingest_snapshot(snapshot, db, dry_run=dry_run)
        if not dry_run:
            db.commit()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="导入 DouyinDataReview 本人点赞/收藏快照（只读）")
    parser.add_argument("--source", choices=["file", "api"], default=None,
                        help=f"数据来源（缺省读 {SOURCE_ENV}；两者都无 = off）")
    parser.add_argument("--account", default=None, help="多账号时的账号目录名")
    parser.add_argument("--base-dir", default=None, help="覆盖 DDR 数据目录")
    parser.add_argument("--dry-run", action="store_true", help="解析并生成报告，不写库")
    args = parser.parse_args()
    try:
        report = run_ingest(args.source, account=args.account, base_dir=args.base_dir, dry_run=args.dry_run)
    except PersonalSourceError as exc:
        print(json.dumps({"status": "error", "kind": exc.kind, "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "ok", **report.to_dict()}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
