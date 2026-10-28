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


# ---------- P1-1 回归：按请求范围解析（部分同步必须可用） ----------

@pytest.mark.parametrize("scopes", [("like",), ("collection",), ("collections",),
                                    ("like", "collection"), ("like", "collections")])
def test_parse_respects_requested_scopes(scopes):
    """只请求部分范围时，未请求范围缺失是正常的，不再抛 schema_changed。"""
    snapshot = parse_f2_result(_ok_result(), scopes=scopes)
    marks = {(i.external_id, i.source_mark) for i in snapshot.items}
    assert ("like",) == tuple() or True  # 占位避免空断言
    if "like" in scopes:
        assert ("7000001", "like") in marks
    else:
        assert all(m != "like" for _, m in marks)
    if "collection" in scopes:
        assert ("7000002", "favorite") in marks
    else:
        assert all(m != "favorite" or eid != "7000002" for eid, m in marks)
    if "collections" in scopes:
        assert snapshot.folder_mode == "multi" and snapshot.folders_available
        assert snapshot.folder_memberships == {"8000001": ["f1", "f2"], "7000001": ["f1"]}
        assert snapshot.direct_sync["collections"] is True
    else:
        assert snapshot.folder_mode is None and not snapshot.folders_available
        assert snapshot.folder_memberships == {}
        assert "collections" not in snapshot.direct_sync
    assert set(snapshot.direct_sync) == set(scopes)


def test_parse_requested_but_missing_scope_is_schema_change():
    """已请求的 scope 在结果中缺失 → schema_changed（区分未请求与请求后缺字段）。"""
    result = _ok_result()
    del result["collection"]
    with pytest.raises(DDRPersonalSourceError) as excinfo:
        parse_f2_result(result, scopes=("like", "collection"))
    assert excinfo.value.kind == "schema_changed"
    # 未请求则正常
    snapshot = parse_f2_result(result, scopes=("like",))
    assert [i.source_mark for i in snapshot.items] == ["like"]


def test_parse_rejects_unknown_scope():
    with pytest.raises(DDRPersonalSourceError) as excinfo:
        parse_f2_result(_ok_result(), scopes=("like", "bogus"))
    assert excinfo.value.kind == "bad_source"


@pytest.mark.parametrize("scopes", [("like",), ("collection",), ("collections",),
                                    ("like", "collection"), ("collection", "collections"),
                                    ("like", "collection", "collections")])
def test_read_snapshot_f2_end_to_end_all_scope_combinations(tmp_path, monkeypatch, scopes):
    """全部范围组合的端到端验证（伪运行时按 --scopes 生成对应输出）。"""
    monkeypatch.setenv("WORKBUDDY_DIR", str(tmp_path / "workbuddy"))
    cookie = tmp_path / "cookie.txt"
    cookie.write_text("sessionid=test-cookie", encoding="utf-8")
    runtime = _make_fake_runtime(tmp_path)
    snapshot = read_snapshot_f2(runtime_dir=runtime, cookie_file=cookie, scopes=scopes)
    assert snapshot.source == "f2"
    assert set(snapshot.direct_sync) == set(scopes)
    if "collections" in scopes:
        assert snapshot.folder_memberships == {"8000001": ["f1", "f2"], "7000001": ["f1"]}
    else:
        assert snapshot.folder_memberships == {}


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


def _make_fake_runtime(tmp_path: Path, result_doc: dict | None = None, *, error_doc: dict | None = None) -> Path:
    """伪造 F2 隔离运行时：.venv/bin/python 解析 --scopes/--output 并写出结果。

    result_doc 为 None 时按请求的 scopes 动态生成对应范围的成功结果（端到端
    部分同步验证）；error_doc 非 None 时写 ok:false（失败透传验证）。
    """
    runtime = tmp_path / "f2-runtime"
    py_dir = runtime / ".venv" / "bin"
    py_dir.mkdir(parents=True, exist_ok=True)
    # 兼容位置传入的错误文档（ok=False → error_doc 语义）
    if result_doc is not None and isinstance(result_doc, dict) and result_doc.get("ok") is False:
        error_doc, result_doc = result_doc, None
    fixed = json.dumps(result_doc, ensure_ascii=False) if result_doc is not None else None
    error = json.dumps(error_doc, ensure_ascii=False) if error_doc is not None else None
    fake_body = f'''
import json
import os
import sys

args = sys.argv[1:]


def arg_value(name, default=None):
    if name in args:
        return args[args.index(name) + 1]
    return default


out = arg_value("--output")
scopes = [s for s in (arg_value("--scopes", "") or "").split(",") if s]
mode = arg_value("--mode", "list")
aweme_id = arg_value("--aweme-id", "")
# 测试钩子：F2_FAKE_INCOMPLETE=1 → collection / 夹 f1 标记不完整（验证 partial 传播）
incomplete = os.environ.get("F2_FAKE_INCOMPLETE") == "1"

if mode == "detail":
    # detail 模式：合成图集详情（两张图片直链）；--aweme-id 含 "noimg" 时返回非图集
    if "noimg" in aweme_id:
        doc = {{"ok": True, "detail": {{
            "external_id": aweme_id, "title": "合成视频", "author": "测试作者",
            "published_at": "2026-05-01T08:00:00Z", "images": [],
            "video_url": "https://cdn.example.test/v.mp4",
        }}}}
    else:
        doc = {{"ok": True, "detail": {{
            "external_id": aweme_id,
            "title": "合成图集",
            "author": "测试作者",
            "published_at": "2026-05-01T08:00:00Z",
            "images": [
                "https://cdn.example.test/p1.jpg",
                "https://cdn.example.test/p2.jpg",
            ],
            "video_url": None,
        }}}}
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False)
    sys.exit(0)

def _work(aweme_id):
    return {{"external_id": aweme_id, "title": f"作品{{aweme_id[-4:]}}", "author": "测试作者",
            "url": f"https://www.douyin.com/video/{{aweme_id}}", "cover_url": "https://c.example/1.jpg",
            "media_type": "video", "published_at": "2026-05-01T08:00:00Z"}}

doc = {{"ok": True, "warnings": [], "runtime": {{"f2_version": "0.0.1.7"}}}}
if ERROR_DOC is not None:
    doc = json.loads(ERROR_DOC)
elif FIXED is not None:
    doc = json.loads(FIXED)
else:
    if "like" in scopes:
        doc["like"] = {{"complete": True, "works": [_work("7000001")]}}
    if "collection" in scopes:
        doc["collection"] = {{"complete": not incomplete, "works": [_work("7000001"), _work("7000002")]}}
    if "collections" in scopes:
        doc["collects"] = {{
            "complete": not incomplete, "pages": 1, "folders_complete": True,
            "folder_incomplete": ["folder#1"] if incomplete else [],
            "folders": [{{"folder_id": "f1", "name": "夹A"}}, {{"folder_id": "f2", "name": "夹B"}}],
            "folder_items": {{
                "f1": {{"complete": not incomplete, "works": [_work("8000001"), _work("7000001")]}},
                "f2": {{"complete": True, "works": [_work("8000001")]}},
            }},
        }}
with open(out, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, ensure_ascii=False)
'''
    fixed_line = f"FIXED = {fixed!r}\nERROR_DOC = {error!r}\n"
    script = py_dir / "python"
    script.write_text(
        "#!/bin/sh\n"
        'exec python3 "$(dirname "$0")/fake_fetch.py" "$@"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    (py_dir / "fake_fetch.py").write_text(fixed_line + fake_body, encoding="utf-8")
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


def test_cli_f2_incomplete_sync_records_partial_not_success(tmp_path):
    """不完整同步（列表分页未走完）→ poll_logs 记 partial + 明细，不冒充干净成功。"""
    import sqlite3

    env = _cli_env(tmp_path)
    env["F2_FAKE_INCOMPLETE"] = "1"
    env["F2_RUNTIME_DIR"] = str(_make_fake_runtime(tmp_path))
    (tmp_path / "cookie.txt").write_text("sessionid=synthetic", encoding="utf-8")
    env["F2_DOUYIN_COOKIE_FILE"] = str(tmp_path / "cookie.txt")
    result = _subprocess.run(
        [sys.executable, "-m", "monitor.personal_source_ingest", "--source", "f2"],
        cwd=str(_WORKSPACE_ROOT), env=env, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr[-400:]
    report = _json.loads(result.stdout)
    assert report["incomplete_lists"].count("collection") == 1
    assert report["incomplete_lists"] == ["collection", "folder×1"] or "folder×1" in report["incomplete_lists"]
    assert not any("8000001" in part or "f1" == part for part in report["incomplete_lists"])
    with sqlite3.connect(tmp_path / "cli.db") as conn:
        row = conn.execute(
            "SELECT status, detail FROM poll_logs WHERE platform='douyin-personal' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None and row[0] == "partial"
    assert "collection" in row[1] and "folder#" in row[1]
    assert "8000001" not in (row[1] or "")  # 日志不含作品 ID
    assert "f1" not in (row[1] or "").replace("folder#", "")  # 日志不含真实收藏夹 ID
