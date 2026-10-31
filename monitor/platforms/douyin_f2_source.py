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
- 提交协议（防漏数据/防误报完成）：父进程用 f2_sync_lock 覆盖“续传状态读取 →
  取数子进程 → 数据库提交 → confirm_f2_progress 游标确认”完整生命周期；取数
  子进程对进度文件只读，确认步把回传状态在业务数据持久提交后原子落盘/清除。
  任何失败（含占锁失败）都不删除、不推进任何续传状态。
- 本模块不 import f2、不 import app.*（f2_fetch 仅在子进程内使用 f2），
  可在主环境独立测试。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from monitor.platforms.douyin_personal_source import (
    PersonalItem,
    PersonalSnapshot,
    PersonalSourceError,
    _clean_str,
    _valid_timestamp,
    redact_sensitive_text,
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


def resolve_progress_file(base: str | os.PathLike | None = None) -> Path:
    """跨运行续传状态文件路径（workbuddy/f2，测试可用 WORKBUDDY_DIR 隔离）。"""
    if base:
        return Path(base)
    workspace_root = Path(__file__).resolve().parents[2]
    log_dir = Path(os.getenv("WORKBUDDY_DIR") or (workspace_root / "workbuddy")) / "f2"
    return log_dir / "sync_progress.json"


def clear_progress_file() -> None:
    """手动恢复工具：清除续传状态，下次同步从头读取。

    正常协议（提交确认）无需调用：取数失败、入库失败、占锁失败都**不**清理
    状态——已确认游标只指向已持久提交的数据，重放是幂等的。仅人工判断需要
    强制全量重取时使用。
    """
    try:
        resolve_progress_file().unlink(missing_ok=True)
    except OSError:
        pass


# 父进程同步锁：flock 覆盖“续传状态读取 → 取数子进程 → 数据库提交 → 游标确认”
# 完整生命周期。子进程不再自行加锁（父进程不再传 --lock-file），否则父子两次
# flock 会立即互斥。同进程内重入由线程锁放行：flock 对同进程的第二个 fd 也互斥。
_SYNC_LOCK_THREAD_GUARD = threading.RLock()
_SYNC_LOCK_HANDLE = None


def _sync_lock_path() -> Path:
    workspace_root = Path(__file__).resolve().parents[2]
    log_dir = Path(os.getenv("WORKBUDDY_DIR") or (workspace_root / "workbuddy")) / "f2"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "sync.lock"


def _acquire_sync_lock(lock_path: Path):
    """非阻塞获取跨进程同步锁；失败抛 already_running，绝不触碰任何进度状态。"""
    try:
        import fcntl

        fh = open(lock_path, "a")  # 不截断：互斥本体是 flock，不是文件内容
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise PersonalSourceError("already_running", "已有一次同步在运行（锁被占用）") from None
        return fh
    except ImportError:
        pass
    # Windows 兜底：O_EXCL 创建型锁（进程崩溃会遗留，由持有方释放路径删除）
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise PersonalSourceError("already_running", "已有一次同步在运行（锁文件存在）") from None
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return None  # 无句柄可关；释放按路径删除


def _release_sync_lock(handle) -> None:
    if handle is not None:
        try:
            handle.close()  # 关闭 fd 即释放 flock
        except OSError:
            pass
        return
    try:
        _sync_lock_path().unlink(missing_ok=True)
    except OSError:
        pass


@contextmanager
def f2_sync_lock():
    """跨进程互斥：覆盖续传状态读取、取数、数据库提交与进度确认全生命周期。

    - 占锁失败抛 already_running：失败方立即放弃，不读取、不写入、不清除任何
      进度状态，正在运行的持有者不受影响。
    - 同一进程内重入（run_ingest 持锁后再进 read_snapshot_f2）直接放行；
      跨线程由线程锁串行化（本进程内同一时间只有一次 F2 同步）。
    - 取数后、提交与确认期间锁不释放：其他进程不能在提交窗口内插进来
      推进/回滚游标。
    """
    global _SYNC_LOCK_HANDLE
    with _SYNC_LOCK_THREAD_GUARD:
        if _SYNC_LOCK_HANDLE is not None:
            yield
            return
        handle = _acquire_sync_lock(_sync_lock_path())
        _SYNC_LOCK_HANDLE = handle
        try:
            yield
        finally:
            _SYNC_LOCK_HANDLE = None
            _release_sync_lock(handle)


def confirm_f2_progress(snapshot, *, progress_file: str | os.PathLike | None = None) -> bool:
    """提交协议确认步：业务数据已持久提交后，才推进/清除跨运行续传状态。

    run_ingest 在 `db.commit()` 成功后调用：
    - 快照携带取数回传的 resume 状态（仅哈希身份/游标/完成标记，无 Cookie/明文）；
    - 全部请求范围确认完整 → 清除状态文件（下一轮从头完整同步）；
    - 否则原子覆盖为最新已确认游标；
    - 形状不完整/写入/替换/清理失败 → 返回 False 且保持旧状态：数据库可能已
      提交，调用方必须如实上报（不冒充干净成功），下一轮从旧确认游标幂等
      重放（marks/works/folder_items 均按唯一键幂等），不丢数据、不产生重复；
      文件系统恢复后确认成功即可清除/推进并开始新一轮。
    失败路径（占锁失败、取数失败、提交失败）不得调用本函数——失败方不删除、
    不推进任何状态。
    """
    from monitor.platforms.f2_fetch import write_confirmed_progress  # f2_fetch 顶层不依赖 f2

    resume = getattr(snapshot, "resume_state", None)
    path = str(progress_file) if progress_file is not None else str(resolve_progress_file())
    return write_confirmed_progress(path, resume)


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


def parse_f2_result(
    result: dict,
    *,
    source_path: str | None = None,
    scopes: tuple[str, ...] = SCOPES_ALL,
) -> PersonalSnapshot:
    """把 f2_fetch.py 的输出文档归一化为 PersonalSnapshot。

    结果文档 ok=False 时抛 PersonalSourceError；按**请求的 scope** 校验：
    未请求的 scope 缺失是正常（部分同步），请求了的 scope 缺失才是 schema_changed。

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
            redact_sensitive_text(_clean_str(error.get("message")) or "F2 取数失败（未提供原因）"),
        )
    unknown_scopes = [s for s in scopes if s not in SCOPES_ALL]
    if unknown_scopes:
        raise PersonalSourceError("bad_source", f"未知 scope: {unknown_scopes}")

    snapshot = PersonalSnapshot(source="f2", source_path=source_path)
    snapshot.warnings = [w for w in result.get("warnings") or [] if isinstance(w, str)]
    want_collections = "collections" in scopes
    snapshot.folders_available = want_collections
    snapshot.folder_mode = "multi" if want_collections else None

    known_ids: set[str] = set()
    for scope, mark in (("like", "like"), ("collection", "favorite")):
        if scope not in scopes:
            continue
        if scope not in result:
            raise PersonalSourceError("schema_changed", f"F2 结果缺少已请求的 {scope} 列表")
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

    if want_collections:
        collects = result.get("collects")
        if not isinstance(collects, dict):
            raise PersonalSourceError("schema_changed", "F2 结果缺少已请求的 collects 列表")
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
        # 夹级完整性明细：夹列表分页未走完 + 各夹作品分页未走完
        if collects.get("folders_complete") is False:
            snapshot.folder_incomplete.append("folders")
        folder_incomplete = collects.get("folder_incomplete")
        if isinstance(folder_incomplete, list):
            # 只接受序号形式（folder#2），真实收藏夹 ID 不得进入日志/报告
            snapshot.folder_incomplete.extend(
                f for f in folder_incomplete if isinstance(f, str) and f.startswith("folder#")
            )

    for block in (result.get("collects", {}).get("folder_items") or {}).values() if want_collections else ():
        for work in block.get("works") or []:
            item = _item_from_work(work, "favorite_folder")
            if not item.external_id:
                snapshot.skipped_invalid.append({"list": "folder_items", "reason": "missing_identity"})
            elif item.external_id not in known_ids:
                # 已在点赞/平铺收藏中出现过的作品不再重复登记
                snapshot.items.append(item)
                known_ids.add(item.external_id)
    snapshot.folder_memberships = _merge_folder_memberships(result) if want_collections else {}
    snapshot.skipped_invalid.extend(
        s for s in (result.get("skipped") or []) if isinstance(s, dict)
    )
    # 提交协议：取数进程不写进度文件，回传的续传状态由父进程在业务数据
    # 持久提交后经 confirm_f2_progress 确认落盘/清除（形状校验在确认步做）。
    resume = result.get("resume")
    if isinstance(resume, dict):
        # {} 空对象也算 dict，形状校验交给 confirm_f2_progress 的写校验失败路径。
        snapshot.resume_state = resume
        snapshot.resume_anomaly = None
    elif result.get("resume_disabled") is True:
        # 未配置进度文件时的合法禁用：回执缺失是预期行为，无需确认。
        snapshot.resume_state = None
        snapshot.resume_anomaly = None
    else:
        # 应有而缺失或类型异常：交给 ingest 如实上报，绝不静默跳过确认。
        snapshot.resume_state = None
        snapshot.resume_anomaly = "missing" if "resume" not in result else "invalid_type"
    snapshot.snapshot_id = _snapshot_id(snapshot.items, snapshot.folder_memberships)
    return snapshot


def read_aweme_detail(
    aweme_id: str,
    *,
    runtime_dir: str | os.PathLike | None = None,
    cookie_file: str | os.PathLike | None = None,
    budget_seconds: int = 120,
) -> dict:
    """单作品详情（图文下载用）：运行 F2 detail 模式并返回白名单字段。

    只调用 aweme/detail 只读接口，不打开/播放作品网页。返回
    {"external_id", "title", "author", "published_at", "images": [url...]}。
    失败 kind：login_required / runtime_missing / login_expired /
    not_image_post / platform_error / timeout 等。
    """
    cookie_path = resolve_cookie_file(cookie_file)
    if not cookie_path.is_file():
        # 不回显本机路径：文案只指向配置约定
        raise PersonalSourceError(
            "login_required",
            f"未找到抖音登录态文件（默认 config/f2/douyin_cookie.txt 或设置 {COOKIE_FILE_ENV}），请先由用户完成登录导出",
        )
    runtime = resolve_runtime_dir(runtime_dir)
    python = runtime_python(runtime)
    script = Path(__file__).resolve().parent / "f2_fetch.py"

    workspace_root = Path(__file__).resolve().parents[2]
    log_dir = Path(os.getenv("WORKBUDDY_DIR") or (workspace_root / "workbuddy")) / "f2"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "detail.log"

    output_path = Path(tempfile.mkdtemp(prefix="f2-detail-")) / "detail.json"
    cmd = [
        str(python),
        str(script),
        "--mode", "detail",
        "--aweme-id", str(aweme_id),
        "--cookie-file", str(cookie_path),
        "--output", str(output_path),
        "--budget-seconds", str(budget_seconds),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=budget_seconds + 60,
        )
        try:
            combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
            log_path.write_text(redact_sensitive_text(combined, max_length=20000), encoding="utf-8")
        except OSError:
            pass
        if not output_path.is_file():
            raise PersonalSourceError("runtime_missing", f"F2 详情取数未产出结果文件（日志: {log_path}）")
        try:
            result = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise PersonalSourceError("invalid_json", f"F2 详情结果不是有效 JSON（日志: {log_path}）") from None
    except subprocess.TimeoutExpired:
        raise PersonalSourceError("timeout", f"F2 详情取数超出时间预算 {budget_seconds}s") from None
    finally:
        try:
            output_path.unlink(missing_ok=True)
            output_path.parent.rmdir()
        except OSError:
            pass

    if not isinstance(result, dict) or result.get("ok") is not True:
        error = result.get("error") if isinstance(result, dict) and isinstance(result.get("error"), dict) else {}
        raise PersonalSourceError(
            _clean_str(error.get("kind")) or "platform_error",
            redact_sensitive_text(_clean_str(error.get("message")) or "F2 详情取数失败"),
        )
    detail = result.get("detail")
    if not isinstance(detail, dict) or not _clean_str(detail.get("external_id")):
        raise PersonalSourceError("invalid_json", "F2 详情结果缺少作品身份")
    return detail


def read_snapshot_f2(
    *,
    runtime_dir: str | os.PathLike | None = None,
    cookie_file: str | os.PathLike | None = None,
    scopes: tuple[str, ...] = SCOPES_ALL,
    sec_user_id: str = "",
    folder_ids: tuple[str, ...] = (),
    budget_seconds: int | None = None,
    page_counts: int = 20,
    progress_file: str | os.PathLike | None = None,
) -> PersonalSnapshot:
    """通道 C：运行 F2 取数脚本并解析。入口由 personal_source_ingest 调用。

    `progress_file=None` 时使用 workbuddy/f2/sync_progress.json 做跨运行续传
    （只含哈希身份/游标/完成标记）；传空字符串可显式禁用（如 dry-run）。
    取数在 f2_sync_lock 内执行：并发调用方立即 already_running 且不触碰进度
    状态；run_ingest 持同一把锁覆盖到数据库提交与游标确认（重入直接放行）。
    """
    if budget_seconds is None:
        # 每轮取数预算：F2_FETCH_BUDGET_SECONDS 可调；风控事件后建议调小
        # 以降低单轮突发请求量（默认仍为 900s）。
        try:
            budget_seconds = int(os.getenv("F2_FETCH_BUDGET_SECONDS", "900"))
        except ValueError:
            budget_seconds = 900
    cookie_path = resolve_cookie_file(cookie_file)
    if not cookie_path.is_file():
        raise PersonalSourceError(
            "login_required",
            f"未找到抖音登录态文件（默认 config/f2/douyin_cookie.txt 或设置 {COOKIE_FILE_ENV}），请先由用户完成登录导出",
        )
    with f2_sync_lock():
        return _read_snapshot_f2_locked(
            cookie_path=cookie_path, runtime_dir=runtime_dir, scopes=scopes,
            sec_user_id=sec_user_id, folder_ids=folder_ids,
            budget_seconds=budget_seconds, page_counts=page_counts,
            progress_file=progress_file,
        )


def _read_snapshot_f2_locked(*, cookie_path: Path, runtime_dir, scopes, sec_user_id,
                             folder_ids, budget_seconds, page_counts,
                             progress_file) -> PersonalSnapshot:
    runtime = resolve_runtime_dir(runtime_dir)
    python = runtime_python(runtime)
    script = Path(__file__).resolve().parent / "f2_fetch.py"

    workspace_root = Path(__file__).resolve().parents[2]
    # 与 app.core.config 一致：WORKBUDDY_DIR 可覆盖（测试隔离依赖）
    log_dir = Path(os.getenv("WORKBUDDY_DIR") or (workspace_root / "workbuddy")) / "f2"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "fetch.log"

    output_path = Path(tempfile.mkdtemp(prefix="f2-fetch-")) / "result.json"
    cmd = [
        str(python),
        str(script),
        "--cookie-file", str(cookie_path),
        "--output", str(output_path),
        "--scopes", ",".join(scopes),
        "--budget-seconds", str(budget_seconds),
        "--page-counts", str(page_counts),
        # 互斥由父进程 f2_sync_lock 持有（覆盖提交与确认），子进程不再自行加锁
    ]
    if progress_file == "":
        resume_path = None  # 显式禁用（dry-run 不推进游标）
    elif progress_file is None:
        resume_path = resolve_progress_file()
    else:
        resume_path = Path(progress_file)
    if resume_path:
        cmd += ["--progress-file", str(resume_path)]
    if sec_user_id:
        cmd += ["--sec-user-id", sec_user_id]
    if folder_ids:
        cmd += ["--folder-ids", ",".join(folder_ids)]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=budget_seconds + 120,
        )
        # 上游输出（含 F2 日志）先脱敏再落 fetch.log：凭据/账号标识不得进入日志
        try:
            combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
            log_path.write_text(redact_sensitive_text(combined, max_length=20000), encoding="utf-8")
        except OSError:
            pass
        if not output_path.is_file():
            raise PersonalSourceError("runtime_missing", f"F2 取数未产出结果文件（日志: {log_path}）")
        try:
            result = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise PersonalSourceError("invalid_json", f"F2 取数结果不是有效 JSON（日志: {log_path}）") from None
        snapshot = parse_f2_result(result, source_path=str(runtime), scopes=tuple(scopes))
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
