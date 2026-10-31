"""运行一条手动链接分析作品，不扫描或处理全局队列。

默认执行完整流水线（下载→转写→抽帧→笔记待生成）；
``--download-only`` 只下载该条作品并写归档 checkpoint（MVP 单链接下载入口）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor import jobs  # noqa: E402
from monitor.pipeline import single_link  # noqa: E402
from monitor.store.shared_db import init_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="处理一条手动链接作品（不触碰全局队列）")
    parser.add_argument("--work-id", type=int, required=True)
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="仅下载该条作品并登记归档，不执行转写/抽帧/笔记阶段",
    )
    args = parser.parse_args()
    if args.work_id < 1:
        parser.error("--work-id 必须为正整数")
    init_db()
    try:
        if args.download_only:
            result = single_link.run_single_link_download(args.work_id)
        else:
            result = jobs.process_link_analysis(args.work_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] != "FAILED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
