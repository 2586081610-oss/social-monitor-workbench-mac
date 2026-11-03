"""失败作品重试入口：等价于 pipeline_runner --retry-failed。

导入 pipeline_runner 即完成 FunASR 进程内补丁（asr_compat.install_asr_patch），
重试转写时与主 runner 行为一致。

用法：
  python retry_failed.py               # 重试 checkpoint 中全部 failed 作品
  python retry_failed.py 16693 16694   # 只重试指定 work_id（须在 selected 列表中）
  python retry_failed.py --dry-run     # 只打印计划，不执行
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pipeline_runner  # noqa: E402  # 导入即安装 FunASR 补丁


def main() -> None:
    ap = argparse.ArgumentParser(description="失败作品重试入口（等价 pipeline_runner --retry-failed）")
    ap.add_argument("work_ids", nargs="*", type=int, metavar="WORK_ID",
                    help="只重试指定 work_id；缺省重试全部 failed")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不执行、不写 checkpoint")
    args = ap.parse_args()
    dry = ["--dry-run"] if args.dry_run else []
    if args.work_ids:
        for wid in args.work_ids:
            pipeline_runner.main(["--retry-failed", "--only", str(wid), *dry])
        return
    pipeline_runner.main(["--retry-failed", *dry])


if __name__ == "__main__":
    main()
