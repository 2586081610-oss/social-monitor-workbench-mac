"""失败作品重试入口：等价于 pipeline_runner --retry-failed。

导入 pipeline_runner 即完成 FunASR 进程内补丁（asr_compat.install_asr_patch），
重试转写时与主 runner 行为一致。

用法：
  python scripts/retry_failed.py               # 重试 checkpoint 中全部 failed 作品
  python scripts/retry_failed.py 16693 16694   # 只重试指定 work_id（须在 selected 列表中）
  python scripts/retry_failed.py --dry-run     # 只打印计划，不执行
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pipeline_runner  # noqa: E402  # 导入即安装 FunASR 补丁


def main() -> None:
    args = sys.argv[1:]
    dry = ["--dry-run"] if "--dry-run" in args else []
    ids = [a for a in args if a != "--dry-run"]
    if ids:
        for raw in ids:
            pipeline_runner.main(["--retry-failed", "--only", raw, *dry])
        return
    pipeline_runner.main(["--retry-failed", *dry])


if __name__ == "__main__":
    main()
