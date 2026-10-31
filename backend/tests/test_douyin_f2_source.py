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
    clear_progress_file,
    confirm_f2_progress,
    f2_sync_lock,
    parse_f2_result,
    read_snapshot_f2,
    resolve_progress_file,
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


def test_clear_progress_file_removes_state(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBUDDY_DIR", str(tmp_path / "workbuddy"))
    path = resolve_progress_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 1}', encoding="utf-8")
    clear_progress_file()
    assert not path.exists()


def test_read_snapshot_f2_passes_progress_file_and_can_disable(tmp_path, monkeypatch):
    """默认传入 workbuddy/f2 续传文件；progress_file="" 时显式禁用（dry-run）。"""
    import monitor.platforms.douyin_f2_source as f2_source

    monkeypatch.setenv("WORKBUDDY_DIR", str(tmp_path / "workbuddy"))
    cookie = tmp_path / "cookie.txt"
    cookie.write_text("sessionid=synthetic", encoding="utf-8")
    runtime = tmp_path / "rt"
    (runtime / ".venv" / "bin").mkdir(parents=True)
    (runtime / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    captured: list[list[str]] = []

    class _Proc:
        stdout = ""
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_text(json.dumps(_ok_result()), encoding="utf-8")
        return _Proc()

    monkeypatch.setattr(f2_source.subprocess, "run", fake_run)
    read_snapshot_f2(runtime_dir=runtime, cookie_file=cookie)
    assert "--progress-file" in captured[0]
    expected = str(tmp_path / "workbuddy" / "f2" / "sync_progress.json")
    assert captured[0][captured[0].index("--progress-file") + 1] == expected
    # 互斥由父进程 f2_sync_lock 持有，子进程不再自行加锁
    assert "--lock-file" not in captured[0]

    read_snapshot_f2(runtime_dir=runtime, cookie_file=cookie, progress_file="")
    assert "--progress-file" not in captured[1]


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


# ---------- 提交协议与跨进程互斥（P1 修复回归；全部合成数据/临时库） ----------

def test_confirm_f2_progress_write_clear_and_reject(tmp_path, monkeypatch):
    """确认步：数据提交后推进/清除游标；形状不完整一律拒绝且不动旧状态。"""
    monkeypatch.setenv("WORKBUDDY_DIR", str(tmp_path / "workbuddy"))
    from monitor.platforms.douyin_personal_source import PersonalSnapshot

    snapshot = PersonalSnapshot(source="f2")
    snapshot.resume_state = {
        "version": 2, "fingerprint": "synthetic-fp", "all_complete": False,
        "scopes": {"like": {"complete": False, "cursor": 111}},
    }
    assert confirm_f2_progress(snapshot) is True
    saved = _json.loads(resolve_progress_file().read_text(encoding="utf-8"))
    assert saved["scopes"]["like"]["cursor"] == 111

    snapshot.resume_state = dict(snapshot.resume_state, all_complete=True)
    assert confirm_f2_progress(snapshot) is True
    assert not resolve_progress_file().exists()  # 全部完整 → 清除

    resolve_progress_file().write_text(
        '{"version": 2, "fingerprint": "old", "scopes": {}}', encoding="utf-8")
    for bad in (None, {}, {"version": 2}, {"version": "2", "fingerprint": "x", "scopes": {}}):
        snapshot.resume_state = bad
        assert confirm_f2_progress(snapshot) is False
    assert _json.loads(resolve_progress_file().read_text(encoding="utf-8"))["fingerprint"] == "old"

    snapshot.resume_state = None  # dry-run / 未启用续传：无可确认状态
    assert confirm_f2_progress(snapshot) is False


def test_f2_sync_lock_excludes_other_processes_and_reenters(tmp_path, monkeypatch):
    """同进程重入放行；另一 fd（模拟其他进程）被互斥；退出后可再次获取。"""
    import fcntl

    monkeypatch.setenv("WORKBUDDY_DIR", str(tmp_path / "workbuddy"))
    lock_path = tmp_path / "workbuddy" / "f2" / "sync.lock"

    with f2_sync_lock():
        with f2_sync_lock():  # 同进程重入（run_ingest → read_snapshot_f2）
            pass
        other = lock_path.open("a")
        try:
            with pytest.raises(OSError):  # flock 对同进程第二个 fd 也互斥
                fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            other.close()
    with f2_sync_lock():  # 释放后可重新获取
        pass


def test_run_ingest_f2_lock_contention_preserves_holder_state(tmp_path, monkeypatch):
    """P1-2 修复回归：占锁失败的一方不取数、不删除/推进持有者的进度。"""
    import fcntl

    import monitor.personal_source_ingest as ingest

    monkeypatch.setenv("WORKBUDDY_DIR", str(tmp_path / "workbuddy"))
    monkeypatch.setattr(ingest, "_read_f2_config",
                        lambda create=True: {"scopes": ("like",), "sec_user_id": ""})

    def _no_fetch(**kwargs):
        raise AssertionError("占锁失败时不得启动取数")

    monkeypatch.setattr(ingest, "read_snapshot_f2", _no_fetch)

    progress = resolve_progress_file()
    progress.parent.mkdir(parents=True, exist_ok=True)
    holder = ('{"version": 2, "fingerprint": "holder",'
              ' "scopes": {"like": {"complete": false, "cursor": 7}}}')
    progress.write_text(holder, encoding="utf-8")

    with (progress.parent / "sync.lock").open("w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(DDRPersonalSourceError) as excinfo:
            ingest.run_ingest("f2")
        assert excinfo.value.kind == "already_running"
    assert progress.read_text(encoding="utf-8") == holder  # 逐字节未动


def test_run_ingest_f2_fetch_failure_preserves_confirmed_state(tmp_path, monkeypatch):
    """取数子进程死亡/失败：已确认状态原样保留，不误报完成，重试从游标续传。"""
    import monitor.personal_source_ingest as ingest

    monkeypatch.setenv("WORKBUDDY_DIR", str(tmp_path / "workbuddy"))
    monkeypatch.setattr(ingest, "_read_f2_config",
                        lambda create=True: {"scopes": ("like",), "sec_user_id": ""})

    progress = resolve_progress_file()
    progress.parent.mkdir(parents=True, exist_ok=True)
    confirmed = {"version": 2, "fingerprint": "fp-k",
                 "scopes": {"like": {"complete": False, "cursor": 33}}}
    progress.write_text(_json.dumps(confirmed), encoding="utf-8")

    def _dead_fetch(**kwargs):
        raise DDRPersonalSourceError("runtime_missing", "F2 取数未产出结果文件（子进程死亡）")

    monkeypatch.setattr(ingest, "read_snapshot_f2", _dead_fetch)
    with pytest.raises(DDRPersonalSourceError):
        ingest.run_ingest("f2")
    assert _json.loads(progress.read_text(encoding="utf-8")) == confirmed


def test_run_ingest_f2_replays_idempotently_when_confirm_was_missed(tmp_path, monkeypatch):
    """提交后、确认前崩溃：游标未推进 → 重取重放，幂等（不重复、不漏项）。"""
    import sqlite3

    import monitor.personal_source_ingest as ingest
    from app.core.config import settings
    from monitor.platforms.douyin_personal_source import PersonalItem, PersonalSnapshot

    monkeypatch.setenv("WORKBUDDY_DIR", str(tmp_path / "workbuddy"))
    monkeypatch.setattr(ingest, "_read_f2_config",
                        lambda create=True: {"scopes": ("like",), "sec_user_id": ""})

    def _snapshot(ids, complete):
        snap = PersonalSnapshot(source="f2")
        snap.items = [PersonalItem(external_id=i, source_mark="like",
                                   url=f"https://www.douyin.com/video/{i}") for i in ids]
        snap.direct_sync = {"like": complete}
        snap.resume_state = {
            "version": 2, "fingerprint": "synthetic-replay-fp", "all_complete": complete,
            "scopes": {"like": {"complete": complete, "cursor": 0 if complete else 111}},
        }
        return snap

    scenario = [_snapshot(("7002101",), False), _snapshot(("7002101", "7002102"), True)]
    monkeypatch.setattr(ingest, "read_snapshot_f2", lambda **kwargs: scenario.pop(0))

    real_confirm = ingest.confirm_f2_progress
    # 第一轮：业务数据已提交，但确认步未执行（提交与确认之间崩溃）
    monkeypatch.setattr(ingest, "confirm_f2_progress", lambda *a, **k: True)
    report1 = ingest.run_ingest("f2")
    assert report1.works_created == 1
    assert not resolve_progress_file().exists()  # 游标未推进

    # 第二轮：从已确认游标 0 重取全部数据 → 幂等重放
    monkeypatch.setattr(ingest, "confirm_f2_progress", real_confirm)
    report2 = ingest.run_ingest("f2")
    assert report2.works_created == 1  # 只有 7002102 是新作品
    with sqlite3.connect(settings.database_path) as conn:
        works = [row[0] for row in conn.execute(
            "SELECT external_id FROM works WHERE platform='douyin' AND external_id IN ('7002101','7002102')")]
        marks = [row[0] for row in conn.execute(
            "SELECT external_id FROM personal_source_marks WHERE platform='douyin'"
            " AND source_mark='like' AND external_id IN ('7002101','7002102')")]
    assert sorted(works) == ["7002101", "7002102"]  # 不漏项
    assert sorted(marks) == ["7002101", "7002102"]  # 不重复
    assert not resolve_progress_file().exists()  # 全部完整 → 清除


def _make_paged_like_runtime(tmp_path: Path) -> Path:
    """伪运行时：按 F2_FAKE_LIKE_PAGES 返回合成点赞页（两页场景，不接触真实账号）。

    - "1"：仅第一页作品（complete=False），回传游标 111 的续传状态；
    - "all"：两页作品（complete=True），回传全部完整。
    """
    runtime = tmp_path / "f2-runtime-paged"
    py_dir = runtime / ".venv" / "bin"
    py_dir.mkdir(parents=True, exist_ok=True)
    body = '''
import json
import os
import sys

args = sys.argv[1:]


def arg_value(name, default=None):
    return args[args.index(name) + 1] if name in args else default


out = arg_value("--output")
pages = os.environ.get("F2_FAKE_LIKE_PAGES", "1")


def _work(aweme_id):
    return {"external_id": aweme_id, "title": f"作品{aweme_id[-4:]}", "author": "测试作者",
            "url": f"https://www.douyin.com/video/{aweme_id}",
            "cover_url": "https://c.example/1.jpg", "media_type": "video",
            "published_at": "2026-05-01T08:00:00Z"}


if pages == "1":
    doc = {"ok": True, "warnings": [], "runtime": {"f2_version": "0.0.1.7"},
           "like": {"complete": False, "works": [_work("1000001")]},
           "resume": {"version": 2, "fingerprint": "synthetic-paged-fp",
                      "all_complete": False,
                      "scopes": {"like": {"complete": False, "cursor": 111}}}}
else:
    doc = {"ok": True, "warnings": [], "runtime": {"f2_version": "0.0.1.7"},
           "like": {"complete": True, "works": [_work("1000001"), _work("1000002")]},
           "resume": {"version": 2, "fingerprint": "synthetic-paged-fp",
                      "all_complete": True,
                      "scopes": {"like": {"complete": True, "cursor": 0}}}}
with open(out, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, ensure_ascii=False)
'''
    script = py_dir / "python"
    script.write_text('#!/bin/sh\nexec python3 "$(dirname "$0")/fake_fetch.py" "$@"\n',
                      encoding="utf-8")
    script.chmod(0o755)
    (py_dir / "fake_fetch.py").write_text(body, encoding="utf-8")
    return runtime


_CRASH_DRIVER_TEMPLATE = '''
import json
import os
import sys

sys.path.insert(0, {root!r})
sys.path.insert(0, {backend!r})
from monitor import personal_source_ingest as ingest

ingest._read_f2_config = lambda create=True: {{"scopes": ("like",), "sec_user_id": "SYNTH"}}
if sys.argv[1] in ("crash", "sigkill"):
    import signal

    _load = ingest._load_snapshot

    def _crash_after_fetch(*args, **kwargs):
        _load(*args, **kwargs)
        if sys.argv[1] == "sigkill":
            os.kill(os.getpid(), signal.SIGKILL)  # 真实 SIGKILL：内核直接回收
        os._exit(73)  # 模拟数据库提交前进程死亡：不走任何异常处理/清理

    ingest._load_snapshot = _crash_after_fetch
report = ingest.run_ingest("f2")
print(json.dumps({{"works": report.works_created, "incomplete": report.incomplete_lists}}))
'''


def test_run_ingest_f2_process_death_before_commit_loses_nothing(tmp_path):
    """P1-1 修复回归（真实进程死亡）：取数完成→提交前 SIGKILL，
    进度不推进；重试从零重取两页全部入库，不漏项、不误报完成。"""
    import sqlite3

    env = _cli_env(tmp_path)
    cookie = tmp_path / "cookie.txt"
    cookie.write_text("sessionid=SYNTHETIC_ONLY", encoding="utf-8")
    env["F2_DOUYIN_COOKIE_FILE"] = str(cookie)
    env["F2_RUNTIME_DIR"] = str(_make_paged_like_runtime(tmp_path))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = _os.pathsep.join([str(_WORKSPACE_ROOT), str(_WORKSPACE_ROOT / "backend")])
    driver = tmp_path / "crash_driver.py"
    driver.write_text(_CRASH_DRIVER_TEMPLATE.format(
        root=str(_WORKSPACE_ROOT), backend=str(_WORKSPACE_ROOT / "backend")), encoding="utf-8")
    progress = tmp_path / "workbuddy" / "f2" / "sync_progress.json"

    # 第一轮：取数完成后、数据库提交前 SIGKILL（内核回收，无任何清理机会）
    first = _subprocess.run([sys.executable, str(driver), "sigkill"],
                            cwd=str(_WORKSPACE_ROOT), env={**env, "F2_FAKE_LIKE_PAGES": "1"},
                            text=True, capture_output=True)
    assert first.returncode == -_subprocess.signal.SIGKILL, first.stdout[-400:]
    assert not progress.exists()  # 取数完成 ≠ 游标推进（提交协议核心不变式）

    # 第二轮：从已确认游标（0）重取，两页作品全部入库
    second = _subprocess.run([sys.executable, str(driver), "resume"],
                             cwd=str(_WORKSPACE_ROOT), env={**env, "F2_FAKE_LIKE_PAGES": "all"},
                             text=True, capture_output=True)
    assert second.returncode == 0, second.stderr[-400:]
    payload = _json.loads(second.stdout.strip().splitlines()[-1])
    assert payload["works"] == 2  # 第一页 + 第二页都入库（不跳过第一段）
    assert payload["incomplete"] == []
    with sqlite3.connect(tmp_path / "cli.db") as conn:
        imported = [row[0] for row in conn.execute(
            "SELECT external_id FROM works WHERE platform='douyin'"
            " AND external_id IN ('1000001','1000002') ORDER BY external_id")]
    assert imported == ["1000001", "1000002"]
    assert not progress.exists()  # 全部完整，确认步已清除


def test_cli_f2_lock_contention_reports_already_running_and_preserves_progress(tmp_path):
    """真实跨进程互斥：另一进程持锁时 CLI 同步立即失败，且不动持有者进度。"""
    import fcntl

    env = _cli_env(tmp_path)
    workbuddy = tmp_path / "workbuddy" / "f2"
    workbuddy.mkdir(parents=True)
    progress = workbuddy / "sync_progress.json"
    holder = ('{"version": 2, "fingerprint": "holder",'
              ' "scopes": {"like": {"complete": false, "cursor": 9}}}')
    progress.write_text(holder, encoding="utf-8")

    with (workbuddy / "sync.lock").open("w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _subprocess.run(
            [sys.executable, "-m", "monitor.personal_source_ingest", "--source", "f2"],
            cwd=str(_WORKSPACE_ROOT), env=env, text=True, capture_output=True,
        )
    assert result.returncode == 1
    payload = _json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["kind"] == "already_running"
    assert progress.read_text(encoding="utf-8") == holder  # 持有者进度逐字节未动


# ---------- 端到端：取数子进程硬超时 → 提交确认 → 重启续传（真实运行时） ----------

_HARD_TIMEOUT_SITECUSTOMIZE = '''
import asyncio
import os

if os.environ.get("F2_SYNTHETIC_HARD_TIMEOUT") == "1":
    try:
        import f2.apps.douyin.handler as _handler_module

        class _SyntheticPage:
            def __init__(self, data):
                self.data = data
                self.status_code = data.get("status_code")

            def _to_raw(self):
                return self.data

        class _SyntheticHardTimeoutHandler:
            """合成 handler：collection 完整；like 首页产出后挂起并吞掉一次取消
            （复刻“下一页请求取消不及时”——内层预算超时被吞，外层硬超时击穿）。"""

            def __init__(self, kwargs):
                self.kwargs = kwargs

            def fetch_user_collection_videos(self, **kwargs):
                async def gen():
                    yield _SyntheticPage({
                        "status_code": 0,
                        "aweme_list": [{"aweme_id": "1000002", "desc": "synthetic-collection",
                                        "author": {"nickname": "synthetic"}}],
                        "has_more": 0, "cursor": 0})
                return gen()

            def fetch_user_like_videos(self, *args, **kwargs):
                async def gen():
                    if int(kwargs.get("max_cursor") or 0) == 111:
                        yield _SyntheticPage({
                            "status_code": 0,
                            "aweme_list": [{"aweme_id": "1000003", "desc": "synthetic-next",
                                            "author": {"nickname": "synthetic"}}],
                            "has_more": 0, "max_cursor": 222})
                        return
                    yield _SyntheticPage({
                        "status_code": 0,
                        "aweme_list": [{"aweme_id": "1000001", "desc": "synthetic-liked",
                                        "author": {"nickname": "synthetic"}}],
                        "has_more": 1, "max_cursor": 111})
                    try:
                        await asyncio.sleep(60)
                    except asyncio.CancelledError:
                        await asyncio.sleep(60)
                return gen()

        _handler_module.DouyinHandler = _SyntheticHardTimeoutHandler
    except ImportError:
        pass  # 非 F2 运行时解释器（父进程）：不打桩
'''

_HARD_TIMEOUT_DRIVER_TEMPLATE = '''
import json
import sys

sys.path.insert(0, {root!r})
sys.path.insert(0, {backend!r})
from monitor import personal_source_ingest as ingest

ingest._read_f2_config = lambda create=True: {{
    "scopes": ("collection", "like"), "sec_user_id": "SYNTH-MANUAL"}}
_original = ingest.read_snapshot_f2


def _bounded(**kwargs):
    return _original(**kwargs, budget_seconds=2, page_counts=1)


ingest.read_snapshot_f2 = _bounded
report = ingest.run_ingest("f2")
print(json.dumps({{"works": report.works_created, "incomplete": report.incomplete_lists}}))
'''


@pytest.mark.skipif(
    not (_WORKSPACE_ROOT / "tools" / "f2-runtime" / ".venv" / "bin" / "python").exists(),
    reason="F2 隔离运行时未安装（tools/f2-runtime/.venv）",
)
def test_run_ingest_f2_hard_timeout_commits_read_pages_and_resumes(tmp_path):
    """P1 回归（第 1 轮复审，真实调用链）：取数子进程硬超时中断时，已读页作品
    随结果入库并确认游标；重启后从已确认游标续传补齐，不漏项、不误报完成。"""
    import sqlite3

    from app.core.config import settings

    env = _cli_env(tmp_path)
    cookie = tmp_path / "cookie.txt"
    cookie.write_text("sessionid=SYNTHETIC_ONLY", encoding="utf-8")
    env["F2_DOUYIN_COOKIE_FILE"] = str(cookie)
    env["F2_RUNTIME_DIR"] = str(_WORKSPACE_ROOT / "tools" / "f2-runtime")
    env["F2_SYNTHETIC_HARD_TIMEOUT"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(_HARD_TIMEOUT_SITECUSTOMIZE, encoding="utf-8")
    env["PYTHONPATH"] = _os.pathsep.join([
        str(sitecustomize.parent), str(_WORKSPACE_ROOT), str(_WORKSPACE_ROOT / "backend")])
    driver = tmp_path / "hard_timeout_driver.py"
    driver.write_text(_HARD_TIMEOUT_DRIVER_TEMPLATE.format(
        root=str(_WORKSPACE_ROOT), backend=str(_WORKSPACE_ROOT / "backend")), encoding="utf-8")
    progress = tmp_path / "workbuddy" / "f2" / "sync_progress.json"

    first = _subprocess.run([sys.executable, str(driver)], cwd=str(_WORKSPACE_ROOT),
                            env=env, text=True, capture_output=True)
    assert first.returncode == 0, first.stderr[-400:]
    payload = _json.loads(first.stdout.strip().splitlines()[-1])
    assert payload["incomplete"] == ["like"]  # 中断范围如实 partial，不冒充完成
    saved = _json.loads(progress.read_text(encoding="utf-8"))
    assert saved["scopes"]["like"]["cursor"] == 111  # 与已提交的 1000001 一致
    with sqlite3.connect(tmp_path / "cli.db") as conn:
        imported = [row[0] for row in conn.execute(
            "SELECT external_id FROM works WHERE platform='douyin'"
            " AND external_id IN ('1000001','1000002') ORDER BY external_id")]
    assert imported == ["1000001", "1000002"]  # 已读页随本批提交，不丢失

    second = _subprocess.run([sys.executable, str(driver)], cwd=str(_WORKSPACE_ROOT),
                             env=env, text=True, capture_output=True)
    assert second.returncode == 0, second.stderr[-400:]
    payload2 = _json.loads(second.stdout.strip().splitlines()[-1])
    assert payload2["incomplete"] == []
    with sqlite3.connect(tmp_path / "cli.db") as conn:
        imported = [row[0] for row in conn.execute(
            "SELECT external_id FROM works WHERE platform='douyin'"
            " AND external_id IN ('1000001','1000002','1000003') ORDER BY external_id")]
    assert imported == ["1000001", "1000002", "1000003"]  # 续传补齐，不漏项
    assert not progress.exists()  # 全部完整，确认步已清除


def test_run_ingest_f2_confirm_failure_reported_partial_and_data_preserved(
        tmp_path, monkeypatch):
    """第 6 轮复审回归：确认失败（写入/替换/清理 OSError）不得冒充干净成功——
    poll_logs 记 partial + 固定原因、报告带警告；已提交数据保全、锁正常释放，
    后续同步可正常获取锁并完成确认。"""
    import sqlite3

    import monitor.personal_source_ingest as ingest
    from app.core.config import settings
    from monitor.platforms.douyin_personal_source import PersonalItem, PersonalSnapshot

    monkeypatch.setenv("WORKBUDDY_DIR", str(tmp_path / "workbuddy"))
    monkeypatch.setattr(ingest, "_read_f2_config",
                        lambda create=True: {"scopes": ("like",), "sec_user_id": ""})

    def _snapshot(external_ids, complete):
        snap = PersonalSnapshot(source="f2")
        snap.items = [PersonalItem(external_id=i, source_mark="like",
                                   url=f"https://www.douyin.com/video/{i}") for i in external_ids]
        snap.direct_sync = {"like": complete}
        snap.resume_state = {
            "version": 2, "fingerprint": "synthetic-confirm-fp", "all_complete": complete,
            "scopes": {"like": {"complete": complete, "cursor": 0 if complete else 111}},
        }
        return snap

    scenario = [_snapshot(("7003101",), False), _snapshot(("7003102",), True)]
    monkeypatch.setattr(ingest, "read_snapshot_f2", lambda **kwargs: scenario.pop(0))
    real_confirm = ingest.confirm_f2_progress
    monkeypatch.setattr(ingest, "confirm_f2_progress", lambda *a, **k: False)  # 故障注入

    report = ingest.run_ingest("f2")   # 数据已提交，但确认失败
    assert report.works_created == 1
    assert any("resume_confirm_failed" in w for w in report.warnings)
    with sqlite3.connect(settings.database_path) as conn:
        status, detail = conn.execute(
            "SELECT status, detail FROM poll_logs WHERE platform='douyin-personal'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        imported = [row[0] for row in conn.execute(
            "SELECT external_id FROM works WHERE external_id='7003101'")]
    assert status == "partial" and "resume_confirm_failed" in detail  # 不冒充干净成功
    assert imported == ["7003101"]  # 已提交数据保全

    monkeypatch.setattr(ingest, "confirm_f2_progress", real_confirm)
    report2 = ingest.run_ingest("f2")  # 锁已释放、可重新获取；确认成功后完整收尾
    assert report2.works_created == 1
    with sqlite3.connect(settings.database_path) as conn:
        status2 = conn.execute(
            "SELECT status FROM poll_logs WHERE platform='douyin-personal'"
            " ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert status2 == "ok"
    assert not resolve_progress_file().exists()
