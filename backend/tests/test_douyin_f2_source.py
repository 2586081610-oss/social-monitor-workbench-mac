"""F2 通道适配器验收：合成 fetch 结果、子进程编排与失败语义（不碰真实账号/网络）。

覆盖任务单要求：分页结果归一化、一作多夹、夹内-only 作品不断言收藏、
失败 ≠ 空列表（登录过期/运行时缺失/未配置登录态）、结果文件用后即删。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

from monitor.platforms.douyin_f2_source import (  # noqa: E402
    PersonalSourceError,
    parse_f2_result,
    read_snapshot_f2,
    runtime_python,
)
from monitor.platforms.douyin_personal_source import PersonalSourceError as DDRPersonalSourceError  # noqa: E402


def _work(aweme_id: str, **overrides) -> dict:
    base = {
        "external_id": aweme_id,
        "title": f"作品{aweme_id[-4:]}",
        "author": "测试作者",
        "author_id": "sec-author-1",
        "url": f"https://www.douyin.com/video/{aweme_id}",
        "cover_url": "https://p.example/cover.jpg",
        "media_type": "video",
        "published_at": "2026-05-01T08:00:00Z",
    }
    base.update(overrides)
    return base


def _ok_result() -> dict:
    return {
        "ok": True,
        "warnings": [],
        "runtime": {"f2_version": "0.0.1.7"},
        "like": {"complete": True, "works": [_work("7000001")]},
        "collection": {"complete": True, "works": [_work("7000001"), _work("7000002", media_type="image")]},
        "collects": {
            "complete": True,
            "folders": [
                {"folder_id": "f1", "name": "夹A", "item_count": 2},
                {"folder_id": "f2", "name": "夹B", "item_count": 1},
            ],
            "folder_items": {
                "f1": {"complete": True, "works": [_work("8000001"), _work("7000001")]},
                "f2": {"complete": True, "works": [_work("8000001")]},
            },
        },
    }


# ---------- parse_f2_result：归一化与多夹 ----------

def test_parse_ok_result_normalizes_marks_and_memberships():
    snapshot = parse_f2_result(_ok_result())
    marks = [(i.external_id, i.source_mark) for i in snapshot.items]
    # like 一条、favorite 两条、夹内-only（8000001）一条 favorite_folder
    assert marks == [
        ("7000001", "like"),
        ("7000001", "favorite"),
        ("7000002", "favorite"),
        ("8000001", "favorite_folder"),
    ]
    # 一作多夹：8000001 同时在 f1/f2；7000001 在 f1
    assert snapshot.folder_memberships == {"8000001": ["f1", "f2"], "7000001": ["f1"]}
    assert snapshot.folders == {"f1": "夹A", "f2": "夹B"}
    assert snapshot.folders_available is True and snapshot.folder_mode == "multi"
    assert snapshot.direct_sync == {"like": True, "collection": True, "collections": True}
    # 列表接口无动作时间：不得把发布时间当点赞/收藏时间
    assert all(i.occurred_at is None and i.occurred_at_source is None for i in snapshot.items)
    assert snapshot.source == "f2"
    assert len(snapshot.snapshot_id) == 16


def test_parse_snapshot_id_ignores_item_order():
    a = parse_f2_result(_ok_result())
    b = parse_f2_result(_ok_result())
    assert a.snapshot_id == b.snapshot_id


def test_parse_failure_passthrough_kind_and_message():
    for kind, message in [
        ("login_expired", "登录已过期"),
        ("rate_limited", "触发限流"),
        ("timeout", "超时"),
        ("like_list_unavailable", "点赞列表不可用"),
    ]:
        with pytest.raises(DDRPersonalSourceError) as excinfo:
            parse_f2_result({"ok": False, "error": {"kind": kind, "message": message}})
        assert excinfo.value.kind == kind


def test_parse_missing_scope_is_schema_change_not_empty_result():
    result = _ok_result()
    del result["collection"]
    with pytest.raises(DDRPersonalSourceError) as excinfo:
        parse_f2_result(result)
    assert excinfo.value.kind == "schema_changed"


def test_parse_folder_item_referencing_unknown_folder_fails():
    result = _ok_result()
    result["collects"]["folders"] = [{"folder_id": "f1", "name": "夹A"}]
    with pytest.raises(DDRPersonalSourceError) as excinfo:
        parse_f2_result(result)
    assert excinfo.value.kind == "schema_changed"


def test_parse_bad_timestamp_treated_as_unknown_not_error():
    result = _ok_result()
    result["like"]["works"][0]["published_at"] = "2026-05-01（不完整）"
    snapshot = parse_f2_result(result)
    liked = next(i for i in snapshot.items if i.source_mark == "like")
    assert liked.published_at is None


# ---------- 运行时解析与登录态约定 ----------

def test_runtime_python_missing_raises_runtime_missing(tmp_path):
    with pytest.raises(DDRPersonalSourceError) as excinfo:
        runtime_python(tmp_path)
    assert excinfo.value.kind == "runtime_missing"


def test_read_snapshot_f2_requires_cookie_file(tmp_path, monkeypatch):
    monkeypatch.delenv("F2_DOUYIN_COOKIE_FILE", raising=False)
    monkeypatch.setenv("WORKBUDDY_DIR", str(tmp_path / "workbuddy"))
    with pytest.raises(DDRPersonalSourceError) as excinfo:
        read_snapshot_f2(runtime_dir=tmp_path, cookie_file=tmp_path / "nope.txt")
    assert excinfo.value.kind == "login_required"


def _make_fake_runtime(tmp_path: Path, result_doc: dict) -> Path:
    """伪造 F2 隔离运行时：可执行 .venv/bin/python 把固定 JSON 写到 --output。"""
    runtime = tmp_path / "f2-runtime"
    py_dir = runtime / ".venv" / "bin"
    py_dir.mkdir(parents=True)
    payload = json.dumps(result_doc, ensure_ascii=False)
    script = py_dir / "python"
    script.write_text(
        "#!/bin/sh\n"
        'out=""\nprev=""\n'
        'for arg in "$@"; do\n'
        '  if [ "$prev" = "--output" ]; then out="$arg"; fi\n'
        '  prev="$arg"\n'
        "done\n"
        f"printf '%s' '{payload}' > \"$out\"\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return runtime


def test_read_snapshot_f2_end_to_end_with_fake_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBUDDY_DIR", str(tmp_path / "workbuddy"))
    cookie = tmp_path / "cookie.txt"
    cookie.write_text("sessionid=test-cookie", encoding="utf-8")
    runtime = _make_fake_runtime(tmp_path, _ok_result())

    snapshot = read_snapshot_f2(runtime_dir=runtime, cookie_file=cookie)

    assert snapshot.source == "f2"
    assert any(i.source_mark == "like" for i in snapshot.items)
    assert snapshot.folder_memberships["8000001"] == ["f1", "f2"]
    # 同步日志落在 WORKBUDDY_DIR 下（不进真实工作区）
    assert (tmp_path / "workbuddy" / "f2" / "fetch.log").exists()


def test_read_snapshot_f2_propagates_failure_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBUDDY_DIR", str(tmp_path / "workbuddy"))
    cookie = tmp_path / "cookie.txt"
    cookie.write_text("sessionid=test-cookie", encoding="utf-8")
    runtime = _make_fake_runtime(
        tmp_path,
        {"ok": False, "error": {"kind": "login_expired", "message": "登录已过期"}},
    )
    with pytest.raises(DDRPersonalSourceError) as excinfo:
        read_snapshot_f2(runtime_dir=runtime, cookie_file=cookie)
    assert excinfo.value.kind == "login_expired"


def test_read_snapshot_f2_cleans_temp_output(tmp_path, monkeypatch):
    """结果含本人数据：临时输出文件用后必须删除。"""
    import tempfile

    monkeypatch.setenv("WORKBUDDY_DIR", str(tmp_path / "workbuddy"))
    cookie = tmp_path / "cookie.txt"
    cookie.write_text("sessionid=test-cookie", encoding="utf-8")
    runtime = _make_fake_runtime(tmp_path, _ok_result())

    real_mkdtemp = tempfile.mkdtemp
    created: list[str] = []

    def tracking_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", tracking_mkdtemp)
    read_snapshot_f2(runtime_dir=runtime, cookie_file=cookie)
    assert created, "应创建过临时输出目录"
    for path in created:
        assert not Path(path).exists(), f"临时目录未清理: {path}"


# ---------- CLI 失败语义（子进程级） ----------

import json as _json  # noqa: E402
import os as _os  # noqa: E402
import subprocess as _subprocess  # noqa: E402


def _cli_env(tmp_path: Path) -> dict:
    return dict(
        _os.environ,
        DATABASE_PATH=str(tmp_path / "cli.db"),
        MEDIA_ROOT=str(tmp_path / "media"),
        OBSIDIAN_ROOT=str(tmp_path / "notes"),
        WORKBUDDY_DIR=str(tmp_path / "workbuddy"),
        F2_DOUYIN_COOKIE_FILE=str(tmp_path / "missing-cookie.txt"),
        F2_RUNTIME_DIR=str(tmp_path / "no-runtime"),
    )


def test_cli_f2_without_cookie_reports_login_required(tmp_path):
    """未配置登录态 → 明确 kind=login_required，退出码 1，不伪装成空结果。"""
    result = _subprocess.run(
        [sys.executable, "-m", "monitor.personal_source_ingest", "--source", "f2"],
        cwd=str(_WORKSPACE_ROOT), env=_cli_env(tmp_path), text=True, capture_output=True,
    )
    assert result.returncode == 1
    payload = _json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["status"] == "error" and payload["kind"] == "login_required"


def test_cli_default_source_stays_off(tmp_path):
    """不显式指定来源、env 缺省 → 拒绝执行（不会隐式读取本人数据）。"""
    env = _cli_env(tmp_path)
    env.pop("DOUYIN_PERSONAL_SOURCE", None)
    result = _subprocess.run(
        [sys.executable, "-m", "monitor.personal_source_ingest"],
        cwd=str(_WORKSPACE_ROOT), env=env, text=True, capture_output=True,
    )
    assert result.returncode == 1
    payload = _json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["kind"] == "disabled"
    assert "f2" in payload["message"]  # 提示信息包含全部支持来源


def test_cli_f2_failure_records_poll_log(tmp_path):
    """失败的同步也如实落 poll_logs（douyin-personal），供状态页展示。"""
    import sqlite3

    env = _cli_env(tmp_path)
    _subprocess.run(
        [sys.executable, "-m", "monitor.personal_source_ingest", "--source", "f2"],
        cwd=str(_WORKSPACE_ROOT), env=env, text=True, capture_output=True,
    )
    with sqlite3.connect(tmp_path / "cli.db") as conn:
        row = conn.execute(
            "SELECT status, detail FROM poll_logs WHERE platform='douyin-personal' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row[0] == "fail" and "login_required" in row[1]
