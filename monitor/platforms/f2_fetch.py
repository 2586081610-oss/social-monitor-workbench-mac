"""F2 取数脚本：读取本人抖音点赞 / 平铺收藏 / 收藏夹 / 夹内作品（只读列表接口）。

本脚本只在隔离运行时 `tools/f2-runtime/.venv` 内执行（依赖固定 commit 的 F2），
由 `monitor/platforms/douyin_f2_source.py` 以子进程方式调用，二者共同遵循
`douyin_personal_source.PersonalSourceError` 的失败语义。

硬约定：
- 只读。只调用点赞列表、平铺收藏、收藏夹列表、收藏夹作品四个列表接口；
  不下载媒体、不写评论、不读取观看历史或聊天记录。
- 登录态凭据只经 `--cookie-file` 读入内存，绝不打印、不写日志、不写输出。
- 失败 ≠ 空列表：登录过期 / 接口报错 / 重试用尽 / 响应缺字段 / 上游提前结束
  都产生 kind 明确的错误或"不完整"标记，绝不把失败伪装成"没有内容"。
- 基于原始响应严格检查：`aweme_list` 缺失或非列表按平台错误处理；
  `has_more` 缺失按不完整处理（真实 F2 Filter 会把缺失转成 False，
  不能依赖 Filter 属性语义）。
- 平台返回条数可能超过请求数量：如一页返回多于请求的 count，如实计数
  （server_oversize_pages）并全部接受，不丢数据也不当作错误。
- 所有错误消息与上游输出经过 redact_sensitive_text：凭据、账号标识不得
  落入日志与结果。
- 输出只含白名单字段（作品身份、标题、作者、发布时间、封面、媒体类型），
  输出文件由调用方放在临时目录，不落在仓库内。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:  # 作为脚本运行时 sys.path[0] 是本目录；作为模块导入时走包路径
    from douyin_personal_source import redact_sensitive_text
except ImportError:  # pragma: no cover - 脚本运行路径
    from monitor.platforms.douyin_personal_source import redact_sensitive_text

# 平台返回的 per-item status_code 中已知"需要重新登录"的码（其余码原样上报）
PAYLOAD_STATUS_LOGIN_EXPIRED = 8

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

KNOWN_SCOPES = ("like", "collection", "collections")


class ScopeError(Exception):
    """单个列表读取失败。kind 对齐 PersonalSourceError 的语义。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="F2 本人抖音列表/作品详情只读取数")
    parser.add_argument("--mode", choices=["list", "detail"], default="list",
                        help="list=本人列表（点赞/收藏/收藏夹）；detail=单作品详情（图文下载用）")
    parser.add_argument("--aweme-id", default="", help="detail 模式的作品 ID")
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


_LOCK_HANDLE = None  # 保持锁文件句柄存活：函数返回后 GC 关闭 fd 会立即释放锁


def acquire_lock(lock_path: str) -> None:
    if not lock_path:
        return
    global _LOCK_HANDLE
    try:
        import fcntl

        fh = open(lock_path, "w")  # noqa: SIM115 - 句柄须存活到进程退出（模块级引用）
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise ScopeError("already_running", "已有一次同步在运行（锁被占用）")
        _LOCK_HANDLE = fh
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


def _payload_status_kind(payload_status) -> tuple[str, str] | None:
    """payload status_code ≠ 0 → (kind, message)。0/None → None。"""
    if payload_status in (0, None):
        return None
    kind = "login_expired" if payload_status == PAYLOAD_STATUS_LOGIN_EXPIRED else "platform_error"
    return kind, f"接口返回 status_code={payload_status}"


async def consume_video_pages(make_gen, *, label: str, requested_page_counts: int,
                              max_pages: int, max_items: int, warnings: list) -> tuple[list[dict], dict]:
    """消费 F2 分页异步生成器：逐页基于原始响应严格校验并提取作品。

    - `aweme_list` 缺失/非列表 → ScopeError(platform_error)：失败不冒充空列表。
    - `has_more` 键缺失 → 记警告并按不完整结束（真实 F2 Filter 把缺失转 False）。
    - 上游未产出任何页 / 提前结束 → 显式错误或不完整，绝不当成功。
    """
    works: list[dict] = []
    seen: set[str] = set()
    skipped: list[dict] = []
    pages = 0
    oversize_pages = 0
    finished_cleanly = False
    stopped_by_budget = False
    has_more_missing = False

    async for page in make_gen(None):
        pages += 1
        raw = page._to_raw()
        status_error = _payload_status_kind(page.status_code)
        if status_error is not None:
            raise ScopeError(status_error[0], f"{label} {status_error[1]}")
        if not isinstance(raw, dict) or "aweme_list" not in raw or not isinstance(raw.get("aweme_list"), list):
            raise ScopeError("platform_error", f"{label} 响应缺少 aweme_list 字段")
        aweme_list = raw["aweme_list"]
        if len(aweme_list) > requested_page_counts:
            oversize_pages += 1
        for entry in aweme_list:
            work, skip = extract_work(entry)
            if work is None:
                skipped.append({"list": label, **(skip or {})})
            elif work["external_id"] not in seen:
                seen.add(work["external_id"])
                works.append(work)
        if "has_more" not in raw:
            warnings.append(f"{label} 第 {pages} 页未返回 has_more，按不完整处理")
            has_more_missing = True
            break
        if not raw["has_more"]:
            finished_cleanly = True
            break
        if pages >= max_pages:
            warnings.append(f"{label} 达到页数上限 {max_pages}，列表可能不完整")
            stopped_by_budget = True
            break
        if len(works) >= max_items:
            warnings.append(f"{label} 达到条数上限 {max_items}，列表可能不完整")
            stopped_by_budget = True
            break
    if pages == 0:
        raise ScopeError("platform_error", f"{label} 未返回任何数据页（上游提前结束）")
    if not finished_cleanly and not stopped_by_budget and not has_more_missing:
        # 生成器耗尽但最后一页仍声称有更多：上游提前结束，按不完整处理
        warnings.append(f"{label} 上游在 has_more=1 时提前结束，按不完整处理")
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


def scope_error_from_exception(exc: Exception, label: str, scope: str) -> ScopeError:
    """把 F2 异常翻译成明确 kind；消息脱敏（不落凭据/账号标识）。

    - APIRetryExhaustedError：点赞列表为"未登录或未公开"（平台不区分两者）；
      其他列表按平台错误处理。
    - APIUnauthorizedError → login_expired；APIRateLimitError → rate_limited。
    """
    try:
        from f2.exceptions.api_exceptions import (
            APIRateLimitError,
            APIRetryExhaustedError,
            APIUnauthorizedError,
        )
    except ImportError:  # pragma: no cover - 非 F2 运行时（测试桩模块）
        APIRetryExhaustedError = APIUnauthorizedError = APIRateLimitError = ()

    if APIRetryExhaustedError and isinstance(exc, APIRetryExhaustedError):
        if scope == "like":
            return ScopeError(
                "like_list_unavailable",
                f"{label} 多次返回空内容：登录可能已过期，或列表未对本人可见（平台不区分两者）",
            )
        return ScopeError("platform_error", f"{label} 接口多次重试后仍失败")
    if APIUnauthorizedError and isinstance(exc, APIUnauthorizedError):
        return ScopeError("login_expired", f"{label} 接口返回未授权（HTTP 401）")
    if APIRateLimitError and isinstance(exc, APIRateLimitError):
        return ScopeError("rate_limited", f"{label} 触发平台限流（HTTP 429）")
    return ScopeError("platform_error", f"{label} 接口失败：{type(exc).__name__}: {redact_sensitive_text(str(exc), max_length=160)}")


async def run_scopes(args: argparse.Namespace) -> dict:
    cookie = Path(args.cookie_file).read_text(encoding="utf-8").strip()
    if not cookie:
        raise ScopeError("login_expired", "cookie 文件为空")

    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    unknown = [s for s in scopes if s not in KNOWN_SCOPES]
    if unknown:
        raise ScopeError("bad_scope", f"未知 scope: {unknown}")
    if not scopes:
        raise ScopeError("bad_scope", "scopes 为空")
    if "like" in scopes and not args.sec_user_id.strip():
        raise ScopeError(
            "missing_sec_user_id",
            "读取点赞列表需要本人 sec_user_id（个人来源配置中填写）",
        )

    # F2 只在隔离运行时可用，延迟导入；失败即运行时缺失，不与平台错误混淆。
    try:
        from f2.apps.douyin.handler import DouyinHandler
    except Exception as exc:  # pragma: no cover - 隔离环境本身损坏
        raise ScopeError("runtime_missing", f"F2 运行时不可用：{redact_sensitive_text(str(exc), max_length=160)}") from None

    _silence_f2_output()

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
    warnings: list[str] = []
    result: dict = {"ok": True, "warnings": warnings, "runtime": {"f2_version": f2_version}}
    page_counts = args.page_counts

    async def fetch_one(label: str, scope: str, make_gen) -> None:
        try:
            works, meta = await consume_video_pages(
                make_gen, label=label, requested_page_counts=page_counts,
                max_pages=args.max_pages_per_list, max_items=args.max_items_per_list,
                warnings=warnings,
            )
        except ScopeError:
            raise
        except Exception as exc:  # noqa: BLE001 - 翻译为明确 kind 并脱敏
            raise scope_error_from_exception(exc, label, scope) from None
        result[scope] = meta | {"works": works}

    for scope in scopes:
        if scope == "like":
            await fetch_one("like", "like", lambda h: handler.fetch_user_like_videos(
                args.sec_user_id.strip(), page_counts=page_counts, max_counts=args.max_items_per_list))
        elif scope == "collection":
            await fetch_one("collection", "collection", lambda h: handler.fetch_user_collection_videos(
                page_counts=page_counts, max_counts=args.max_items_per_list))
        elif scope == "collections":
            try:
                await fetch_collections_scope(handler, args, result, warnings)
            except ScopeError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise scope_error_from_exception(exc, "collections", "collections") from None
        await asyncio.sleep(1)
    return result


def _silence_f2_output() -> None:
    """静默 F2 的 INFO 日志与 rich 输出：sec_user_id/收藏夹 ID 不得进入 fetch.log。"""
    logging.getLogger("f2").setLevel(logging.CRITICAL)
    logging.getLogger("f2.log").setLevel(logging.CRITICAL)
    try:
        from f2.apps.douyin.handler import rich_console

        rich_console.quiet = True
    except Exception:  # noqa: BLE001 - 输出抑制失败不影响取数
        pass


async def fetch_collections_scope(handler, args, result: dict, warnings: list) -> None:
    """收藏夹列表 + 逐夹作品。文件夹读取失败按失败处理（不静默跳过）。

    完整性 = 夹列表分页走完 且 每个夹的作品分页走完；任何一处未走完都如实上报。
    """
    folders: dict[str, dict] = {}
    pages = 0
    folders_complete = False
    async for page in handler.fetch_user_collects(
        page_counts=args.page_counts, max_counts=args.max_items_per_list,
    ):
        pages += 1
        raw = page._to_raw()
        status_error = _payload_status_kind(page.status_code)
        if status_error is not None:
            raise ScopeError(status_error[0], f"收藏夹列表 {status_error[1]}")
        if not isinstance(raw, dict) or "collects_list" not in raw or not isinstance(raw.get("collects_list"), list):
            raise ScopeError("platform_error", "收藏夹列表响应缺少 collects_list 字段")
        for row in raw["collects_list"]:
            if not isinstance(row, dict):
                continue
            folder_id = str(row.get("collects_id") or "").strip()
            if not folder_id:
                continue
            folders[folder_id] = {
                "folder_id": folder_id,
                "name": (row.get("collects_name") or "").strip() or None,
                "item_count": row.get("collects_item_count") if row.get("collects_item_count") is not None else row.get("item_count"),
            }
        if "has_more" not in raw:
            warnings.append("收藏夹列表未返回 has_more，按不完整处理")
            break
        if not raw["has_more"]:
            folders_complete = True
            break
        if pages >= args.max_pages_per_list:
            warnings.append("收藏夹列表达到页数上限，可能不完整")
            break
    if pages == 0:
        raise ScopeError("platform_error", "收藏夹列表未返回任何数据页（上游提前结束）")

    wanted = {f.strip() for f in args.folder_ids.split(",") if f.strip()}
    folder_items: dict[str, dict] = {}
    folder_incomplete: list[str] = []
    all_items_complete = folders_complete
    first = True
    for ordinal, folder_id in enumerate(folders, start=1):
        if wanted and folder_id not in wanted:
            continue
        if not first:
            await asyncio.sleep(1)
        first = False
        # 日志/不完整清单只允许序号，不允许真实收藏夹 ID
        label = f"收藏夹#{ordinal}"
        try:
            works, meta = await _fetch_folder_items_raw(
                handler, folder_id, label=label, args=args, warnings=warnings,
            )
        except ScopeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise scope_error_from_exception(exc, label, "collections") from None
        folder_items[folder_id] = meta | {"works": works}
        if not meta["complete"]:
            folder_incomplete.append(f"folder#{ordinal}")
            all_items_complete = False
    result["collects"] = {
        "pages": pages,
        "folders": sorted(folders.values(), key=lambda f: f["folder_id"]),
        "folders_complete": folders_complete,
        "folder_items": folder_items,
        "folder_incomplete": folder_incomplete,
        "complete": all_items_complete and bool(pages),
    }


async def _fetch_folder_items_raw(handler, folder_id: str, *, label: str, args, warnings: list):
    """逐页直读爬虫原始响应获取夹内作品（不经过 handler 生成器）。

    真实 F2 的 `fetch_user_collects_videos` 生成器对空 `aweme_list` 在 yield 前
    就结束，无法区分"合法空夹"与"上游失败"。这里直接调用底层 crawler 并对原始
    响应严格校验：
    - status_code=8 → login_expired（夹内登录失效正确分类）；
    - aweme_list 缺失/非列表 → platform_error；
    - `aweme_list=[]` 且 has_more=0 → 合法空夹（complete，继续其他夹）；
    - has_more 键缺失 → 不完整；页数/条数预算 → 不完整。
    """
    from f2.apps.douyin.crawler import DouyinCrawler
    from f2.apps.douyin.model import UserCollectsVideo

    works: list[dict] = []
    seen: set[str] = set()
    skipped: list[dict] = []
    pages = 0
    oversize_pages = 0
    finished_cleanly = False
    stopped_by_budget = False
    has_more_missing = False
    cursor = 0

    async with DouyinCrawler(handler.kwargs) as crawler:
        while True:
            params = UserCollectsVideo(
                cursor=cursor, count=int(args.page_counts), collects_id=str(folder_id),
            )
            response = await crawler.fetch_user_collects_video(params)
            pages += 1
            status_code = response.get("status_code") if isinstance(response, dict) else None
            status_error = _payload_status_kind(status_code)
            if status_error is not None:
                raise ScopeError(status_error[0], f"{label} {status_error[1]}")
            if not isinstance(response, dict) or "aweme_list" not in response \
                    or not isinstance(response.get("aweme_list"), list):
                raise ScopeError("platform_error", f"{label} 响应缺少 aweme_list 字段")
            aweme_list = response["aweme_list"]
            if len(aweme_list) > args.page_counts:
                oversize_pages += 1
            for entry in aweme_list:
                work, skip = extract_work(entry)
                if work is None:
                    skipped.append({"list": label, **(skip or {})})
                elif work["external_id"] not in seen:
                    seen.add(work["external_id"])
                    works.append(work)
            if "has_more" not in response:
                warnings.append(f"{label} 第 {pages} 页未返回 has_more，按不完整处理")
                has_more_missing = True
                break
            if not response["has_more"]:
                finished_cleanly = True
                break
            if pages >= args.max_pages_per_list:
                warnings.append(f"{label} 达到页数上限 {args.max_pages_per_list}，列表可能不完整")
                stopped_by_budget = True
                break
            if len(works) >= args.max_items_per_list:
                warnings.append(f"{label} 达到条数上限 {args.max_items_per_list}，列表可能不完整")
                stopped_by_budget = True
                break
            next_cursor = response.get("cursor")
            cursor = int(next_cursor) if isinstance(next_cursor, (int, float)) else 0
            await asyncio.sleep(max(0, int(args.timeout)))

    if pages == 0:
        # crawler 调用循环至少执行一次，除非构造即失败——这里只可能是异常路径
        raise ScopeError("platform_error", f"{label} 未返回任何数据页")
    if not finished_cleanly and not stopped_by_budget and not has_more_missing:
        warnings.append(f"{label} 上游在 has_more=1 时提前结束，按不完整处理")
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


def write_output(path: str, doc: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, target)


def run_detail(args: argparse.Namespace) -> dict:
    """单作品详情（图文下载的数据来源）：只调 aweme/detail 只读接口，不打开网页。

    同步函数（内部自管事件循环，调用方需在工作线程中执行）。
    输出白名单字段：作品身份、图片 CDN 地址列表、标题、作者、发布时间。
    失败语义与列表模式一致（kind 明确，绝不伪装成功）。
    """
    cookie = Path(args.cookie_file).read_text(encoding="utf-8").strip()
    if not cookie:
        raise ScopeError("login_expired", "cookie 文件为空")
    aweme_id = (args.aweme_id or "").strip()
    if not aweme_id:
        raise ScopeError("bad_request", "detail 模式需要 --aweme-id")

    try:
        from f2.apps.douyin.handler import DouyinHandler
    except Exception as exc:  # pragma: no cover
        raise ScopeError("runtime_missing", f"F2 运行时不可用：{redact_sensitive_text(str(exc), max_length=160)}") from None

    _silence_f2_output()
    handler = DouyinHandler({
        "headers": {"User-Agent": _USER_AGENT},
        "cookie": cookie,
        "timeout": args.timeout,
        "max_retries": args.max_retries,
        "proxies": {"http://": None, "https://": None},
    })

    async def _fetch() -> dict:
        try:
            page = await handler.fetch_one_video(aweme_id)
        except ScopeError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一翻译（删除/私密/接口异常），消息不含作品 ID
            raise scope_error_from_exception(exc, "作品详情", "detail") from None
        raw = page._to_raw()
        status_error = _payload_status_kind(page.status_code)
        if status_error is not None:
            raise ScopeError(status_error[0], f"作品详情 {status_error[1]}")
        detail = raw.get("aweme_detail") if isinstance(raw, dict) else None
        if not isinstance(detail, dict):
            raise ScopeError("platform_error", "作品详情响应缺少 aweme_detail 字段")
        images: list[str] = []
        rows = detail.get("images") if isinstance(detail.get("images"), list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url_list = row.get("url_list")
            if isinstance(url_list, list) and url_list and isinstance(url_list[0], str):
                images.append(url_list[0])
        author = detail.get("author") if isinstance(detail.get("author"), dict) else {}
        return {
            "external_id": str(detail.get("aweme_id") or "").strip(),
            "title": (detail.get("desc") or "").strip() or None,
            "author": (author.get("nickname") or "").strip() or None,
            "published_at": iso_utc_from_unix(detail.get("create_time")),
            "images": images,
        }

    detail = asyncio.run(asyncio.wait_for(_fetch(), timeout=args.budget_seconds))
    if not detail["external_id"]:
        raise ScopeError("platform_error", "作品详情缺少 aweme_id")
    if not detail["images"]:
        raise ScopeError("not_image_post", "作品详情未包含图片地址（可能不是图集作品）")
    return {"ok": True, "detail": detail}


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    try:
        acquire_lock(args.lock_file)
    except ScopeError as exc:
        write_output(args.output, {"ok": False, "error": {"kind": exc.kind, "message": redact_sensitive_text(str(exc))}})
        return 1

    async def bounded() -> dict:
        if args.mode == "detail":
            # run_detail 内部自管事件循环：放到工作线程执行，避免嵌套 asyncio.run
            return await asyncio.wait_for(asyncio.to_thread(run_detail, args), timeout=args.budget_seconds)
        return await asyncio.wait_for(run_scopes(args), timeout=args.budget_seconds)

    try:
        result = asyncio.run(bounded())
    except ScopeError as exc:
        write_output(args.output, {"ok": False, "error": {"kind": exc.kind, "message": redact_sensitive_text(str(exc))}})
        return 1
    except asyncio.TimeoutError:
        write_output(args.output, {
            "ok": False,
            "error": {"kind": "timeout", "message": f"取数超出时间预算 {args.budget_seconds}s"},
        })
        return 1
    except Exception as exc:  # noqa: BLE001 - 兜底错误必须落盘为明确 JSON 且脱敏
        write_output(args.output, {
            "ok": False,
            "error": {
                "kind": "platform_error",
                "message": redact_sensitive_text(f"{type(exc).__name__}: {exc}"),
            },
        })
        return 1
    result["elapsed_seconds"] = round(time.monotonic() - started, 1)
    write_output(args.output, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
