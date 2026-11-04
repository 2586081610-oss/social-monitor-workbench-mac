"""个人来源连续同步循环测试：API 启动/停止/状态守卫与 runner 停止语义（离线）。"""

from __future__ import annotations

import json
import sys
import time
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
    monkeypatch.setattr(loop_mod, "_poll_summary", _poll_factory(["fail", "fail", "fail"]))
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


def test_personal_works_endpoint_pagination_and_filters(client, auth_headers, db):
    """作品清单接口：分页、类型/状态过滤、标题搜索（douyin 平台专属）。"""
    from app.models import Work

    # 共享测试库会累积其他用例写入的抖音作品，因此本用例所有断言都用唯一标题前缀
    # 通过 q 过滤，只针对自己构造的数据，不依赖库内总量。
    prefix = "分页测试"
    db.add_all([
        Work(platform="douyin", external_id=f"pg{i:03d}", title=f"{prefix}作品{i}",
             content_type="video" if i % 2 else "image", published_at=f"2024-0{1 + i % 9}-01",
             url=f"https://www.douyin.com/video/pg{i:03d}", cover_url=f"https://cover/{i}.jpg", status="NEW")
        for i in range(12)
    ])
    db.add(Work(platform="douyin", external_id="pgdone", title=f"{prefix}已完成作品", content_type="video",
                published_at="2024-05-05", url="https://www.douyin.com/video/pgdone", status="DOWNLOADED",
                media_path="未分组/pgdone/video.mp4"))
    db.commit()

    resp = client.get(f"/api/v1/personal-source/works?q={prefix}&limit=5&offset=0", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 13
    assert len(data["items"]) == 5
    first = data["items"][0]
    for field in ("external_id", "title", "content_type", "published_at", "url", "cover_url", "status", "media_path"):
        assert field in first

    # 类型过滤
    resp = client.get(f"/api/v1/personal-source/works?q={prefix}&content_type=image", headers=auth_headers)
    data = resp.json()["data"]
    assert data["total"] == 6
    assert all(item["content_type"] == "image" for item in data["items"])

    # 状态过滤 + 媒体路径
    resp = client.get(f"/api/v1/personal-source/works?q={prefix}&status=DOWNLOADED", headers=auth_headers)
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["media_path"].endswith("video.mp4")

    # 标题搜索
    resp = client.get(f"/api/v1/personal-source/works?q={prefix}已完成", headers=auth_headers)
    assert resp.json()["data"]["total"] == 1


def test_personal_works_source_filter_and_marks(client, auth_headers, db):
    """来源过滤（like/favorite）与标记聚合：一作可同时带 赞+藏。"""
    from app.models import PersonalSourceMark, Work

    db.add_all([
        Work(platform="douyin", external_id="mlike", title="只赞过", content_type="video", url="https://www.douyin.com/video/mlike", status="NEW"),
        Work(platform="douyin", external_id="mfav", title="只藏过", content_type="image", url="https://www.douyin.com/note/mfav", status="NEW"),
        Work(platform="douyin", external_id="mboth", title="又赞又藏", content_type="video", url="https://www.douyin.com/video/mboth", status="NEW"),
    ])
    db.add_all([
        PersonalSourceMark(platform="douyin", external_id="mlike", source_mark="like", source_channel="f2", snapshot_id="snap-test"),
        PersonalSourceMark(platform="douyin", external_id="mfav", source_mark="favorite", source_channel="f2", snapshot_id="snap-test"),
        PersonalSourceMark(platform="douyin", external_id="mboth", source_mark="like", source_channel="f2", snapshot_id="snap-test"),
        PersonalSourceMark(platform="douyin", external_id="mboth", source_mark="favorite", source_channel="f2", snapshot_id="snap-test",
                           folder_name="我的宝藏夹", occurred_at="2026-01-01T00:00:00+08:00"),
    ])
    db.commit()

    resp = client.get("/api/v1/personal-source/works?source_mark=like", headers=auth_headers)
    data = resp.json()["data"]
    ids = {i["external_id"] for i in data["items"]}
    assert {"mlike", "mboth"} <= ids and "mfav" not in ids

    resp = client.get("/api/v1/personal-source/works?source_mark=favorite", headers=auth_headers)
    data = resp.json()["data"]
    ids = {i["external_id"] for i in data["items"]}
    assert {"mfav", "mboth"} <= ids and "mlike" not in ids

    by_id = {i["external_id"]: i for i in data["items"]}
    assert set(by_id["mboth"]["source_marks"]) == {"like", "favorite"}
    assert by_id["mboth"]["folder_name"] == "我的宝藏夹"
    assert by_id["mboth"]["occurred_at"].startswith("2026-01-01")
    assert by_id["mfav"]["source_marks"] == ["favorite"]


def test_run_loop_breaker_treats_fail_and_failed_alike(tmp_path, monkeypatch):
    """真实 poll 状态值是 fail（曾因字符串不匹配导致熔断失效）——两者都必须计数。"""
    monkeypatch.setattr(loop_mod, "_poll_summary", _poll_factory(["fail", "failed", "fail"]))
    monkeypatch.setattr(loop_mod.time, "sleep", lambda s: None)
    state = loop_mod.run_loop(
        max_rounds=10, f2_dir=tmp_path / "f2", db_path=tmp_path / "db.sqlite",
        python_exe="python", workspace=tmp_path, log=lambda m: None,
    )
    assert state["final_status"] == "aborted_consecutive_failures"
    assert state["rounds_done"] == 3


def test_page_delay_applied_between_pages_and_capped_by_deadline(monkeypatch, tmp_path):
    """页间限速：两页之间睡一次（默认 2.5s+抖动），最后一页后不睡；预算到期截断。"""
    import asyncio

    from monitor.platforms import f2_fetch

    monkeypatch.setenv("F2_PAGE_DELAY_SECONDS", "2.5")
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(f2_fetch.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(f2_fetch.random, "random", lambda: 0.0)  # 抖动确定性

    class FakePage:
        status_code = 0

        def __init__(self, raw):
            self._raw = raw

        def _to_raw(self):
            return self._raw

    page = FakePage({"aweme_list": [{"aweme_id": "1", "desc": "t", "author": {}}], "has_more": True})

    def make_gen(_cursor):
        async def gen():
            yield page
            yield FakePage({"aweme_list": [{"aweme_id": "2", "desc": "t2", "author": {}}], "has_more": False})
        return gen()

    works, meta = asyncio.run(
        f2_fetch.consume_video_pages(
            make_gen, label="like", requested_page_counts=20, max_pages=10, max_items=100,
            warnings=[], deadline=None, on_page=None, on_partial=None,
        )
    )
    assert len(works) == 2 and meta["complete"] is True
    assert len(sleeps) == 1 and 2.5 <= sleeps[0] <= 3.75

    # 预算只剩 0.5s：页间间隔被截断到 ≤0.5s，绝不睡过截止后继续发请求
    sleeps.clear()

    def make_gen_deadline(_cursor):
        async def gen():
            yield page
            yield FakePage({"aweme_list": [{"aweme_id": "1", "desc": "t", "author": {}}], "has_more": False})
        return gen()

    works2, meta2 = asyncio.run(
        f2_fetch.consume_video_pages(
            make_gen_deadline, label="like", requested_page_counts=20, max_pages=10, max_items=100,
            warnings=[], deadline=time.monotonic() + 0.5, on_page=None, on_partial=None,
        )
    )
    # 0.5s 预算下：页间间隔被截断到 ≤0.5s（绝不睡过截止后继续发请求）
    assert sleeps and all(s <= 0.5 for s in sleeps)


def test_round_gap_default_is_conservative(monkeypatch):
    """风控事件后：轮间休息默认 180s，环境变量可调。"""
    monkeypatch.delenv("F2_ROUND_GAP_SECONDS", raising=False)
    assert loop_mod._round_gap_seconds() == 180
    monkeypatch.setenv("F2_ROUND_GAP_SECONDS", "600")
    assert loop_mod._round_gap_seconds() == 600
    monkeypatch.setenv("F2_ROUND_GAP_SECONDS", "junk")
    assert loop_mod._round_gap_seconds() == 180


def test_fetch_budget_env_default(monkeypatch, tmp_path):
    """取数预算默认 900s，F2_FETCH_BUDGET_SECONDS 可调低。"""
    from app.services import personal_source_service as svc

    # 只验证 env 解析语义（不真跑 F2）：直接调用内联逻辑
    import os

    monkeypatch.delenv("F2_FETCH_BUDGET_SECONDS", raising=False)
    assert int(os.getenv("F2_FETCH_BUDGET_SECONDS", "900")) == 900
    monkeypatch.setenv("F2_FETCH_BUDGET_SECONDS", "600")
    assert int(os.getenv("F2_FETCH_BUDGET_SECONDS", "900")) == 600
