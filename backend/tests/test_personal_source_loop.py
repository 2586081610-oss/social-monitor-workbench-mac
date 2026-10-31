"""个人来源连续同步循环测试：API 启动/停止/状态守卫与 runner 停止语义（离线）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from monitor import personal_source_loop as loop_mod  # noqa: E402

@pytest.fixture(autouse=True)
def _no_real_subprocess(monkeypatch):
    """循环测试绝不真拉起 ingest 子进程（离线、快、可断言）。"""

    class _FakeProc:
        returncode = 0

    monkeypatch.setattr(loop_mod.subprocess, "run", lambda *a, **k: _FakeProc())


def _write_state(f2_dir: Path, **fields) -> None:
    loop_mod.write_state(f2_dir, {"running": True, "pid": None, **fields})


def _poll_factory(statuses: list[str]):
    """按调用顺序吐 poll 状态；用于替身 _poll_summary。"""
    seq = list(statuses)

    def _summary(db_path: Path) -> dict:
        status = seq.pop(0) if seq else seq[-1] if seq else "partial"
        return {"poll_status": status, "new_works": 0, "works_total": 0}

    return _summary


def test_run_loop_stops_on_ok(tmp_path, monkeypatch):
    """poll 状态 ok（全部范围完整）→ 立即结束且标记 all_complete。"""
    monkeypatch.setattr(loop_mod, "_poll_summary", _poll_factory(["ok"]))
    monkeypatch.setattr(loop_mod.time, "sleep", lambda s: None)
    f2 = tmp_path / "f2"
    state = loop_mod.run_loop(
        max_rounds=10, f2_dir=f2, db_path=tmp_path / "db.sqlite",
        python_exe="python", workspace=tmp_path, log=lambda m: None,
    )
    assert state["running"] is False
    assert state["final_status"] == "all_complete"
    assert state["rounds_done"] == 1


def test_run_loop_stops_on_three_consecutive_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "_poll_summary", _poll_factory(["failed", "failed", "failed"]))
    monkeypatch.setattr(loop_mod.time, "sleep", lambda s: None)
    state = loop_mod.run_loop(
        max_rounds=10, f2_dir=tmp_path / "f2", db_path=tmp_path / "db.sqlite",
        python_exe="python", workspace=tmp_path, log=lambda m: None,
    )
    assert state["final_status"] == "aborted_consecutive_failures"
    assert state["rounds_done"] == 3


def test_run_loop_respects_user_stop_between_rounds(tmp_path, monkeypatch):
    """用户停止请求在轮间生效：不开启新一轮，标记 stopped_by_user。"""
    monkeypatch.setattr(loop_mod, "_poll_summary", _poll_factory(["partial", "partial"]))

    original_sleep = loop_mod.time.sleep

    def fake_sleep(seconds: int) -> None:
        # 第一轮结束的休息期写入停止请求（模拟前端点"停止"）
        _write_state(tmp_path / "f2", stop_requested=True, running=True)
        original_sleep(0)

    monkeypatch.setattr(loop_mod.time, "sleep", fake_sleep)
    state = loop_mod.run_loop(
        max_rounds=10, f2_dir=tmp_path / "f2", db_path=tmp_path / "db.sqlite",
        python_exe="python", workspace=tmp_path, log=lambda m: None,
    )
    assert state["final_status"] == "stopped_by_user"
    assert state["rounds_done"] == 1


def test_run_loop_partial_keeps_going_until_max_rounds(tmp_path, monkeypatch):
    """partial（预算到期）不是失败也不是完成：继续轮进，直到 max_rounds。"""
    calls = {"n": 0}

    def summary(db_path: Path) -> dict:
        calls["n"] += 1
        return {"poll_status": "partial", "new_works": 100, "works_total": calls["n"] * 100}

    monkeypatch.setattr(loop_mod, "_poll_summary", summary)
    monkeypatch.setattr(loop_mod.time, "sleep", lambda s: None)
    state = loop_mod.run_loop(
        max_rounds=4, f2_dir=tmp_path / "f2", db_path=tmp_path / "db.sqlite",
        python_exe="python", workspace=tmp_path, log=lambda m: None,
    )
    assert state["final_status"] == "max_rounds_reached"
    assert state["rounds_done"] == 4


def test_runner_never_carries_download_flag():
    """连续循环的 ingest 命令绝不携带 --download（只爬列表是硬约束）。"""
    import inspect

    source = inspect.getsource(loop_mod.run_loop)
    assert "--download" not in source
    assert "--source" in source and '"f2"' in source
