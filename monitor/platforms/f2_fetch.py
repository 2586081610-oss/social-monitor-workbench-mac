"""F2 取数脚本：读取本人抖音点赞 / 平铺收藏 / 收藏夹 / 夹内作品（只读列表接口）。

本脚本只在隔离运行时 `tools/f2-runtime/.venv` 内执行（依赖固定 commit 的 F2），
由 `monitor/platforms/douyin_f2_source.py` 以子进程方式调用，二者共同遵循
`douyin_personal_source.PersonalSourceError` 的失败语义。

硬约定：
- 只读。只调用点赞列表、平铺收藏、收藏夹列表、收藏夹作品四个列表接口；
  不下载媒体、不写评论、不读取观看历史或聊天记录。
- 登录态凭据只经 `--cookie-file` 读入内存，绝不打印、不写日志、不写输出。
- 失败 ≠ 空列表：登录过期 / 接口报错 / 重试用尽都产生 kind 明确的错误，
  绝不把失败伪装成"没有内容"。
- 平台返回条数可能超过请求数量：如一页返回多于请求的 count，如实计数
  （server_oversize_pages）并全部接受，不丢数据也不当作错误。
- 输出只含白名单字段（作品身份、标题、作者、发布时间、封面、媒体类型），
  输出文件由调用方放在临时目录，不落在仓库内。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# 平台返回的 per-item status_code 中已知"需要重新登录"的码（其余码原样上报）
PAYLOAD_STATUS_LOGIN_EXPIRED = 8

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class ScopeError(Exception):
    """单个列表读取失败。kind 对齐 PersonalSourceError 的语义。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="F2 本人抖音列表只读取数")
    parser.add_argument("--cookie-file", required=True, help="登录态 cookie 文本文件（不回显）")
    parser.add_argument("--output", required=True, help="结果 JSON 输出路径")
    parser.add_argument(
        "--scopes", default="like,collection,collections",
        help="要读取的列表：like（点赞）/ collection（平铺收藏）/ collections（收藏夹及夹内作品）",
    )
    parser.add_argument("--sec-user-id", default="", help="本人 sec_user_id（点赞列表需要）")
    parser.add_argument("--folder-ids", default="", help="只读这些收藏夹（缺省读全部）")
    parser.add_argument("--page-counts", type=int, default=20)
    parser.add_argument("--max-items-per-list", type=int, default=5000)
    parser.add_argument("--max-pages-per-list", type=int, default=500)
    parser.add_argument("--budget-seconds", type=int, default=900, help="整体时间预算")
    parser.add_argument("--timeout", type=int, default=5, help="F2 页间等待秒数（限流）")
    parser.add_argument("--max-retries", type=int, default=2, help="F2 单接口重试次数")
    parser.add_argument("--lock-file", default="", help="互斥锁文件（防止同步重叠）")
    return parser.parse_args()


def acquire_lock(lock_path: str) -> None:
    if not lock_path:
        return
    try:
        import fcntl

        fh = open(lock_path, "w")  # noqa: SIM115 - 句柄需存活到进程退出
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ScopeError("already_running", "已有一次同步在运行（锁被占用）")
        return
    except ImportError:
        pass
    # Windows 兜底：O_EXCL 创建型锁（进程崩溃会遗留，由调用方按 mtime 清理）
    lock = Path(lock_path)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        raise ScopeError("already_running", "已有一次同步在运行（锁文件存在）")


def iso_utc_from_unix(ts) -> str | None:
    """发布时间：unix 秒 → 完整 UTC ISO 串（秒精度）。缺项/非法一律 None，不猜测。"""
    if not isinstance(ts, int) or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def extract_work(raw_aweme) -> tuple[dict | None, dict | None]:
    """从平台原始 aweme 对象提取白名单字段。身份缺失 → (None, skip_reason)。"""
    if not isinstance(raw_aweme, dict):
        return None, {"reason": "not_an_object", "raw_kind": type(raw_aweme).__name__}
    aweme_id = raw_aweme.get("aweme_id")
    aweme_id = str(aweme_id).strip() if aweme_id is not None else ""
    if not aweme_id:
        return None, {"reason": "missing_identity", "raw_id": ""}
    author = raw_aweme.get("author") if isinstance(raw_aweme.get("author"), dict) else {}
    images = raw_aweme.get("images") if isinstance(raw_aweme.get("images"), list) else None
    media_type = "image" if images else "video"
    cover_url = None
    if images and isinstance(images[0], dict):
        url_list = images[0].get("url_list")
        if isinstance(url_list, list) and url_list:
            cover_url = url_list[0]
    video = raw_aweme.get("video") if isinstance(raw_aweme.get("video"), dict) else {}
    if cover_url is None and isinstance(video.get("cover"), dict):
        url_list = video["cover"].get("url_list")
        if isinstance(url_list, list) and url_list:
            cover_url = url_list[0]
    work = {
        "external_id": aweme_id,
        "title": (raw_aweme.get("desc") or "").strip() or None,
        "author": (author.get("nickname") or "").strip() or None,
        "author_id": (author.get("sec_uid") or "").strip() or None,
        "url": f"https://www.douyin.com/video/{aweme_id}",
        "cover_url": cover_url,
        "media_type": media_type,
        "published_at": iso_utc_from_unix(raw_aweme.get("create_time")),
    }
    return work, None


async def consume_video_pages(handler, make_gen, *, label: str, requested_page_counts: int,
                              max_pages: int, max_items: int, warnings: list) -> tuple[list[dict], dict]:
    """消费 F2 分页异步生成器：逐页提取作品，处理去重/超量/状态码/页数预算。"""
    works: list[dict] = []
    seen: set[str] = set()
    skipped: list[dict] = []
    pages = 0
    oversize_pages = 0
    finished_cleanly = False

    async for page in make_gen(handler):
        pages += 1
        raw = page._to_raw()
        payload_status = page.status_code
        if payload_status not in (0, None):
            kind = ("login_expired" if payload_status == PAYLOAD_STATUS_LOGIN_EXPIRED else "platform_error")
            raise ScopeError(kind, f"{label} 接口返回 status_code={payload_status}")
        aweme_list = raw.get("aweme_list") if isinstance(raw, dict) else None
        aweme_list = aweme_list if isinstance(aweme_list, list) else []
        if len(aweme_list) > requested_page_counts:
            oversize_pages += 1
        for entry in aweme_list:
            work, skip = extract_work(entry)
            if work is None:
                skipped.append({"list": label, **(skip or {})})
            elif work["external_id"] not in seen:
                seen.add(work["external_id"])
                works.append(work)
        if page.has_more is None:
            warnings.append(f"{label} 第 {pages} 页未返回 has_more，按不完整处理")
            break
        if not page.has_more:
            finished_cleanly = True
            break
        if pages >= max_pages:
            warnings.append(f"{label} 达到页数上限 {max_pages}，列表可能不完整")
            break
        if len(works) >= max_items:
            warnings.append(f"{label} 达到条数上限 {max_items}，列表可能不完整")
            break
    meta = {
        "pages": pages,
        "items": len(works),
        "complete": finished_cleanly,
        "server_oversize_pages": oversize_pages,
    }
    if skipped:
        meta["skipped"] = skipped[:20]
        meta["skipped_count"] = len(skipped)
    return works, meta


def scope_error_from_exception(exc: Exception, label: str) -> ScopeError:
    """把 F2 异常翻译成明确 kind。点赞列表未登录/未公开在 F2 中表现为重试用尽。"""
    from f2.exceptions.api_exceptions import (
        APIRateLimitError,
        APIRetryExhaustedError,
        APIUnauthorizedError,
    )

    if isinstance(exc, APIRetryExhaustedError):
        return ScopeError(
            "like_list_unavailable",
            f"{label} 多次返回空内容：登录可能已过期，或列表未对本人可见（平台不区分两者）",
        )
    if isinstance(exc, APIUnauthorizedError):
        return ScopeError("login_expired", f"{label} 接口返回未授权（HTTP 401）")
    if isinstance(exc, APIRateLimitError):
        return ScopeError("rate_limited", f"{label} 触发平台限流（HTTP 429）")
    return ScopeError("platform_error", f"{label} 接口失败：{type(exc).__name__}: {exc}")


async def run_scopes(args: argparse.Namespace) -> dict:
    cookie = Path(args.cookie_file).read_text(encoding="utf-8").strip()
    if not cookie:
        raise ScopeError("login_expired", "cookie 文件为空")

    # F2 只在隔离运行时可用，延迟导入；失败即运行时缺失，不与平台错误混淆。
    try:
        from f2.apps.douyin.handler import DouyinHandler
    except Exception as exc:  # pragma: no cover - 隔离环境本身损坏
        raise ScopeError("runtime_missing", f"F2 运行时不可用：{exc}") from None

    import importlib.metadata as md
    f2_version = md.version("f2")

    kwargs = {
        "headers": {"User-Agent": _USER_AGENT},
        "cookie": cookie,
        "timeout": args.timeout,
        "max_retries": args.max_retries,
        "proxies": {"http://": None, "https://": None},
    }
    handler = DouyinHandler(kwargs)
    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    warnings: list[str] = []
    result: dict = {"ok": True, "warnings": warnings, "runtime": {"f2_version": f2_version}}
    page_counts = args.page_counts

    async def fetch_one(label: str, make_gen) -> None:
        works, meta = await consume_video_pages(
            handler, make_gen, label=label, requested_page_counts=page_counts,
            max_pages=args.max_pages_per_list, max_items=args.max_items_per_list,
            warnings=warnings,
        )
        result[label] = meta | {"works": works}

    for scope in scopes:
        if scope == "like":
            if not args.sec_user_id:
                warnings.append("未提供本人 sec_user_id，跳过点赞列表")
                continue
            await fetch_one("like", lambda h: h.fetch_user_like_videos(
                args.sec_user_id, page_counts=page_counts, max_counts=args.max_items_per_list))
        elif scope == "collection":
            await fetch_one("collection", lambda h: h.fetch_user_collection_videos(
                page_counts=page_counts, max_counts=args.max_items_per_list))
        elif scope == "collections":
            await fetch_collections_scope(handler, args, result, warnings)
        else:
            raise ScopeError("bad_scope", f"未知 scope: {scope}")
        await asyncio.sleep(1)
    return result


async def fetch_collections_scope(handler, args, result: dict, warnings: list) -> None:
    """收藏夹列表 + 逐夹作品。文件夹读取失败按失败处理（不静默跳过）。"""
    folders: dict[str, dict] = {}
    pages = 0
    async for page in handler.fetch_user_collects(
        page_counts=args.page_counts, max_counts=args.max_items_per_list,
    ):
        pages += 1
        raw = page._to_raw()
        payload_status = page.status_code
        if payload_status not in (0, None):
            kind = ("login_expired" if payload_status == PAYLOAD_STATUS_LOGIN_EXPIRED else "platform_error")
            raise ScopeError(kind, f"收藏夹列表接口返回 status_code={payload_status}")
        rows = raw.get("collects_list") if isinstance(raw, dict) else None
        for row in rows or []:
            folder_id = str(row.get("collects_id") or "").strip()
            if not folder_id:
                continue
            folders[folder_id] = {
                "folder_id": folder_id,
                "name": (row.get("collects_name") or "").strip() or None,
                "item_count": row.get("collects_item_count") if row.get("collects_item_count") is not None else row.get("item_count"),
            }
        if not page.has_more:
            break
        if pages >= args.max_pages_per_list:
            warnings.append("收藏夹列表达到页数上限，可能不完整")
            break
    wanted = {f.strip() for f in args.folder_ids.split(",") if f.strip()}
    folder_items: dict[str, dict] = {}
    first = True
    for folder_id, info in folders.items():
        if wanted and folder_id not in wanted:
            continue
        if not first:
            await asyncio.sleep(1)
        first = False
        works, meta = await consume_video_pages(
            handler,
            lambda h, fid=folder_id: h.fetch_user_collects_videos(
                fid, page_counts=args.page_counts, max_counts=args.max_items_per_list),
            label=f"folder:{folder_id}", requested_page_counts=args.page_counts,
            max_pages=args.max_pages_per_list, max_items=args.max_items_per_list,
            warnings=warnings,
        )
        folder_items[folder_id] = meta | {"works": works}
    result["collects"] = {
        "pages": pages,
        "folders": sorted(folders.values(), key=lambda f: f["folder_id"]),
        "folder_items": folder_items,
        "complete": True,
    }


def write_output(path: str, doc: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, target)


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    try:
        acquire_lock(args.lock_file)
    except ScopeError as exc:
        write_output(args.output, {"ok": False, "error": {"kind": exc.kind, "message": str(exc)}})
        return 1

    async def bounded() -> dict:
        return await asyncio.wait_for(run_scopes(args), timeout=args.budget_seconds)

    try:
        result = asyncio.run(bounded())
    except ScopeError as exc:
        write_output(args.output, {"ok": False, "error": {"kind": exc.kind, "message": str(exc)}})
        return 1
    except asyncio.TimeoutError:
        write_output(args.output, {
            "ok": False,
            "error": {"kind": "timeout", "message": f"取数超出时间预算 {args.budget_seconds}s"},
        })
        return 1
    except Exception as exc:  # noqa: BLE001 - 兜底错误必须落盘为明确 JSON
        write_output(args.output, {
            "ok": False,
            "error": {"kind": "platform_error", "message": f"{type(exc).__name__}: {exc}"},
        })
        return 1
    result["elapsed_seconds"] = round(time.monotonic() - started, 1)
    write_output(args.output, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
