"""F2 续传协议缺陷修复回归：resume 回执三分支解析 + ingest 确认步守卫。

被测语义规格（与实现对齐）：

parse_f2_result（scopes=("like",) 最小合成文档：ok=True + like=dict 且 works=list）：
- ``resume`` 为 dict（含空对象 {}）→ ``snapshot.resume_state`` 为该 dict 且
  ``snapshot.resume_anomaly is None``（{} 的形状校验交给确认步写校验，不在 parse 层拦截）。
- ``resume`` 键缺失且 ``result.get("resume_disabled") is True`` → 合法禁用：
  ``resume_state is None`` 且 ``resume_anomaly is None``。
- ``resume`` 键缺失且无禁用标记 → ``resume_state is None`` 且
  ``resume_anomaly == "missing"``。
- ``resume`` 键存在但非 dict（null / 数组 / 字符串 / 数字）→
  ``resume_state is None`` 且 ``resume_anomaly == "invalid_type"``。

run_ingest("f2") 实时路径（非 dry-run，取数在 f2_sync_lock 内）：
- 快照 ``resume_anomaly`` 非空 → 完全不调用 confirm_f2_progress（调用数 0）；
  poll log 记 "partial"，文案以 ``resume_missing_or_invalid:`` 开头并含括号里
  的 anomaly 值；旧续传状态文件原样保留（逐字节未动）；数据已提交保全。
- ``resume_state`` 为正常 dict → confirm_f2_progress 被调用恰好一次（返回 True）；
  干净同步 poll log 记 "ok"（detail=None）。
- ``resume_state`` 为 {} → confirm_f2_progress 被调用一次，返回 False 模拟写
  校验失败 → poll log "partial" 且文案含 ``resume_confirm_failed``，旧状态保全
  （既有行为回归保护）。
- ``resume_disabled=True``（未配置进度文件的合法禁用）→ 实时路径同样不确认、
  不报异常，poll log 记 "ok"。
- dry_run=True 的 f2 预演 → 完全不触碰 confirm_f2_progress、不写任何 poll log
  （无 partial 因续传产生）、取数禁用续传（progress_file=""）、不创建配置库。

测试手法：合成结果文档现场经 parse_f2_result 构造快照（整链覆盖 parse 语义），
monkeypatch ``read_snapshot_f2`` / ``confirm_f2_progress`` / ``_record_sync_poll_log``
/ ``f2_sync_lock`` 注入；WORKBUDDY_DIR 指向 tmp_path 隔离进度与锁文件；
数据库仅用 conftest 的临时 SQLite。全部离线，不访问网络、不读真实账号数据。
"""
from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

import pytest

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

import monitor.personal_source_ingest as ingest  # noqa: E402
from monitor.platforms.douyin_f2_source import (  # noqa: E402
    parse_f2_result,
    resolve_progress_file,
)

# resume 键缺失的哨兵（与"显式传 None"区分开：None 属于 invalid_type 形态）
_MISSING = object()


def _work(aweme_id: str, **overrides) -> dict:
    base = {
        "external_id": aweme_id,
        "title": f"作品{aweme_id[-4:]}",
        "author": "测试作者",
        "url": f"https://www.douyin.com/video/{aweme_id}",
        "media_type": "video",
        "published_at": "2026-05-01T08:00:00Z",
    }
    base.update(overrides)
    return base


def _like_result(aweme_id: str = "9200001", resume=_MISSING, **extra) -> dict:
    """最小合法合成结果文档：scopes=("like",) 只要求 like=dict 且 works=list。"""
    doc: dict = {
        "ok": True,
        "warnings": [],
        "like": {"complete": True, "works": [_work(aweme_id)]},
    }
    if resume is not _MISSING:
        doc["resume"] = resume
    doc.update(extra)
    return doc


def _snapshot(resume=_MISSING, **kwargs):
    """合成结果文档 → parse_f2_result 现场解析（整链，不绕过 parse 层）。"""
    return parse_f2_result(_like_result(resume=resume, **kwargs), scopes=("like",))


def _seed_progress() -> tuple[Path, str]:
    """预写旧续传状态文件，返回（路径, 原始文本）供"原样保留"断言。

    需先由 _install_f2_runtime 把 WORKBUDDY_DIR 指向 tmp_path。
    """
    progress = resolve_progress_file()
    progress.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(
        {"version": 2, "fingerprint": "old-confirmed-fp",
         "scopes": {"like": {"complete": False, "cursor": 7}}},
        ensure_ascii=False,
    )
    progress.write_text(raw, encoding="utf-8")
    return progress, raw


def _install_f2_runtime(monkeypatch, tmp_path, snapshot, *, confirm_results=(True,)):
    """隔离并替换 f2 取数/锁/确认/poll log，返回 (fetch_calls, poll_calls, confirm_calls)。

    - read_snapshot_f2 → 固定返回合成快照（快照已现场 parse，整链覆盖 parse 语义）；
    - f2_sync_lock → 无操作上下文（真锁互斥语义已由 test_douyin_f2_source 覆盖）；
    - confirm_f2_progress → 记录收到的快照并按 confirm_results 依次返回（默认 True）；
    - _record_sync_poll_log → 记录 (status, detail, new_works)，不落库。
    """
    monkeypatch.setenv("WORKBUDDY_DIR", str(tmp_path / "workbuddy"))
    monkeypatch.setattr(ingest, "_read_f2_config",
                        lambda create=True: {"scopes": ("like",), "sec_user_id": ""})

    fetch_calls: list[dict] = []

    def _fake_fetch(**kwargs):
        fetch_calls.append(kwargs)
        return snapshot

    monkeypatch.setattr(ingest, "read_snapshot_f2", _fake_fetch)

    @contextlib.contextmanager
    def _fake_lock():
        yield

    monkeypatch.setattr(ingest, "f2_sync_lock", _fake_lock)

    results = list(confirm_results)
    confirm_calls: list = []

    def _fake_confirm(snap, **kwargs):
        confirm_calls.append(snap)
        return results.pop(0) if results else True

    monkeypatch.setattr(ingest, "confirm_f2_progress", _fake_confirm)

    poll_calls: list[dict] = []

    def _fake_poll_log(status, detail, *, started_at, new_works=0):
        poll_calls.append({"status": status, "detail": detail, "new_works": new_works})

    monkeypatch.setattr(ingest, "_record_sync_poll_log", _fake_poll_log)
    return fetch_calls, poll_calls, confirm_calls


# ---------- parse 层：resume 回执三分支 ----------


@pytest.mark.parametrize("resume_value", [
    {"version": 2, "fingerprint": "synthetic-fp", "all_complete": False,
     "scopes": {"like": {"complete": False, "cursor": 111}}},
    {},
], ids=["normal_dict", "empty_dict"])
def test_parse_f2_resume_dict_keeps_state_without_anomaly(resume_value):
    """resume 为 dict（含空对象 {}）→ 原样进入 resume_state，无异常标记。"""
    snapshot = parse_f2_result(_like_result(resume=resume_value), scopes=("like",))
    assert snapshot.resume_state == resume_value
    assert snapshot.resume_anomaly is None


def test_parse_f2_resume_disabled_marker_is_legal():
    """resume 键缺失但 resume_disabled=True → 合法禁用：两者皆 None，不在 parse 层拦截。"""
    doc = _like_result()
    doc["resume_disabled"] = True
    snapshot = parse_f2_result(doc, scopes=("like",))
    assert snapshot.resume_state is None
    assert snapshot.resume_anomaly is None


def test_parse_f2_resume_missing_without_marker_flags_missing():
    """resume 应有而缺失且未声明禁用 → missing 异常（绝不静默跳过确认）。"""
    snapshot = parse_f2_result(_like_result(), scopes=("like",))
    assert snapshot.resume_state is None
    assert snapshot.resume_anomaly == "missing"


@pytest.mark.parametrize("resume_value", [None, [1, 2], "abc", 123],
                         ids=["null", "array", "string", "number"])
def test_parse_f2_resume_wrong_type_flags_invalid_type(resume_value):
    """resume 键存在但非 dict（null/数组/字符串/数字）→ invalid_type 异常。"""
    snapshot = parse_f2_result(_like_result(resume=resume_value), scopes=("like",))
    assert snapshot.resume_state is None
    assert snapshot.resume_anomaly == "invalid_type"


# ---------- run_ingest("f2") 实时路径：确认步守卫 ----------


@pytest.mark.parametrize("resume_value,aweme_id", [
    (_MISSING, "9201001"),
    (None, "9201002"),
    ([1, 2], "9201003"),
], ids=["missing", "null", "array"])
def test_run_ingest_f2_resume_anomaly_skips_confirm_and_keeps_old_progress(
        tmp_path, monkeypatch, resume_value, aweme_id):
    """续传回执异常：不确认、如实 partial 上报、旧进度逐字保留、数据已提交保全。"""
    snapshot = _snapshot(resume=resume_value, aweme_id=aweme_id)
    assert snapshot.resume_anomaly in ("missing", "invalid_type")
    _, poll_calls, confirm_calls = _install_f2_runtime(monkeypatch, tmp_path, snapshot)
    progress, raw = _seed_progress()

    report = ingest.run_ingest("f2")

    assert confirm_calls == []  # 异常回执绝不触发确认步（旧游标不能被碰）
    assert report.works_created == 1  # 数据已提交保全，下一轮从旧游标幂等重放
    assert report.warnings and report.warnings[0].startswith("resume_missing_or_invalid:")
    assert len(poll_calls) == 1
    entry = poll_calls[0]
    assert entry["status"] == "partial"
    assert entry["detail"] is not None
    assert entry["detail"].startswith("resume_missing_or_invalid:")
    assert f"（{snapshot.resume_anomaly}）" in entry["detail"]  # 括号内含 anomaly 值
    assert progress.read_text(encoding="utf-8") == raw  # 旧续传状态文件逐字节未动


def test_run_ingest_f2_normal_resume_confirms_once_and_logs_ok(tmp_path, monkeypatch):
    """正常 dict 回执：业务提交后 confirm_f2_progress 恰好一次，干净同步记 ok。"""
    snapshot = _snapshot(resume={
        "version": 2, "fingerprint": "synthetic-ok-fp", "all_complete": True,
        "scopes": {"like": {"complete": True, "cursor": 0}},
    }, aweme_id="9210001")
    _, poll_calls, confirm_calls = _install_f2_runtime(monkeypatch, tmp_path, snapshot)

    report = ingest.run_ingest("f2")

    assert confirm_calls == [snapshot]  # 恰好一次，且传入同一快照
    assert report.warnings == []
    assert len(poll_calls) == 1
    assert poll_calls[0]["status"] == "ok"
    assert poll_calls[0]["detail"] is None
    assert poll_calls[0]["new_works"] == 1


def test_run_ingest_f2_empty_resume_confirm_failure_logs_partial(tmp_path, monkeypatch):
    """{} 空对象回执：仍走确认步；写校验失败 → partial + resume_confirm_failed（既有行为回归）。"""
    snapshot = _snapshot(resume={}, aweme_id="9220001")
    _, poll_calls, confirm_calls = _install_f2_runtime(
        monkeypatch, tmp_path, snapshot, confirm_results=[False])
    progress, raw = _seed_progress()

    report = ingest.run_ingest("f2")

    assert confirm_calls == [snapshot]  # 空对象不在 parse 层拦截，交给确认步形状校验
    assert confirm_calls[0].resume_state == {}
    assert report.warnings and report.warnings[0].startswith("resume_confirm_failed:")
    assert len(poll_calls) == 1
    assert poll_calls[0]["status"] == "partial"
    assert "resume_confirm_failed" in (poll_calls[0]["detail"] or "")
    assert progress.read_text(encoding="utf-8") == raw  # 写校验失败不动旧状态


def test_run_ingest_f2_resume_disabled_marker_syncs_clean_without_confirm(
        tmp_path, monkeypatch):
    """resume_disabled=True（未配置进度文件的合法禁用）：实时路径无需确认，直接 ok。"""
    doc = _like_result(aweme_id="9230001")
    doc["resume_disabled"] = True
    snapshot = parse_f2_result(doc, scopes=("like",))
    assert snapshot.resume_state is None and snapshot.resume_anomaly is None
    _, poll_calls, confirm_calls = _install_f2_runtime(monkeypatch, tmp_path, snapshot)

    report = ingest.run_ingest("f2")

    assert confirm_calls == []  # 无可确认状态：合法跳过，不算确认失败
    assert report.warnings == []
    assert len(poll_calls) == 1
    assert poll_calls[0]["status"] == "ok"
    assert poll_calls[0]["detail"] is None


def test_run_ingest_f2_dry_run_never_confirms_logs_or_touches_storage(
        tmp_path, monkeypatch):
    """f2 预演：禁用续传取数、不确认、不写 poll log、不创建配置库。"""
    from app.core.config import settings

    snapshot = _snapshot(resume={
        "version": 2, "fingerprint": "synthetic-dry-fp", "all_complete": False,
        "scopes": {"like": {"complete": False, "cursor": 5}},
    }, aweme_id="9240001")
    fetch_calls, poll_calls, confirm_calls = _install_f2_runtime(
        monkeypatch, tmp_path, snapshot)
    dry_db = tmp_path / "dry-target" / "aggregator.db"
    monkeypatch.setattr(settings, "database_path", dry_db)

    report = ingest.run_ingest("f2", dry_run=True)

    assert report.dry_run is True
    assert fetch_calls and fetch_calls[0]["progress_file"] == ""  # 预演显式禁用续传取数
    assert confirm_calls == []  # 预演不推进/清除任何游标
    assert poll_calls == []  # 预演不产生任何 poll log（无 partial 因续传产生）
    assert not dry_db.exists()  # 预演不创建配置库
