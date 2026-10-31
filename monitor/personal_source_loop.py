"""个人来源连续同步循环：只读列表元数据（不下载），直到全部范围完整。

由 ``personal_source_service.start_sync_loop`` 作为子进程拉起
（``python -m monitor.personal_source_loop --max-rounds N``）。每轮复用
``monitor.personal_source_ingest`` 的预算机制（900s 熔断），轮间休息。

停止条件（按序判定）：
1. 状态文件 ``stop_requested=true``（前端"停止"按钮）；
2. poll 状态 ok（like/collection/collections/folders 全部完整）；
3. 连续 3 轮 failed（避免对账号持续加压）；
4. 达到 max_rounds。

绝不携带 ``--download``：媒体下载由 ``douyin_personal_auto_download``
独立授权管理，连续爬取永远只做列表元数据。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

INGEST_TIMEOUT_SECONDS = 2100
def _round_gap_seconds() -> int:
    """轮间休息秒数：F2_ROUND_GAP_SECONDS 可调；风控事件后建议拉长。"""
    try:
        return max(0, int(os.getenv("F2_ROUND_GAP_SECONDS", "180")))
    except ValueError:
        return 180
MAX_CONSECUTIVE_FAILURES = 3
_TZ = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def _state_path(f2_dir: Path) -> Path:
    return f2_dir / "loop_state.json"


def read_state(f2_dir: Path) -> dict:
    try:
        return json.loads(_state_path(f2_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(f2_dir: Path, state: dict) -> None:
    f2_dir.mkdir(parents=True, exist_ok=True)
    tmp = _state_path(f2_dir).with_name("loop_state.json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, _state_path(f2_dir))


def _poll_summary(db_path: Path) -> dict:
    """最近一轮 poll 状态与作品总数（只读）。测试可注入替换。"""
    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        status, new_works = con.execute(
            "SELECT status, new_works FROM poll_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        total = con.execute(
            "SELECT COUNT(*) FROM works WHERE platform='douyin'"
        ).fetchone()[0]
    finally:
        con.close()
    return {"poll_status": status, "new_works": new_works or 0, "works_total": total}


def run_loop(
    *,
    max_rounds: int,
    f2_dir: Path,
    db_path: Path,
    python_exe: str,
    workspace: Path,
    log,
) -> dict:
    state = read_state(f2_dir)
    rounds_done = int(state.get("rounds_done") or 0)
    consecutive_failures = 0
    final_status = "max_rounds_reached"
    started_at = state.get("started_at") or _now()

    for round_no in range(1, max_rounds + 1):
        state = read_state(f2_dir)
        if state.get("stop_requested"):
            final_status = "stopped_by_user"
            log(f"round {round_no}: stop requested before start")
            break
        log(f"round {round_no}/{max_rounds}: ingest start {_now()}")
        try:
            proc = subprocess.run(  # noqa: S603 - 固定模块名，无用户可控片段
                [python_exe, "-m", "monitor.personal_source_ingest", "--source", "f2"],
                cwd=str(workspace),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                timeout=INGEST_TIMEOUT_SECONDS,
            )
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            exit_code = -1
        summary = _poll_summary(db_path)
        rounds_done = round_no
        log(
            f"round {round_no}: exit={exit_code} poll={summary['poll_status']} "
            f"new={summary['new_works']} total={summary['works_total']}"
        )
        state = read_state(f2_dir)
        state.update(
            {
                "running": True,
                "rounds_done": rounds_done,
                "last_round": {"round_no": round_no, "exit": exit_code, **summary, "at": _now()},
            }
        )
        write_state(f2_dir, state)

        # poll 状态实际值为 fail（ingest 写入），防御性同时接受 failed
        if summary["poll_status"] in ("fail", "failed"):
            consecutive_failures += 1
        else:
            consecutive_failures = 0
        if summary["poll_status"] in ("ok", "complete"):
            final_status = "all_complete"
            log("all scopes complete")
            break
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            final_status = "aborted_consecutive_failures"
            log("3 consecutive failed rounds, aborting")
            break
        if round_no < max_rounds:
            time.sleep(_round_gap_seconds())

    state = read_state(f2_dir)
    state.update(
        {
            "running": False,
            "finished_at": _now(),
            "rounds_done": rounds_done,
            "started_at": started_at,
            "final_status": final_status,
        }
    )
    write_state(f2_dir, state)
    log(f"loop end: {final_status} after {rounds_done} rounds")
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description="个人来源连续同步循环（只列表，不下载）")
    parser.add_argument("--max-rounds", type=int, default=60)
    args = parser.parse_args()

    from monitor.store.shared_db import settings

    f2_dir = settings.workbuddy_dir / "f2"
    f2_dir.mkdir(parents=True, exist_ok=True)
    log_path = f2_dir / "loop.log"

    def log(message: str) -> None:
        line = f"[{datetime.now(_TZ).isoformat(timespec='seconds')}] {message}"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    run_loop(
        max_rounds=max(1, min(int(args.max_rounds), 200)),
        f2_dir=f2_dir,
        db_path=settings.database_path,
        python_exe=sys.executable,
        workspace=settings.workspace_root,
        log=log,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
