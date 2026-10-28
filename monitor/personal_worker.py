"""个人来源任务进程：只运行已授权的本人点赞/收藏定时同步与下载。

用法（Mac）：
    python -m monitor.personal_worker

与 `monitor/main.py`（完整监控层：RSSHub/抖音博主轮询、trigger 消费、流水线）
不同，本进程只注册两类任务：
- write_personal_heartbeat：独立心跳（页面据此显示"个人任务进程在线"）
- check_douyin_personal_sync：tick——同步到期执行 + 已授权下载积压消费

默认全部关闭；用户在前端"个人来源"页开启后本进程才会真正执行读取/下载。
不注册任何其他平台轮询、不消费 trigger、不启动全局下载队列。
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from monitor.store.shared_db import settings  # noqa: E402

from app.core.db import init_db  # noqa: E402

from monitor import jobs  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("monitor.personal_worker")

PERSONAL_TICK_JOB_ID = "check_douyin_personal_sync"
PERSONAL_HEARTBEAT_JOB_ID = "personal_worker_heartbeat"


def build_personal_scheduler() -> BackgroundScheduler:
    """只注册个人来源相关任务；注册清单由隔离测试锁定（防止隐式扩大范围）。"""
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        jobs.write_personal_heartbeat,
        IntervalTrigger(seconds=settings.heartbeat_interval),
        id=PERSONAL_HEARTBEAT_JOB_ID,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        jobs.check_douyin_personal_sync,
        IntervalTrigger(seconds=settings.monitor_personal_sync_tick_interval),
        id=PERSONAL_TICK_JOB_ID,
        max_instances=1,
        coalesce=True,
    )
    return scheduler


def main() -> None:
    init_db()
    jobs.write_personal_heartbeat()
    scheduler = build_personal_scheduler()
    scheduler.start()
    logger.info("个人来源任务进程已启动 pid=%s", __import__("os").getpid())
    logger.info(
        "只注册任务: %s（同步/下载开关读 app_settings，默认关闭）",
        [PERSONAL_HEARTBEAT_JOB_ID, PERSONAL_TICK_JOB_ID],
    )
    try:
        while True:
            time.sleep(5)
    except (KeyboardInterrupt, SystemExit):
        logger.info("个人来源任务进程退出")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
