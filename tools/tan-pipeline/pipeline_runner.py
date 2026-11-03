"""后台流水线 runner：对选中的 14 部B站作品逐个执行 下载→转写→抽帧→pending包。

断点：data/pipeline_checkpoint.json 记录已完成/失败 work_id，重跑自动跳过。

用法：
  python scripts/pipeline_runner.py                 # 默认：跳过 done/failed，处理剩余（行为同旧版）
  python scripts/pipeline_runner.py --retry-failed  # 重试 failed 中的作品（先自动备份 checkpoint）
  python scripts/pipeline_runner.py --only 16693    # 只处理指定 work_id（若在 failed 中则视为重试）
  python scripts/pipeline_runner.py --dry-run       # 只打印计划，不执行、不写 checkpoint、不备份
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path

from tan_paths import DATA_ROOT, WORKBENCH_ROOT, inject_syspath  # noqa: E402

WS = DATA_ROOT  # 任务数据目录（含 data/ 的工作区根，TAN_DATA_DIR 可覆盖）
ROOT = WORKBENCH_ROOT  # workbench 仓库根（向上探测，TAN_WORKBENCH 可覆盖）
inject_syspath()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# funasr 已知 bug 补丁（仅本进程）：token 与 timestamp 数组错位时 IndexError。
# 对齐长度后重试；不修改项目与库文件。须在 import monitor.jobs 之前完成。
from asr_compat import install_asr_patch  # noqa: E402

install_asr_patch()

from monitor import jobs  # noqa: E402
from monitor.store.shared_db import SessionLocal, init_db  # noqa: E402

SELECTION = json.loads((WS / "data" / "pipeline_selection.json").read_text())
CHECKPOINT = WS / "data" / "pipeline_checkpoint.json"


def load_checkpoint() -> dict:
    state = json.loads(CHECKPOINT.read_text()) if CHECKPOINT.exists() else {"done": [], "failed": []}
    state.setdefault("done", [])
    state.setdefault("failed", [])
    return state


def save_checkpoint(state: dict) -> None:
    CHECKPOINT.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def backup_checkpoint() -> Path:
    """重试前备份 checkpoint 到同目录带时间戳的 .bak 文件（同名自动加序号）。"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = CHECKPOINT.with_name(f"{CHECKPOINT.stem}.bak-{ts}{CHECKPOINT.suffix}")
    n = 1
    while bak.exists():
        bak = CHECKPOINT.with_name(f"{CHECKPOINT.stem}.bak-{ts}-{n}{CHECKPOINT.suffix}")
        n += 1
    if CHECKPOINT.exists():
        shutil.copy2(CHECKPOINT, bak)
    else:
        bak.write_text(json.dumps({"done": [], "failed": []}, ensure_ascii=False, indent=1))
    return bak


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="作品处理流水线 runner（断点续跑）")
    ap.add_argument("--retry-failed", action="store_true",
                    help="重试 checkpoint 中 failed 的作品（先自动备份 checkpoint，不影响 done 集合）")
    ap.add_argument("--only", type=int, metavar="WORK_ID", default=None,
                    help="只处理指定 work_id；若其在 failed 中则视为重试（同样先备份）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印处理计划，不执行、不写 checkpoint、不备份")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    state = load_checkpoint()

    items = list(SELECTION["selected"])
    if args.only is not None:
        items = [it for it in items if it["work_id"] == args.only]
        if not items:
            sys.exit(f"work {args.only} 不在 data/pipeline_selection.json 的 selected 列表中")

    # 显式重试集合：--retry-failed 取全部 failed；--only 命中 failed 时也算重试
    retry_ids: set[int] = set()
    if args.retry_failed:
        retry_ids.update(state["failed"])
    if args.only is not None and args.only in state["failed"]:
        retry_ids.add(args.only)

    if args.dry_run:
        n_run = 0
        for item in items:
            wid = item["work_id"]
            if wid in state["done"]:
                print(f"[dry-run][skip] work {wid} done ({item['title'][:20]})", flush=True)
            elif wid in state["failed"] and wid not in retry_ids:
                print(f"[dry-run][skip] work {wid} failed，--retry-failed 可重试 ({item['title'][:20]})", flush=True)
            else:
                tag = "retry" if wid in retry_ids else "run"
                n_run += 1
                print(f"[dry-run][{tag}] work {wid} {item.get('reason', '')} {item['title'][:40]}", flush=True)
        print(f"[dry-run] 计划处理 {n_run} 个，跳过 {len(items) - n_run} 个；未执行处理、未写 checkpoint", flush=True)
        return

    if retry_ids:
        bak = backup_checkpoint()
        print(f"[backup] checkpoint 已备份 -> {bak}", flush=True)
        state["failed"] = [w for w in state["failed"] if w not in retry_ids]

    init_db()
    for item in items:
        wid = item["work_id"]
        if wid in state["done"] or wid in state["failed"]:
            print(f"[skip] work {wid} already processed ({item['title'][:20]})", flush=True)
            continue
        print(f"[start] work {wid} {item['reason']} {item['title'][:40]}", flush=True)
        try:
            with SessionLocal() as db:
                work = db.get(jobs.Work, wid)
                if work is None:
                    raise RuntimeError("work not found")
                jobs._process_work(db, work)
                db.commit()
            if wid not in state["done"]:
                state["done"].append(wid)
            print(f"[ok] work {wid}", flush=True)
        except Exception as exc:  # noqa: BLE001
            if wid not in state["failed"]:
                state["failed"].append(wid)
            print(f"[fail] work {wid}: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
            traceback.print_exc()
        save_checkpoint(state)
    print(f"PIPELINE RUNNER DONE: ok={len(state['done'])} fail={len(state['failed'])}", flush=True)


if __name__ == "__main__":
    main()
