"""抖音本人来源 F2 适配器：以子进程调用隔离运行时中的 f2_fetch.py。

通道 C（F2）：读取本人抖音点赞、平铺收藏、平台收藏夹及夹内作品。
F2 固定安装在工作区 `tools/f2-runtime/.venv`（版本见该目录 UPSTREAM_SOURCE.json），
本模块不 import f2，可在主环境独立测试。

硬约定（与 DDR 适配器一致）：
- 失败 ≠ 空列表：运行时缺失、登录过期、限流、超时都抛 PersonalSourceError(kind)。
- 列表接口不提供点赞/收藏动作时间：occurred_at 一律未知，不把作品发布时间
  当作点赞/收藏时间。
- 收藏夹是平台侧多夹语义（一作可多夹）：夹内归属写入独立的 folder_memberships，
  与 DDR 的单夹语义（marks.folder_id）分开，不互相覆盖。
- 登录态经环境变量 F2_DOUYIN_COOKIE_FILE 指向的文本文件提供（用户自行登录导出），
  本模块只传递路径，不读取、不打印内容。
- 本模块不 import app.*，保持可独立测试。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from monitor.platforms.douyin_personal_source import (
    PersonalItem,
    PersonalSnapshot,
    PersonalSourceError,
    _clean_str,
    _valid_timestamp,
)

# 相对工作区根的默认路径；F2_RUNTIME_DIR / F2_DOUYIN_COOKIE_FILE 可覆盖
_DEFAULT_RUNTIME_DIR = Path(__file__).resolve().parents[2] / "tools" / "f2-runtime"
_DEFAULT_COOKIE_FILE = Path(__file__).resolve().parents[2] / "config" / "f2" / "douyin_cookie.txt"
RUNTIME_DIR_ENV = "F2_RUNTIME_DIR"
COOKIE_FILE_ENV = "F2_DOUYIN_COOKIE_FILE"
# 点赞 / 平铺收藏 / 收藏夹（含夹内作品）
SCOPES_ALL = ("like", "collection", "collections")
_DEFAULT_SCOPES = ",".join(SCOPES_ALL)
LOG_RELATIVE_DIR = "workbuddy/f2"


def resolve_runtime_dir(base_dir: str | os.PathLike | None = None) -> Path:
    raw = base_dir or os.getenv(RUNTIME_DIR_ENV) or _DEFAULT_RUNTIME_DIR
    return Path(raw).expanduser()


def resolve_cookie_file(cookie_file: str | os.PathLike | None = None) -> Path:
    raw = cookie_file or os.getenv(COOKIE_FILE_ENV) or _DEFAULT_COOKIE_FILE
    return Path(raw).expanduser()


def runtime_python(runtime_dir: Path) -> Path:
    """隔离运行时的解释器；Windows 也在 .venv/Scripts/python.exe（存在才返回）。"""
    for candidate in (runtime_dir / ".venv" / "bin" / "python", runtime_dir / ".venv" / "Scripts" / "python.exe"):
        if candidate.exists():
            return candidate
    raise PersonalSourceError(
        "runtime_missing",
        f"F2 隔离运行时缺失：{runtime_dir}（见 tools/f2-runtime/README.md 安装说明）",
    )


def _merge_folder_memberships(result: dict) -> dict[str, list[str]]:
    """夹内作品 → external_id → [folder_id...]（一作多夹，顺序稳定）。"""
    memberships: dict[str, list[str]] = {}
    for folder_id, block in (result.get("collects", {}).get("folder_items") or {}).items():
        for work in block.get("works") or []:
            memberships.setdefault(work["external_id"], [])
            if folder_id not in memberships[work["external_id"]]:
                memberships[work["external_id"]].append(folder_id)
    return memberships


def _snapshot_id(items: list[PersonalItem], memberships: dict[str, list[str]]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            [
                [i.external_id, i.source_mark, i.published_at or ""]
                for i in sorted(items, key=lambda x: (x.source_mark, x.external_id))
            ]
            + sorted([k, sorted(v)] for k, v in memberships.items()),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return digest[:16]


def _item_from_work(work: dict, mark: str) -> PersonalItem:
    external_id = _clean_str(work.get("external_id")) or ""
    published = _clean_str(work.get("published_at"))
    if published and not _valid_timestamp(published):
        published = None
    media_type = _clean_str(work.get("media_type"))
    return PersonalItem(
        external_id=external_id,
        source_mark=mark,
        title=_clean_str(work.get("title")),
        author=_clean_str(work.get("author")),
        author_id=_clean_str(work.get("author_id")),
        url=_clean_str(work.get("url")) or f"https://www.douyin.com/video/{external_id}",
        cover_url=_clean_str(work.get("cover_url")),
        media_type=media_type if media_type in {"video", "image"} else None,
        published_at=published,
        occurred_at=None,  # 列表接口不含点赞/收藏动作时间
        occurred_at_source=None,
    )


def parse_f2_result(result: dict, *, source_path: str | None = None) -> PersonalSnapshot:
    """把 f2_fetch.py 的输出文档归一化为 PersonalSnapshot。

    结果文档 ok=False 时抛 PersonalSourceError；scope 缺失/形状不对按失败处理。

    夹内作品以 source_mark="favorite_folder" 进入 items：仅用于作品登记（使夹内
    新作品可被发现与下载），不写 personal_source_marks——平台收藏夹归属是独立的
    事实（folder_memberships），不能据此断言"平铺收藏"状态。
    """
    if not isinstance(result, dict):
        raise PersonalSourceError("invalid_json", "F2 取数结果不是 JSON 对象")
    if result.get("ok") is not True:
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        raise PersonalSourceError(
            _clean_str(error.get("kind")) or "platform_error",
            _clean_str(error.get("message")) or "F2 取数失败（未提供原因）",
        )

    snapshot = PersonalSnapshot(source="f2", source_path=source_path)
    snapshot.warnings = [w for w in result.get("warnings") or [] if isinstance(w, str)]
    snapshot.folders_available = True
    snapshot.folder_mode = "multi"

    known_ids: set[str] = set()
    for scope, mark in (("like", "like"), ("collection", "favorite")):
        if scope not in result:
            raise PersonalSourceError("schema_changed", f"F2 结果缺少 {scope} 列表")
        block = result[scope]
        if not isinstance(block, dict):
            raise PersonalSourceError("schema_changed", f"F2 结果 {scope} 不是对象")
        works = block.get("works")
        if not isinstance(works, list):
            raise PersonalSourceError("schema_changed", f"F2 结果 {scope}.works 不是列表")
        for work in works:
            item = _item_from_work(work, mark)
            if item.external_id:
                snapshot.items.append(item)
                known_ids.add(item.external_id)
            else:
                snapshot.skipped_invalid.append({"list": scope, "reason": "missing_identity"})
        snapshot.direct_sync[scope] = bool(block.get("complete"))

    collects = result.get("collects")
    if not isinstance(collects, dict):
        raise PersonalSourceError("schema_changed", "F2 结果缺少 collects 列表")
    folder_rows = collects.get("folders")
    folder_items = collects.get("folder_items")
    if not isinstance(folder_rows, list) or not isinstance(folder_items, dict):
        raise PersonalSourceError("schema_changed", "F2 结果 collects.folders/folder_items 形状不正确")
    for row in folder_rows:
        if not isinstance(row, dict) or not _clean_str(row.get("folder_id")):
            raise PersonalSourceError("schema_changed", "F2 收藏夹记录缺少 folder_id")
        snapshot.folders[str(row["folder_id"])] = _clean_str(row.get("name")) or ""
    for folder_id in folder_items:
        if folder_id not in snapshot.folders:
            raise PersonalSourceError("schema_changed", f"F2 夹内作品引用了未知收藏夹: {folder_id}")
    snapshot.direct_sync["collections"] = bool(collects.get("complete"))

    for block in folder_items.values():
        for work in block.get("works") or []:
            item = _item_from_work(work, "favorite_folder")
            if not item.external_id:
                snapshot.skipped_invalid.append({"list": "folder_items", "reason": "missing_identity"})
            elif item.external_id not in known_ids:
                # 已在点赞/平铺收藏中出现过的作品不再重复登记
                snapshot.items.append(item)
                known_ids.add(item.external_id)
    snapshot.folder_memberships = _merge_folder_memberships(result)
    snapshot.skipped_invalid.extend(
        s for s in (result.get("skipped") or []) if isinstance(s, dict)
    )
    snapshot.snapshot_id = _snapshot_id(snapshot.items, snapshot.folder_memberships)
    return snapshot


def read_snapshot_f2(
    *,
    runtime_dir: str | os.PathLike | None = None,
    cookie_file: str | os.PathLike | None = None,
    scopes: tuple[str, ...] = SCOPES_ALL,
    sec_user_id: str = "",
    folder_ids: tuple[str, ...] = (),
    budget_seconds: int = 900,
    page_counts: int = 20,
) -> PersonalSnapshot:
    """通道 C：运行 F2 取数脚本并解析。入口由 personal_source_ingest 调用。"""
    runtime = resolve_runtime_dir(runtime_dir)
    python = runtime_python(runtime)
    script = Path(__file__).resolve().parent / "f2_fetch.py"
    cookie_path = resolve_cookie_file(cookie_file)
    if not cookie_path.is_file():
        raise PersonalSourceError(
            "login_required",
            f"未找到抖音登录态文件: {cookie_path}（设置 {COOKIE_FILE_ENV} 或先由用户完成登录导出）",
        )

    workspace_root = Path(__file__).resolve().parents[2]
    log_dir = workspace_root / LOG_RELATIVE_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "fetch.log"
    lock_path = log_dir / "sync.lock"

    output_path = Path(tempfile.mkdtemp(prefix="f2-fetch-")) / "result.json"
    cmd = [
        str(python),
        str(script),
        "--cookie-file", str(cookie_path),
        "--output", str(output_path),
        "--scopes", ",".join(scopes),
        "--budget-seconds", str(budget_seconds),
        "--page-counts", str(page_counts),
        "--lock-file", str(lock_path),
    ]
    if sec_user_id:
        cmd += ["--sec-user-id", sec_user_id]
    if folder_ids:
        cmd += ["--folder-ids", ",".join(folder_ids)]

    try:
        with open(log_path, "w", encoding="utf-8") as log_fh:
            proc = subprocess.run(
                cmd,
                cwd=str(workspace_root),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                timeout=budget_seconds + 120,
            )
        if not output_path.is_file():
            raise PersonalSourceError("runtime_missing", f"F2 取数未产出结果文件（日志: {log_path}）")
        try:
            result = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise PersonalSourceError("invalid_json", f"F2 取数结果不是有效 JSON（日志: {log_path}）") from None
        snapshot = parse_f2_result(result, source_path=str(runtime))
    except subprocess.TimeoutExpired:
        raise PersonalSourceError("timeout", f"F2 取数超出时间预算 {budget_seconds}s") from None
    finally:
        # 结果含本人作品数据：用完即删，不留在临时目录
        try:
            output_path.unlink(missing_ok=True)
            output_path.parent.rmdir()
        except OSError:
            pass
    return snapshot
