"""单链接"只下载这一条"执行器：不触碰全局队列，只处理指定的一条作品。

与 ``jobs.process_link_analysis``（完整流水线）并行存在，本模块是 MVP 的
**仅下载**变体：认领 → 下载并写归档 checkpoint → 状态置 DOWNLOADED。
转写/抽帧/AI 笔记阶段保持 pending，后续可用既有重试/流水线路径继续。

复用既有链路：
- ``jobs._claim_work_for_processing``：跨进程原子认领（NEW/QUEUED → DOWNLOADING）；
- ``jobs.download_and_checkpoint``：下载 + Work/ArchiveEntry/同步任务落库；
- ``jobs._safe_error_text``：失败文案白名单，凭据/路径不进 DB/API/页面。
"""
from __future__ import annotations

import logging

from monitor.pipeline.download import PipelineOfflineError
from monitor.store.shared_db import SessionLocal

logger = logging.getLogger("monitor.pipeline.single_link")

# 仅下载模式可启动的状态：QUEUED（新建/重试后）。DOWNLOADING 需先确认认领已死。
_RUNNABLE_STATUSES = {"QUEUED", "NEW"}


def _mark_parse_succeeded(db, work_id: int) -> None:
    from app.repositories import task_repo

    task_repo.upsert(db, work_id, "parse", "succeeded", attempts=1, error=None)


def run_single_link_download(work_id: int) -> dict[str, str | int]:
    """只下载用户刚提交的一条作品；成功置 DOWNLOADED，失败置 FAILED。

    返回 {"work_id", "status"}；绝不扫描或处理其他作品，不写触发文件，
    不启动任何轮询/定时任务。
    """
    from app.repositories import task_repo, work_repo
    from monitor import jobs

    with SessionLocal() as db:
        work = db.get(jobs.Work, work_id)
        if work is None:
            raise ValueError(f"work {work_id} 不存在")
        if work.status == "DOWNLOADING" and jobs._claim_stale(
            work.download_claimed_at, work.download_claim_pid
        ):
            # 上一次执行者已死亡：CAS 恢复为 QUEUED 后按新作品认领
            jobs._recover_stale_claim(db, work_id, work.download_claimed_at, work.download_claim_pid)
            db.refresh(work)
        if work.status not in _RUNNABLE_STATUSES:
            # 已在处理/已完成/已下载：不重复下载，返回现状即可
            return {"work_id": work.id, "status": work.status}
        if not jobs._claim_work_for_processing(db, work_id, personal=False):
            return {"work_id": work.id, "status": "DOWNLOADING"}
        _mark_parse_succeeded(db, work_id)
        db.commit()
        db.refresh(work)

        try:
            jobs.download_and_checkpoint(db, work, allow_browser_fallback=False)
            work.status = "DOWNLOADED"
            work.last_error = None
            db.commit()
            jobs._clear_claim(db, work_id)
            db.commit()
            logger.info("single-link download %s finished: status=DOWNLOADED", work.id)
        except PipelineOfflineError as exc:
            safe_message = jobs._safe_error_text(exc)
            work.status = "FAILED"
            work.last_error = safe_message
            task_repo.mark_failed(db, work.id, "download", safe_message)
            jobs._clear_claim(db, work_id)
            db.commit()
            logger.warning("single-link download %s offline: error_type=%s", work.id, type(exc).__name__)
        except Exception as exc:  # noqa: BLE001
            safe_message = jobs._safe_error_text(exc)
            work.status = "FAILED"
            work.last_error = safe_message
            task_repo.mark_failed(db, work.id, "download", safe_message)
            jobs._clear_claim(db, work_id)
            db.commit()
            logger.warning("single-link download %s failed: error_type=%s", work.id, type(exc).__name__)
        return {"work_id": work.id, "status": work.status}


def requeue_failed_for_download(db, work_id: int) -> bool:
    """FAILED 作品回到 QUEUED 以便重新执行仅下载（等价手动重试语义）。"""
    from app.models import Work
    from app.repositories import work_repo

    work = db.get(Work, work_id)
    if work is None or work.status != "FAILED":
        return False
    work_repo.update(
        db,
        work,
        status="QUEUED",
        last_error=None,
        retry_count=work.retry_count + 1,
        download_claimed_at=None,
        download_claim_pid=None,
    )
    return True
