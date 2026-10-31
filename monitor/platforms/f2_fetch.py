"""F2 取数脚本：读取本人抖音点赞 / 平铺收藏 / 收藏夹 / 夹内作品（只读列表接口）。

本脚本只在隔离运行时 `tools/f2-runtime/.venv` 内执行（依赖固定 commit 的 F2），
由 `monitor/platforms/douyin_f2_source.py` 以子进程方式调用，二者共同遵循
`douyin_personal_source.PersonalSourceError` 的失败语义。

硬约定：
- 只读。只调用点赞列表、平铺收藏、收藏夹列表、收藏夹作品四个列表接口；
  点赞缺少显式本人标识时，额外调用一次只读本人资料接口确认身份；
  不下载媒体、不写评论、不读取观看历史或聊天记录。
- 登录态凭据只经 `--cookie-file` 读入内存，绝不打印、不写日志、不写输出。
- 失败 ≠ 空列表：登录过期 / 接口报错 / 重试用尽 / 响应缺字段 / 上游提前结束
  都产生 kind 明确的错误或"不完整"标记，绝不把失败伪装成"没有内容"。
- 所有携带登录态的请求（本人资料、点赞/平铺收藏/收藏夹/夹内作品列表、详情）
  都不跟随重定向：3xx 明确失败，避免 Cookie 派生的 uifid 等网关标识被转发到其他来源。
- 基于原始响应严格检查：`aweme_list` 缺失或非列表按平台错误处理；
  `has_more` 缺失按不完整处理（真实 F2 Filter 会把缺失转成 False，
  不能依赖 Filter 属性语义）。
- 平台返回条数可能超过请求数量：如一页返回多于请求的 count，如实计数
  （server_oversize_pages）并全部接受，不丢数据也不当作错误。
- 整体时间预算到期时保留已完成范围与夹的结果，未完成部分明确标记
  budget_exhausted/不完整（partial），不把未请求或异常当作空/完成。
- 可选跨运行续传（提交协议的取数半程）：本进程对进度文件**只读不写**，仅在
  结果中回传本次运行结束时的哈希身份、分页游标与夹完成标记（Cookie 明文绝不
  落盘，也不落进度文件）；续传起点只认父进程确认过的状态。父进程把本批结果
  与业务数据同一事务提交 SQLite 后，才经 `write_confirmed_progress` 原子推进
  或在全部完整时清除（见 `douyin_f2_source.f2_sync_lock` / `confirm_f2_progress`）。
  因此崩溃发生在取数、结果返回、数据库提交、游标确认任意边界，都不会跳过
  未入库数据：确认之前游标不动，确认之后重放是幂等的。
- 中断一致性（硬超时/取消/清理挂起）：已读页作品**随读随挂**到结果对象
  （`consume_video_pages`/`_fetch_folder_items_raw` 的 `on_partial`），任意
  中断点下"结果中已有的作品 ⊇ 游标覆盖范围"恒成立，回传游标永远不会指向
  未返回、未提交的数据。
- 连续多个夹读取失败（非登录/限流/跳转）达到上限后停止后续夹请求，
  剩余夹标记未完成；单个 403 不解释为登录过期或限流。
- 所有错误消息与上游输出经过 redact_sensitive_text：凭据、账号标识不得
  落入日志与结果。
- 输出只含白名单字段（作品身份、标题、作者、发布时间、封面、媒体类型），
  输出文件由调用方放在临时目录，不落在仓库内。
- 列表接口（点赞/平铺收藏/收藏夹/夹内作品）使用固定版 F2 官方配置里的请求头
  （`conf.yaml` 的 `douyin.headers`：User-Agent + Referer）的安全副本：UA 与请求
  模型/Edge 签名指纹一致；配置缺失或与模型不一致时按固定 kind 明确失败。
  详情模式沿用既有已验收的请求头，不改动其路径。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:  # 作为脚本运行时 sys.path[0] 是本目录；作为模块导入时走包路径
    from douyin_personal_source import redact_sensitive_text
except ImportError:  # pragma: no cover - 脚本运行路径
    from monitor.platforms.douyin_personal_source import redact_sensitive_text

# 平台返回的 per-item status_code 中已知"需要重新登录"的码（其余码原样上报）
PAYLOAD_STATUS_LOGIN_EXPIRED = 8

# 夹内作品读取遇到这些 kind 必须整体停止（凭据/限流/跳转按安全语义报错），
# 不得降级为“单夹失败继续”。其余单夹失败只标记该夹与整体不完整。
_COLLECTS_ABORT_KINDS = frozenset({"login_expired", "rate_limited", "redirect_refused"})

# 连续多少个单夹失败后停止后续夹请求（避免大量连续 403/连接失败时逐夹重试）：
# 单个 403 仍按 platform_error 如实上报，不武断解释为登录过期或限流。
_MAX_CONSECUTIVE_FOLDER_FAILURES = 2

# 收藏夹内容连续多少轮不可读（aweme_list=null）后按「不可访问」收尾：实测
# （2026-10-31）夹存在且 total_number>0 时平台仍可能返回 null——夹内作品已被
# 作者删除/设私密，或瞬时风控；跨轮计 streak，避免同夹永久重试，也避免把
# 瞬时不可读误当空夹跳过。
_MAX_FOLDER_UNAVAILABLE_ROUNDS = 3

# 整体预算到期后的收尾宽限（用于把已完成结果落盘）；到期本身按 partial 上报。
_BUDGET_GRACE_SECONDS = 5.0


def _is_unavailable_null_list(response: dict) -> bool:
    """收藏夹作品列表的「不可访问」形状：aweme_list 键存在但非列表，且 has_more=0。

    实测（2026-10-31）夹存在、total_number>0 时平台仍可能如此返回——夹内作品
    已被作者删除/设私密，或瞬时风控。它不是空夹（空夹是 aweme_list=[]），
    也不是可重试错误（当前设计会永久重试导致轮询 partial）。
    """
    return (
        "aweme_list" in response
        and not isinstance(response.get("aweme_list"), list)
        and not response.get("has_more")
    )
_BUDGET_WARNING = "整体时间预算到期：已保留已完成结果，其余范围/夹标记不完整"

# 跨运行续传状态版本；只保存哈希身份、分页游标与完成标记，不保存 Cookie/明文标识。
# v2（提交协议）：进度文件只由父进程在业务数据持久提交后写入；旧 v1 的
# “取数即写”会让提交前崩溃窗口内的未入库数据被下一轮跳过，遇到 v1 一律作废重取。
_PROGRESS_VERSION = 2

_USER_AGENT = (
    # 仅详情（单作品直取）沿用既有已验收路径的请求头；列表接口改用固定版 F2 官方配置
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# 官方抖音站点来源；固定版 F2 的 conf.yaml 中 douyin.headers.Referer 即此值
_DOUYIN_REFERER_ORIGIN = "https://www.douyin.com"
_UA_BROWSER_TOKENS = {"Edge": "Edg/", "Chrome": "Chrome/", "Firefox": "Firefox/", "Safari": "Version/"}

KNOWN_SCOPES = ("like", "collection", "collections")

# 只读本人资料接口：固定版 F2 没有“我是谁”的官方封装，这里按同一套请求头、
# 网关处理与签名自行构造一次 GET。该能力不是 F2 官方文档接口，可能随平台变化；
# 失败必须明确上报，不得用作品作者 ID 或其他账号代替本人。
_PROFILE_SELF_ENDPOINT = "https://www.douyin.com/aweme/v1/web/user/profile/self/"
# 自动识别失败（无有效本人标识 / 响应无效 / 请求失败）的统一 kind
SELF_IDENTITY_KIND = "self_identity_unavailable"
_SEC_UID_PATTERN = re.compile(r"^[A-Za-z0-9_\-=]{16,256}$")
_SELF_IDENTITY_FAILED_MESSAGE = (
    "无法从登录态确认本人标识：请重新登录导出登录态，或在个人来源配置中填写本人 sec_user_id"
)


class ScopeError(Exception):
    """单个列表读取失败。kind 对齐 PersonalSourceError 的语义。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class RedirectRefused(Exception):
    """接口返回 3xx：拒绝跟随，避免把登录态衍生的网关标识发到其他来源。

    不继承 httpx 异常，避免被固定版 F2 的请求层按“网络错误”吞掉并重试；
    由 scope_error_from_exception 统一翻译为明确的 redirect_refused。
    """


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
    parser.add_argument("--sec-user-id", default="",
                        help="本人 sec_user_id（点赞列表需要；缺省时用同一次登录态自动确认）")
    parser.add_argument("--folder-ids", default="", help="只读这些收藏夹（缺省读全部）")
    parser.add_argument("--page-counts", type=int, default=20)
    parser.add_argument("--max-items-per-list", type=int, default=5000)
    parser.add_argument("--max-pages-per-list", type=int, default=500)
    parser.add_argument("--budget-seconds", type=int, default=900,
                        help="整体时间预算（到期保留已完成结果并将未完成范围标记不完整）")
    parser.add_argument("--progress-file", default="",
                        help="跨运行续传状态文件（只含哈希身份/游标/完成标记，不含 Cookie）")
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


def _page_delay_seconds() -> float:
    """页间限速秒数：F2_PAGE_DELAY_SECONDS 可调（默认 2.5s）+ 最多 50% 随机抖动。

    平台对背靠背分页请求敏感（2026-10-31 实测触发风控）；温和的页间隔是
    当前架构下最有效的降险手段，不涉及任何检测规避。
    """
    try:
        base = float(os.getenv("F2_PAGE_DELAY_SECONDS", "0"))
    except ValueError:
        base = 2.5
    return max(0.0, base) * (1.0 + random.random() * 0.5)


async def consume_video_pages(make_gen, *, label: str, requested_page_counts: int,
                              max_pages: int, max_items: int, warnings: list,
                              deadline: float | None = None,
                              on_page=None, on_partial=None) -> tuple[list[dict], dict]:
    """消费 F2 分页异步生成器：逐页基于原始响应严格校验并提取作品。

    - `aweme_list` 缺失/非列表 → ScopeError(platform_error)：失败不冒充空列表。
    - `has_more` 键缺失 → 记警告并按不完整结束（真实 F2 Filter 把缺失转 False）。
    - 上游未产出任何页 / 提前结束 → 显式错误或不完整，绝不当成功。
    - 整体截止到期：不再发起下一页，返回已读页与不完整标记；`on_page` 在每页
      有下一页时回调，用于持久化游标，供下一次有限预算运行续传。
    - `on_partial` 在每页游标推进后回调（works 为存活列表）：把已读页作品
      **增量挂到调用方的结果对象上**。硬超时/取消可能落在页间或收尾清理中
      （真实 F2 生成器吞掉一次取消再挂起），若等函数返回才挂载，已读页作品
      会随帧销毁而回传游标已前进——确认后下一轮将从没有已提交数据的位置
      续传，永久跳过它们。增量挂载保证任意中断点下结果与游标严格一致。
    """
    works: list[dict] = []
    seen: set[str] = set()
    skipped: list[dict] = []
    pages = 0
    oversize_pages = 0
    finished_cleanly = False
    stopped_by_budget = False
    budget_stop = False
    has_more_missing = False

    agen = make_gen(None)
    try:
        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    budget_stop = True
                    warnings.append(f"{label} 整体时间预算到期，按不完整处理")
                    break
                try:
                    page = await asyncio.wait_for(agen.__anext__(), timeout=remaining)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    budget_stop = True
                    warnings.append(f"{label} 整体时间预算到期，按不完整处理")
                    break
            else:
                try:
                    page = await agen.__anext__()
                except StopAsyncIteration:
                    break

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
            if on_page is not None:
                try:
                    on_page(raw)
                except Exception:  # noqa: BLE001 - 续传写入失败不丢弃已读数据
                    warnings.append(f"{label} 续传状态写入失败，本次继续按不完整处理")
            if on_partial is not None:
                # 增量挂载紧随游标推进：任意中断点下结果与游标严格一致
                try:
                    on_partial(works, pages, oversize_pages)
                except Exception:  # noqa: BLE001 - 挂载失败不影响取数
                    warnings.append(f"{label} 已读页结果挂载失败，本次继续按不完整处理")
            # 页间限速：随机抖动的温和间隔，避免背靠背突发请求触发风控；
            # 受整体预算约束（绝不睡过截止后继续发请求）。
            if pages < max_pages and len(works) < max_items:
                delay = _page_delay_seconds()
                if deadline is not None:
                    delay = min(delay, max(0.0, deadline - time.monotonic()))
                if delay > 0:
                    await asyncio.sleep(delay)
            if pages >= max_pages:
                warnings.append(f"{label} 达到页数上限 {max_pages}，列表可能不完整")
                stopped_by_budget = True
                break
            if len(works) >= max_items:
                warnings.append(f"{label} 达到条数上限 {max_items}，列表可能不完整")
                stopped_by_budget = True
                break
    finally:
        try:
            await agen.aclose()
        except Exception:  # noqa: BLE001 - 关闭失败不影响已读取结果
            pass
    if pages == 0:
        if budget_stop:
            # 预算在首页前/首页等待中到期：明确不完整，不冒充空列表
            return [], {"pages": 0, "items": 0, "complete": False,
                        "server_oversize_pages": 0, "budget_exhausted": True}
        raise ScopeError("platform_error", f"{label} 未返回任何数据页（上游提前结束）")
    if not finished_cleanly and not stopped_by_budget and not budget_stop and not has_more_missing:
        # 生成器耗尽但最后一页仍声称有更多：上游提前结束，按不完整处理
        warnings.append(f"{label} 上游在 has_more=1 时提前结束，按不完整处理")
    meta = {
        "pages": pages,
        "items": len(works),
        "complete": finished_cleanly,
        "server_oversize_pages": oversize_pages,
    }
    if budget_stop:
        meta["budget_exhausted"] = True
    if skipped:
        meta["skipped"] = skipped[:20]
        meta["skipped_count"] = len(skipped)
    return works, meta


def scope_error_from_exception(exc: Exception, label: str, scope: str) -> ScopeError:
    """把 F2 异常翻译成明确 kind；消息脱敏（不落凭据/账号标识）。

    - RedirectRefused → redirect_refused：3xx 拒绝跟随，不把登录态标识外传。
    - APIRetryExhaustedError：点赞列表为"未登录或未公开"（平台不区分两者）；
      其他列表按平台错误处理。
    - APIUnauthorizedError → login_expired；APIRateLimitError → rate_limited。
    """
    if isinstance(exc, RedirectRefused):
        return ScopeError("redirect_refused", f"{label} 接口返回重定向，已拒绝跟随（避免登录态标识外传）")
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


def official_list_headers() -> dict[str, str]:
    """复制固定版 F2 的官方抖音请求头供列表接口使用（不改共享配置）。

    固定版 F2 的官方 CLI 在构造 kwargs 时把 `conf.yaml` 的 `douyin.headers`
    （User-Agent + `Referer: https://www.douyin.com/`）合并进请求头，且
    `ABogusManager` 固定按该版本请求模型的 Edge 指纹签名。项目此前只发 Mac
    Chrome UA、不带 Referer，与请求模型的浏览器参数和签名指纹不一致。

    这里只读取并复制配置：返回新字典，绝不修改 `ClientConfManager` 的共享字典；
    代理、超时、重试仍由调用方 kwargs 决定。必需项缺失、类型非法、Referer 不是
    抖音 HTTPS 站点，或 UA 与模型浏览器参数不一致时，按固定 kind 明确失败，
    不静默退回不一致的默认请求头。
    """
    try:
        from f2.apps.douyin.utils import ClientConfManager
    except Exception as exc:  # pragma: no cover - 仅隔离运行时损坏
        raise ScopeError(
            "runtime_missing",
            f"F2 运行时不可用：{redact_sensitive_text(str(exc), max_length=160)}",
        ) from None
    try:
        configured = ClientConfManager.headers()
        browser = ClientConfManager.brm_browser()
    except Exception:  # noqa: BLE001 - 不回显配置读取异常文本
        raise ScopeError("runtime_config_missing", "无法读取固定版 F2 官方抖音请求头配置") from None
    if not isinstance(configured, dict):
        raise ScopeError("runtime_config_missing", "固定版 F2 官方抖音请求头配置不是对象")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in configured.items()):
        raise ScopeError("runtime_config_missing", "固定版 F2 官方抖音请求头配置含非字符串项")
    headers = dict(configured)  # 安全副本：不把引用传出去，避免被下游改写配置
    ua = headers.get("User-Agent", "").strip()
    referer = headers.get("Referer", "").strip()
    if not ua:
        raise ScopeError("runtime_config_missing", "固定版 F2 官方抖音请求头缺少 User-Agent")
    if not referer:
        raise ScopeError("runtime_config_missing", "固定版 F2 官方抖音请求头缺少 Referer")
    if referer.rstrip("/").lower() != _DOUYIN_REFERER_ORIGIN:
        raise ScopeError("runtime_config_missing", "固定版 F2 官方 Referer 不是抖音 HTTPS 站点")
    if isinstance(browser, dict):
        name = str(browser.get("name") or "")
        version = str(browser.get("version") or "")
        token = _UA_BROWSER_TOKENS.get(name)
        if token and version and (token not in ua or version not in ua):
            raise ScopeError(
                "runtime_config_missing",
                "固定版 F2 官方 User-Agent 与请求模型浏览器参数不一致",
            )
    headers["User-Agent"] = ua
    headers["Referer"] = referer
    return headers


def extract_self_sec_uid(response) -> str | None:
    """从本人资料原始响应中提取本人标识；不合格一律 None（由调用方明确失败）。

    只认**明确成功**（`status_code` 为整数 0）的 `user.sec_uid`，且必须是形如抖音
    sec_uid 的无空白短标识。状态缺失/null/类型异常、作品作者 ID、其他账号字段与
    非字符串值都不采信。
    """
    if not isinstance(response, dict):
        return None
    status = response.get("status_code")
    if isinstance(status, bool) or not isinstance(status, int) or status != 0:
        return None
    user = response.get("user")
    if not isinstance(user, dict):
        return None
    value = user.get("sec_uid")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _SEC_UID_PATTERN.match(value) else None


def _safe_douyin_crawler():
    """返回禁止跟随重定向的 DouyinCrawler 子类（进程内构造，不改共享 F2 运行时）。

    固定版 F2 的 BaseCrawler 在 GET/POST 中固定传 `follow_redirects=True`：任何 3xx
    都会把 Cookie 与 `uifid` 等登录态派生网关标识转发给跳转目标。这里在客户端调用
    层强制 `follow_redirects=False`，并把 3xx 直接转成 RedirectRefused（明确失败），
    不改变官方 UA、签名、超时/重试与其他失败语义。
    """
    from f2.apps.douyin.crawler import DouyinCrawler

    class _NoRedirectClient:
        """只包住 BaseCrawler 用到的 get/post/aclose，导向请求不跟随跳转。"""

        def __init__(self, client) -> None:
            self._client = client

        async def _request(self, method: str, url: str, **kwargs):
            kwargs["follow_redirects"] = False
            response = await self._client.request(method, url, **kwargs)
            if 300 <= response.status_code < 400:
                raise RedirectRefused("接口返回重定向，拒绝跟随")
            return response

        async def get(self, url, **kwargs):
            return await self._request("GET", url, **kwargs)

        async def post(self, url, **kwargs):
            return await self._request("POST", url, **kwargs)

        async def aclose(self) -> None:
            await self._client.aclose()

    class _SafeDouyinCrawler(DouyinCrawler):
        @property
        def aclient(self):
            # 兼容已注入的自定义 client（测试/诊断）：统一在调用层禁止跳转
            return _NoRedirectClient(super().aclient)

    return _SafeDouyinCrawler


@contextmanager
def _refuse_redirects_for_handler():
    """本次列表取数期间，把 handler 内部构造的 crawler 换成禁止重定向的子类。

    只替换进程内模块引用，退出时恢复；不写共享 F2 运行时，也不改变详情路径。
    """
    from f2.apps.douyin import handler as handler_module

    safe = _safe_douyin_crawler()
    original = getattr(handler_module, "DouyinCrawler", None)
    if original is None:  # pragma: no cover - 固定版运行时必有该符号
        yield
        return
    handler_module.DouyinCrawler = safe
    try:
        yield
    finally:
        handler_module.DouyinCrawler = original


def _self_identity_error(exc: Exception) -> ScopeError:
    """把本人资料请求异常翻译为固定安全文案（绝不回显 URL/响应/异常文本/标识）。"""
    if isinstance(exc, RedirectRefused):
        return ScopeError("redirect_refused", "本人资料接口返回重定向，已拒绝跟随（避免登录态标识外传）")
    try:
        from f2.exceptions.api_exceptions import (
            APIRateLimitError,
            APIUnauthorizedError,
        )
    except ImportError:  # pragma: no cover - 非 F2 运行时（测试桩模块）
        APIRateLimitError = APIUnauthorizedError = ()
    if APIUnauthorizedError and isinstance(exc, APIUnauthorizedError):
        return ScopeError("login_expired", "登录态无效，无法确认本人标识；请重新登录导出登录态")
    if APIRateLimitError and isinstance(exc, APIRateLimitError):
        return ScopeError("rate_limited", "本人资料接口触发平台限流，无法确认本人标识")
    return ScopeError(SELF_IDENTITY_KIND, _SELF_IDENTITY_FAILED_MESSAGE)


async def resolve_self_sec_user_id(handler) -> str:
    """用同一次已配置登录态确认本人 sec_user_id（只在点赞缺少显式配置时调用）。

    固定版 F2 没有本人资料接口封装，这里复用列表接口的官方请求头、网关请求头、
    ABogus 签名与 crawler 超时/重试/代理，向固定抖音 HTTPS 路径发一次只读 GET。
    返回值只在本进程内传给点赞列表，不写入结果、配置、日志或进程参数。
    任何失败都抛 ScopeError（kind 明确、文案固定），绝不返回空串或用作者 ID 代替。
    """
    try:
        from f2.apps.douyin.model import BaseRequestModel
        from f2.apps.douyin.utils import ABogusManager

        crawler_cls = _safe_douyin_crawler()
    except Exception:  # noqa: BLE001 - 隔离运行时损坏，不回显异常文本
        raise ScopeError("runtime_missing", "F2 运行时不可用：缺少本人资料接口依赖") from None

    try:
        class _SelfProfileRequest(BaseRequestModel):
            """固定版 F2 请求模型默认参数（浏览器指纹与签名一致），不含任何身份字段。"""

        params = _SelfProfileRequest().model_dump()
    except Exception as exc:  # noqa: BLE001 - 含 msToken 初始化失败，统一固定文案
        raise _self_identity_error(exc) from None

    try:
        async with crawler_cls(handler.kwargs) as crawler:
            crawler_headers = crawler.headers if isinstance(crawler.headers, dict) else {}
            user_agent = str(crawler_headers.get("User-Agent") or "")
            if not user_agent:
                raise ScopeError("runtime_config_missing", "固定版 F2 请求头缺少 User-Agent，无法确认本人标识")
            endpoint = ABogusManager.model_2_endpoint(
                user_agent, _PROFILE_SELF_ENDPOINT, params)
            response = await crawler._fetch_get_json(endpoint)
    except ScopeError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一固定文案，不输出原始异常
        raise _self_identity_error(exc) from None

    if not isinstance(response, dict):
        raise ScopeError(SELF_IDENTITY_KIND, _SELF_IDENTITY_FAILED_MESSAGE)
    status = response.get("status_code")
    if isinstance(status, bool) or not isinstance(status, int):
        # 缺失/null/类型异常都不能当作已确认成功
        raise ScopeError(SELF_IDENTITY_KIND, _SELF_IDENTITY_FAILED_MESSAGE)
    if status != 0:
        kind, _ = _payload_status_kind(status)
        if kind == "login_expired":
            raise ScopeError("login_expired", "登录态无效，无法确认本人标识；请重新登录导出登录态")
        raise ScopeError("platform_error", "本人资料接口返回异常状态，无法确认本人标识")
    sec_user_id = extract_self_sec_uid(response)
    if not sec_user_id:
        raise ScopeError(SELF_IDENTITY_KIND, _SELF_IDENTITY_FAILED_MESSAGE)
    return sec_user_id


def _requested_scopes(args: argparse.Namespace) -> list[str]:
    return [s.strip() for s in str(getattr(args, "scopes", "")).split(",") if s.strip()]


def _identity_hash(value: str) -> str:
    """进度文件只存哈希：账号、收藏夹 ID 不以明文落盘/落日志。"""
    return hashlib.sha256(f"f2-progress:{value}".encode("utf-8")).hexdigest()[:32]


def _progress_fingerprint(*, sec_user_id: str, scopes: list[str], folder_ids: str,
                          page_counts: int, max_items: int, max_pages: int,
                          cookie_hash: str = "") -> str:
    """身份/会话/范围/过滤条件指纹：任一变化都不得沿用旧进度。

    `cookie_hash` 是登录态的哈希（不落明文），用于在没有手动 sec_user_id 时也能
    区分不同登录账号；Cookie 本身绝不写入进度文件。
    """
    folders = sorted(_identity_hash(f.strip()) for f in str(folder_ids).split(",") if f.strip())
    payload = {
        "account": _identity_hash(sec_user_id) if sec_user_id else "",
        "session": cookie_hash,
        "scopes": sorted(scopes),
        "folders": folders,
        "page_counts": int(page_counts),
        "max_items": int(max_items),
        "max_pages": int(max_pages),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _load_progress(path: str, fingerprint: str) -> dict:
    """读取续传状态；缺失/损坏/指纹不符一律视为无进度（不误用旧游标）。

    文件里只可能是父进程确认过（对应业务数据已持久提交）的状态；旧 v1
    “取数即写”协议的状态同样按版本不符作废，从零重取。
    """
    if not path:
        return {}
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict) or doc.get("version") != _PROGRESS_VERSION:
        return {}
    if doc.get("fingerprint") != fingerprint:
        return {}
    scopes = doc.get("scopes")
    return scopes if isinstance(scopes, dict) else {}


def _save_progress(path: str, fingerprint: str, scopes: dict, *,
                   version: int = _PROGRESS_VERSION) -> bool:
    """原子写入续传状态（0600）；返回是否真正落盘。

    只允许确认步（write_confirmed_progress）调用：取数进程对进度文件只读，
    绝不在业务数据提交前推进游标。失败（临时写入/os.replace 的 OSError）时
    目标保持旧确认状态、临时文件清理后返回 False——调用方必须如实上报，
    不得当作确认成功；下一轮从旧确认游标幂等重放。
    """
    if not path:
        return False
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({
            "version": version,
            "fingerprint": fingerprint,
            "scopes": scopes,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # pragma: no cover - 部分平台不支持
            pass
        os.replace(tmp, target)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)  # 不残留半份状态
        except OSError:  # pragma: no cover - 清理失败不影响失败上报
            pass
        return False


def write_confirmed_progress(path: str, resume) -> bool:
    """提交协议确认步：业务数据已持久提交后，把取数回传的续传状态落盘。

    返回值=确认是否真正成功：
    - 形状不完整（缺 version/fingerprint/scopes）→ False 且不动文件；
    - `all_complete` 为真 → 清除状态文件，下一轮重新从头完整同步（与既有
      "全部完整才清理"语义一致）；**清除失败（OSError）返回 False**——旧状态
      残留时调用方必须如实上报，不得当作完整同步；
    - 否则原子覆盖为最新已确认游标（tmp + os.replace，0600）；写入/替换失败
      返回 False，目标保持旧确认状态，下一轮从旧游标幂等重放。
    """
    if not path or not isinstance(resume, dict):
        return False
    version = resume.get("version")
    fingerprint = resume.get("fingerprint")
    scopes = resume.get("scopes")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return False
    if not isinstance(fingerprint, str) or not fingerprint:
        return False
    if not isinstance(scopes, dict):
        return False
    if resume.get("all_complete") is True:
        return _clear_progress(path)
    return _save_progress(path, fingerprint, scopes, version=version)


def _clear_progress(path: str) -> bool:
    """清除续传状态文件；返回是否确认已不存在（ unlink 失败返回 False）。"""
    if not path:
        return False
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _page_cursor(raw: dict, scope: str) -> int | None:
    """从原始响应取下一页游标（点赞 max_cursor / 平铺收藏 cursor）。"""
    value = raw.get("max_cursor") if scope == "like" else raw.get("cursor")
    if value is None and scope != "like":
        value = raw.get("max_cursor")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _complete_scope_block(scope: str) -> dict:
    """续传时跳过上一轮已完成范围：complete=True 但本次没有新作品。"""
    if scope == "collections":
        return {
            "pages": 0, "folders": [], "folders_complete": True,
            "folder_items": {}, "folder_incomplete": [], "folder_failures": [],
            "complete": True, "resumed": True,
        }
    return {
        "pages": 0, "items": 0, "complete": True,
        "server_oversize_pages": 0, "works": [], "resumed": True,
    }


def _pass_complete(result: dict, scopes: list[str]) -> bool:
    """只有每个被请求范围都确实完整，才允许清理续传状态。"""
    for scope in scopes:
        block = result.get("collects" if scope == "collections" else scope)
        if not isinstance(block, dict) or block.get("complete") is not True:
            return False
    return True


def _mark_scope_budget_exhausted(result: dict, scope: str, warnings: list, *,
                                folder_ids: str = "") -> None:
    """预算到期时标记未完成范围：complete=False，绝不冒充空列表或已完成。"""
    if scope == "collections":
        block = result.get("collects")
        if isinstance(block, dict) and block.get("complete") is True:
            return
        if not isinstance(block, dict):
            block = {
                "pages": 0, "folders": [], "folders_complete": False,
                "folder_items": {}, "folder_incomplete": [], "folder_failures": [],
                "complete": False,
            }
            result["collects"] = block
        block["complete"] = False
        block["budget_exhausted"] = True
        wanted = {f.strip() for f in folder_ids.split(",") if f.strip()}
        items = block.setdefault("folder_items", {})
        labels = block.setdefault("folder_incomplete", [])
        failures = block.setdefault("folder_failures", [])
        for ordinal, row in enumerate(block.get("folders") or [], start=1):
            folder_id = row.get("folder_id")
            if wanted and folder_id not in wanted:
                continue
            label = f"folder#{ordinal}"
            if label in labels or folder_id in items:
                continue
            items[folder_id] = {"pages": 0, "items": 0, "complete": False,
                                "budget_exhausted": True}
            labels.append(label)
            failures.append({"folder": label, "kind": "budget_exhausted"})
    else:
        block = result.get(scope)
        if isinstance(block, dict) and block.get("complete") is True:
            return
        if not isinstance(block, dict):
            block = {"pages": 0, "items": 0, "complete": False,
                     "server_oversize_pages": 0, "works": []}
            result[scope] = block
        block["complete"] = False
        block["budget_exhausted"] = True
    if _BUDGET_WARNING not in warnings:
        warnings.append(_BUDGET_WARNING)


def _finalize_budget_partial(args: argparse.Namespace, result: dict) -> dict:
    """把已取得的部分结果收尾为 ok/partial，未完成范围明确标记不完整。"""
    warnings = result.setdefault("warnings", [])
    for scope in _requested_scopes(args):
        _mark_scope_budget_exhausted(result, scope, warnings,
                                     folder_ids=str(getattr(args, "folder_ids", "") or ""))
    result["ok"] = True
    result["partial"] = True
    result["budget_exhausted"] = True
    return result


def _has_partial_data(result) -> bool:
    """是否已取得任何可导入结果（用于区分“超时但有数据”与“完全没读到”）。"""
    if not isinstance(result, dict):
        return False
    for scope in ("like", "collection"):
        block = result.get(scope)
        if isinstance(block, dict) and (block.get("complete") is True or block.get("works")):
            return True
    block = result.get("collects")
    if isinstance(block, dict):
        if block.get("folder_items") or block.get("pages"):
            return True
    return False


def _budget_grace(args: argparse.Namespace) -> float:
    """硬超时宽限：给内部截止后的收尾与落盘留出时间，但不无限延长。"""
    timeout = float(getattr(args, "timeout", 0) or 0)
    retries = max(1, int(getattr(args, "max_retries", 1) or 1))
    return max(_BUDGET_GRACE_SECONDS, timeout * (retries + 1))


async def run_scopes(args: argparse.Namespace, *, progress: dict | None = None,
                     deadline: float | None = None) -> dict:
    cookie = Path(args.cookie_file).read_text(encoding="utf-8").strip()
    if not cookie:
        raise ScopeError("login_expired", "cookie 文件为空")

    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    unknown = [s for s in scopes if s not in KNOWN_SCOPES]
    if unknown:
        raise ScopeError("bad_scope", f"未知 scope: {unknown}")
    if not scopes:
        raise ScopeError("bad_scope", "scopes 为空")

    # F2 只在隔离运行时可用，延迟导入；失败即运行时缺失，不与平台错误混淆。
    try:
        from f2.apps.douyin.handler import DouyinHandler
    except Exception as exc:  # pragma: no cover - 隔离环境本身损坏
        raise ScopeError("runtime_missing", f"F2 运行时不可用：{redact_sensitive_text(str(exc), max_length=160)}") from None

    _silence_f2_output()

    import importlib.metadata as md
    f2_version = md.version("f2")

    kwargs = {
        # 列表接口使用固定版 F2 官方请求头（UA 与请求模型/Edge 签名指纹一致，且含 Referer）
        "headers": official_list_headers(),
        "cookie": cookie,
        "timeout": args.timeout,
        "max_retries": args.max_retries,
        "proxies": {"http://": None, "https://": None},
    }
    handler = DouyinHandler(kwargs)
    warnings: list[str] = []
    result: dict = {"ok": True, "warnings": warnings, "runtime": {"f2_version": f2_version}}
    if progress is not None:
        progress["result"] = result  # 供硬超时收尾读取已完成部分
    if deadline is None:
        deadline = time.monotonic() + max(0, args.budget_seconds)
    page_counts = args.page_counts
    # 显式配置优先（既有行为不变）；缺失时才在请求点赞前用同一次登录态自动确认。
    like_sec_user_id = args.sec_user_id.strip()

    progress_path = str(getattr(args, "progress_file", "") or "")
    progress_state: dict = {}
    fingerprint = ""
    if progress_path:
        resume_candidate = Path(progress_path).is_file()
        if "like" in scopes and not like_sec_user_id and time.monotonic() < deadline:
            # 续传与账号绑定：点赞需要自动识别时先确认本人身份，保证指纹用真实账号
            like_sec_user_id = await resolve_self_sec_user_id(handler)
        fingerprint = _progress_fingerprint(
            sec_user_id=like_sec_user_id if "like" in scopes else "",
            scopes=scopes,
            folder_ids=str(getattr(args, "folder_ids", "") or ""),
            page_counts=page_counts,
            max_items=args.max_items_per_list,
            max_pages=args.max_pages_per_list,
            cookie_hash=_identity_hash(cookie),
        )
        if resume_candidate:
            # 只读：文件里只有父进程确认过（对应数据已持久提交）的状态；
            # 指纹不符/损坏一律视为无进度，从零重取，不误用旧游标。
            progress_state = _load_progress(progress_path, fingerprint)
        # 提前挂上回传状态：取数进程不写进度文件（提交协议），只把运行结束时的
        # 游标/完成标记回传给父进程，由父进程在业务数据持久提交后经
        # write_confirmed_progress 推进/清除。scopes 是存活引用：硬超时收尾的
        # partial 也携带最新游标，正常路径在结尾刷新 all_complete。
        result["resume"] = {
            "version": _PROGRESS_VERSION,
            "fingerprint": fingerprint,
            "all_complete": False,
            "scopes": progress_state,
        }
    else:
        # 显式声明刻意禁用续传，父进程据此区分合法缺失与协议异常。
        result["resume_disabled"] = True

    async def fetch_one(label: str, scope: str, make_gen, *, on_page=None) -> None:
        def _attach_partial(works, pages, oversize_pages) -> None:
            # 已读页作品随读随挂（works 为存活列表，后续页继续追加）：
            # 硬超时/取消在任意点打断时，结果中已有与游标一致的数据
            result[scope] = {"pages": pages, "items": len(works), "complete": False,
                             "server_oversize_pages": oversize_pages, "works": works}

        try:
            works, meta = await consume_video_pages(
                make_gen, label=label, requested_page_counts=page_counts,
                max_pages=args.max_pages_per_list, max_items=args.max_items_per_list,
                warnings=warnings, deadline=deadline, on_page=on_page,
                on_partial=_attach_partial,
            )
        except ScopeError:
            raise
        except Exception as exc:  # noqa: BLE001 - 翻译为明确 kind 并脱敏
            raise scope_error_from_exception(exc, label, scope) from None
        result[scope] = meta | {"works": works}

    with _refuse_redirects_for_handler():
        for index, scope in enumerate(scopes):
            if time.monotonic() >= deadline:
                # 预算到期：未开始/未完成的范围明确标记不完整，已完成的保持原样
                for remaining in scopes[index:]:
                    _mark_scope_budget_exhausted(
                        result, remaining, warnings,
                        folder_ids=str(getattr(args, "folder_ids", "") or ""))
                result["partial"] = True
                result["budget_exhausted"] = True
                break
            if scope == "like":
                info = progress_state.get("like") if progress_path else None
                if isinstance(info, dict) and info.get("complete") is True:
                    result["like"] = _complete_scope_block("like")
                else:
                    start_cursor = int(info.get("cursor") or 0) if isinstance(info, dict) else 0
                    def _like_page(raw, key="like"):
                        # 只记内存游标：确认落盘由父进程在数据提交后统一完成
                        cursor = _page_cursor(raw, key)
                        if cursor is not None:
                            progress_state[key] = {"complete": False, "cursor": cursor}
                    if not like_sec_user_id:
                        # 只在本次请求点赞且没有手动指定时查询一次；失败明确上报，不当作空点赞
                        like_sec_user_id = await resolve_self_sec_user_id(handler)
                    await fetch_one(
                        "like", "like",
                        lambda h: handler.fetch_user_like_videos(
                            like_sec_user_id, max_cursor=start_cursor,
                            page_counts=page_counts, max_counts=args.max_items_per_list),
                        on_page=_like_page)
                    if result["like"].get("complete") is True:
                        progress_state["like"] = {"complete": True, "cursor": 0}
            elif scope == "collection":
                info = progress_state.get("collection") if progress_path else None
                if isinstance(info, dict) and info.get("complete") is True:
                    result["collection"] = _complete_scope_block("collection")
                else:
                    start_cursor = int(info.get("cursor") or 0) if isinstance(info, dict) else 0
                    def _collection_page(raw, key="collection"):
                        # 只记内存游标：确认落盘由父进程在数据提交后统一完成
                        cursor = _page_cursor(raw, key)
                        if cursor is not None:
                            progress_state[key] = {"complete": False, "cursor": cursor}
                    await fetch_one(
                        "collection", "collection",
                        lambda h: handler.fetch_user_collection_videos(
                            max_cursor=start_cursor, page_counts=page_counts,
                            max_counts=args.max_items_per_list),
                        on_page=_collection_page)
                    if result["collection"].get("complete") is True:
                        progress_state["collection"] = {"complete": True, "cursor": 0}
            elif scope == "collections":
                info = progress_state.get("collections") if progress_path else None
                if isinstance(info, dict) and info.get("complete") is True:
                    result["collects"] = _complete_scope_block("collections")
                else:
                    try:
                        await fetch_collections_scope(
                            handler, args, result, warnings, deadline=deadline,
                            scope_progress=(progress_state.setdefault("collections", {})
                                            if progress_path else None))
                    except ScopeError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        raise scope_error_from_exception(exc, "collections", "collections") from None
            block = result.get("collects" if scope == "collections" else scope)
            if isinstance(block, dict) and block.get("budget_exhausted") is True:
                result["partial"] = True
                result["budget_exhausted"] = True
            await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    if progress_path and fingerprint:
        # 刷新回传状态：全部完整与否由父进程确认时判断——确认步在数据持久提交后
        # 清除文件（下一轮重新从头开始，避免永久跳过新内容）或推进游标。
        result["resume"]["all_complete"] = _pass_complete(result, scopes)
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


async def fetch_collections_scope(handler, args, result: dict, warnings: list, *,
                                  deadline: float | None = None,
                                  scope_progress: dict | None = None) -> None:
    """收藏夹列表 + 逐夹作品。单夹读取失败不丢弃其他已完成结果。

    完整性 = 夹列表分页走完 且 每个夹的作品分页走完；任何一处未走完都如实上报。
    个别夹读取失败（如响应缺 aweme_list）只标记该夹与整体不完整并继续其他夹，
    且该夹的续传游标回滚到进入前状态（本次已读页的作品随异常丢弃，若游标仍
    前进，下一轮会从未入库位置续传而漏掉它们）；
    连续失败达到上限时停止后续夹请求并把剩余夹标记未完成；
    登录失效 / 限流 / 重定向按安全语义整体停止报错；
    时间预算到期时保留已完成结果，未完成的夹明确标记不完整。
    绝不把异常响应或未请求当作空夹或完成。
    目录跨轮续传（水位线）：目录页游标保存在续传状态（list_cursor，仅数字、
    不含夹标识）。水位线只在“该页发现的夹全部处理到终结”后推进到该页响应
    游标；页内还有未完成/失败夹、游标缺失或无效、预算中断时水位线停留，
    下一轮从水位线重读该页（已完成夹按哈希身份瞬时跳过，重放幂等）。目录
    从未读到 has_more=0 时如实不完整，不空转推进、不虚报完成。目录页存在
    非对象或缺少/空收藏夹标识的记录时同样如实不完整：该页水位线停留、
    下轮重读重试，页内有效夹照常处理保全，绝不静默越过未知项或虚报完整。
    平台在两轮
    之间把新收藏夹插到水位线之前时，该夹要等本轮走完、状态清除后的下一轮
    完整同步才会被发现（与点赞/收藏续传的平台变更容忍度一致）。
    """
    folders: dict[str, dict] = {}
    pages = 0
    folders_complete = False
    list_budget_exhausted = False
    # 目录水位线：续传状态里的目录页游标（只存数字；缺失/无效一律从 0 完整重读）
    list_cursor = 0
    if scope_progress is not None:
        stored = scope_progress.get("list_cursor")
        if isinstance(stored, int) and not isinstance(stored, bool) and stored > 0:
            list_cursor = int(stored)
    request_cursor = list_cursor
    # 目录页边界：起始游标、该页发现的夹、响应游标（None=缺失/无效/列表结束）、
    # 是否存在缺少收藏夹标识的记录（invalid=True 的页水位线不得越过）
    page_boundaries: list[dict] = []
    list_has_invalid = False
    async for page in handler.fetch_user_collects(
        max_cursor=request_cursor,
        page_counts=args.page_counts, max_counts=args.max_items_per_list,
    ):
        pages += 1
        page_start = request_cursor
        raw = page._to_raw()
        status_error = _payload_status_kind(page.status_code)
        if status_error is not None:
            raise ScopeError(status_error[0], f"收藏夹列表 {status_error[1]}")
        if not isinstance(raw, dict) or "collects_list" not in raw or not isinstance(raw.get("collects_list"), list):
            raise ScopeError("platform_error", "收藏夹列表响应缺少 collects_list 字段")
        page_folder_ids: list[str] = []
        page_invalid = 0
        for row in raw["collects_list"]:
            folder_id = ""
            if isinstance(row, dict):
                folder_id = str(row.get("collects_id") or "").strip()
            if not folder_id:
                # 非对象或缺少/空收藏夹标识的未知项：绝不能静默越过——该页按
                # 不完整处理、水位线停留，下一轮重读重试（不输出原始响应内容）
                page_invalid += 1
                continue
            page_folder_ids.append(folder_id)
            # 固定版 F2 的夹内数量字段是 total_number（0 为合法值）；
            # collects_item_count/item_count 仅作旧响应兼容回退。
            item_count = row.get("total_number")
            if item_count is None:
                item_count = row.get("collects_item_count")
            if item_count is None:
                item_count = row.get("item_count")
            folders[folder_id] = {
                "folder_id": folder_id,
                "name": (row.get("collects_name") or "").strip() or None,
                "item_count": item_count,
            }
        boundary = {"start": page_start, "folders": page_folder_ids, "next": None,
                    "invalid": page_invalid > 0}
        if page_invalid:
            warnings.append(
                f"收藏夹列表第 {pages} 页存在 {page_invalid} 条缺少收藏夹标识的记录，"
                "该页按不完整处理，目录无法跨过该页推进")
            list_has_invalid = True
        if "has_more" not in raw:
            warnings.append("收藏夹列表未返回 has_more，按不完整处理")
            page_boundaries.append(boundary)
            break
        if not raw["has_more"]:
            folders_complete = True
            page_boundaries.append(boundary)
            break
        next_cursor = raw.get("cursor")
        if isinstance(next_cursor, bool) or not isinstance(next_cursor, (int, float)) \
                or int(next_cursor) <= 0:
            # 响应游标缺失/无效：无法安全推进目录水位线，明确不完整并重读本页
            warnings.append("收藏夹列表未返回有效 cursor，目录无法跨轮推进，按不完整处理")
            page_boundaries.append(boundary)
            break
        request_cursor = int(next_cursor)
        boundary["next"] = request_cursor
        page_boundaries.append(boundary)
        if deadline is not None and time.monotonic() >= deadline:
            warnings.append("整体时间预算到期：收藏夹列表未读取完，按不完整处理")
            list_budget_exhausted = True
            break
        if pages >= args.max_pages_per_list:
            warnings.append("收藏夹列表达到页数上限，可能不完整")
            break
    if pages == 0:
        raise ScopeError("platform_error", "收藏夹列表未返回任何数据页（上游提前结束）")
    if list_has_invalid:
        # 目录页存在无法解释的记录：夹清单不完整——即使读到了 has_more=0 也
        # 绝不虚报完整，下一轮从异常页起始重读（有效夹已按哈希进度保全）。
        folders_complete = False

    wanted = {f.strip() for f in args.folder_ids.split(",") if f.strip()}
    # 固定按收藏夹 ID 排序后编号：结果 folders、folder#N 标签与续传判定使用同一顺序，
    # 避免平台返回顺序（如 Z/A/M）与排序顺序（A/M/Z）不一致导致漏标。
    ordered_folders = sorted(folders.values(), key=lambda f: f["folder_id"])
    folder_items: dict[str, dict] = {}
    folder_incomplete: list[str] = []
    folder_failures: list[dict] = []
    all_items_complete = folders_complete
    first = True
    consecutive_failures = 0
    block = {
        "pages": pages,
        "folders": ordered_folders,
        "folders_complete": folders_complete,
        "folder_items": folder_items,
        "folder_incomplete": folder_incomplete,
        "folder_failures": folder_failures,
        "complete": False,
    }
    if list_budget_exhausted:
        block["budget_exhausted"] = True
    result["collects"] = block  # 尽早挂到结果上：硬中断时已完成夹仍可保留
    folder_progress = None
    if scope_progress is not None:
        folder_progress = scope_progress.get("folders")
        if not isinstance(folder_progress, dict):
            folder_progress = {}
            scope_progress["folders"] = folder_progress

    def _mark_folder_incomplete(ordinal: int) -> None:
        """把夹序号加入不完整清单（去重：增量挂载可能已提前标记）。"""
        label = f"folder#{ordinal}"
        if label not in folder_incomplete:
            folder_incomplete.append(label)

    def _record_folder_failure(folder_id: str, ordinal: int, kind: str) -> None:
        """记录单夹失败：不写入 works（避免被当成合法空夹），只标记失败与不完整。"""
        folder_items[folder_id] = {
            "pages": 0, "items": 0, "complete": False, "failed": True, "kind": kind,
        }
        _mark_folder_incomplete(ordinal)
        folder_failures.append({"folder": f"folder#{ordinal}", "kind": kind})
        warnings.append(f"收藏夹#{ordinal} 读取失败（{kind}），保留其他范围与其他夹已完成的结果")

    def _record_remaining(folder_start_ordinal: int, kind: str, reason: str) -> None:
        """把从某序号起尚未处理的夹标记为未完成（只含序号，不含真实夹 ID）。"""
        for remaining_ordinal, remaining_row in enumerate(ordered_folders, start=1):
            remaining_folder = str(remaining_row.get("folder_id") or "")
            if remaining_ordinal < folder_start_ordinal:
                continue
            if wanted and remaining_folder not in wanted:
                continue
            if remaining_folder in folder_items:
                continue
            folder_items[remaining_folder] = {
                "pages": 0, "items": 0, "complete": False, "failed": True, "kind": kind,
            }
            if kind == "budget_exhausted":
                folder_items[remaining_folder]["budget_exhausted"] = True
            _mark_folder_incomplete(remaining_ordinal)
            folder_failures.append({"folder": f"folder#{remaining_ordinal}", "kind": kind})
            warnings.append(f"收藏夹#{remaining_ordinal} {reason}，已标记不完整")

    for ordinal, folder_row in enumerate(ordered_folders, start=1):
        folder_id = str(folder_row.get("folder_id") or "")
        if wanted and folder_id not in wanted:
            continue
        # 上一轮已完整读取的夹直接跳过（按哈希身份判定，不依赖顺序或明文 ID）
        folder_key = _identity_hash(folder_id) if folder_progress is not None else ""
        previous = folder_progress.get(folder_key) if folder_progress is not None else None
        if isinstance(previous, dict) and previous.get("complete") is True:
            folder_items[folder_id] = {
                "pages": 0, "items": 0, "complete": True, "works": [], "resumed": True,
            }
            continue
        if not first:
            # 夹间限流等待也要受整体截止约束，不能睡过预算后继续发请求
            wait = 1.0
            if deadline is not None:
                wait = min(1.0, max(0.0, deadline - time.monotonic()))
            await asyncio.sleep(wait)
        first = False
        if deadline is not None and time.monotonic() >= deadline:
            _record_remaining(ordinal, "budget_exhausted", "因整体时间预算到期未读取")
            block["budget_exhausted"] = True
            all_items_complete = False
            break
        # 日志/不完整清单只允许序号，不允许真实收藏夹 ID
        label = f"收藏夹#{ordinal}"
        start_cursor = int(previous.get("cursor") or 0) if isinstance(previous, dict) else 0
        # 失败回滚点：记录进入本夹前的续传状态（previous 是 folder_progress 内的
        # 存活对象，_on_cursor 会整体替换条目、不改旧对象，浅拷贝足够）
        prefetch_progress = dict(previous) if isinstance(previous, dict) else None

        def _on_cursor(next_cursor, key=folder_key):
            # 只记内存：确认落盘由父进程在业务数据提交后统一完成
            if folder_progress is None:
                return
            folder_progress[key] = {"complete": False, "cursor": int(next_cursor or 0)}

        def _rollback_folder_progress() -> None:
            """单夹失败：该夹游标回到进入前状态，下一轮重读本次已丢弃的页。"""
            if folder_progress is None:
                return
            if prefetch_progress is None:
                folder_progress.pop(folder_key, None)
            else:
                folder_progress[folder_key] = prefetch_progress

        def _on_folder_partial(works, pages_read, oversize) -> None:
            # 已读页作品随读随挂（works 为存活列表）：硬超时/取消落在页间或
            # crawler 关闭中时，结果与该夹游标保持一致，确认不会越过未提交数据
            folder_items[folder_id] = {"pages": pages_read, "items": len(works),
                                       "complete": False,
                                       "server_oversize_pages": oversize, "works": works}
            _mark_folder_incomplete(ordinal)

        try:
            works, meta = await _fetch_folder_items_raw(
                handler, folder_id, label=label, args=args, warnings=warnings,
                start_cursor=start_cursor, deadline=deadline, on_cursor=_on_cursor,
                on_partial=_on_folder_partial,
            )
        except ScopeError as exc:
            if exc.kind in _COLLECTS_ABORT_KINDS:
                raise
            _rollback_folder_progress()
            _record_folder_failure(folder_id, ordinal, exc.kind)
            all_items_complete = False
            consecutive_failures += 1
            if consecutive_failures >= _MAX_CONSECUTIVE_FOLDER_FAILURES:
                _record_remaining(ordinal + 1, "consecutive_failures", "因连续失败停止后续请求")
                block["stopped_after_consecutive_failures"] = True
                warnings.append(
                    f"连续 {_MAX_CONSECUTIVE_FOLDER_FAILURES} 次收藏夹读取失败，"
                    "已停止后续夹请求并将剩余夹标记为不完整")
                break
            continue
        except Exception as exc:  # noqa: BLE001
            error = scope_error_from_exception(exc, label, "collections")
            if error.kind in _COLLECTS_ABORT_KINDS:
                raise error from None
            _rollback_folder_progress()
            _record_folder_failure(folder_id, ordinal, error.kind)
            all_items_complete = False
            consecutive_failures += 1
            if consecutive_failures >= _MAX_CONSECUTIVE_FOLDER_FAILURES:
                _record_remaining(ordinal + 1, "consecutive_failures", "因连续失败停止后续请求")
                block["stopped_after_consecutive_failures"] = True
                warnings.append(
                    f"连续 {_MAX_CONSECUTIVE_FOLDER_FAILURES} 次收藏夹读取失败，"
                    "已停止后续夹请求并将剩余夹标记为不完整")
                break
            continue
        consecutive_failures = 0
        folder_items[folder_id] = meta | ({"works": works} if works else {})
        if meta["complete"]:
            if folder_progress is not None:
                folder_progress[folder_key] = {"complete": True, "cursor": 0}
            # 增量挂载阶段可能已提前标记：完成后撤下该夹的不完整标记
            done_label = f"folder#{ordinal}"
            if done_label in folder_incomplete:
                folder_incomplete.remove(done_label)
        elif meta.get("unavailable"):
            # 夹内容不可读（aweme_list=null）：跨轮计 streak，连续
            # _MAX_FOLDER_UNAVAILABLE_ROUNDS 轮仍不可读才按不可访问收尾；
            # 期间如实算不完整。不计入 consecutive_failures（数据条件而非请求错误）。
            streak = int(previous.get("unavailable_streak") or 0) if isinstance(previous, dict) else 0
            streak += 1
            if streak >= _MAX_FOLDER_UNAVAILABLE_ROUNDS:
                if folder_progress is not None:
                    folder_progress[folder_key] = {"complete": True, "cursor": 0, "unavailable": True}
                folder_items[folder_id] = {
                    "pages": meta["pages"], "items": 0, "complete": True, "unavailable": True,
                }
                warnings.append(
                    f"{label} 连续 {streak} 轮内容不可读（aweme_list=null），按不可访问收藏夹收尾")
            else:
                if folder_progress is not None:
                    folder_progress[folder_key] = {
                        "complete": False, "cursor": 0, "unavailable_streak": streak,
                    }
                folder_items[folder_id] = {
                    "pages": meta["pages"], "items": 0, "complete": False, "unavailable": True,
                    "unavailable_streak": streak,
                }
                _mark_folder_incomplete(ordinal)
                all_items_complete = False
                warnings.append(
                    f"{label} 内容不可读（aweme_list=null），"
                    f"第 {streak}/{_MAX_FOLDER_UNAVAILABLE_ROUNDS} 轮，暂记不完整")
        else:
            _mark_folder_incomplete(ordinal)
            if meta.get("budget_exhausted") is True:
                folder_failures.append({"folder": f"folder#{ordinal}", "kind": "budget_exhausted"})
            all_items_complete = False
    def _folder_terminal(folder_id: str) -> bool:
        """夹处理终结（作品分页完成）。不在请求范围内的夹不阻塞目录水位线。"""
        if wanted and folder_id not in wanted:
            return True
        row = folder_items.get(folder_id)
        return isinstance(row, dict) and row.get("complete") is True

    block["complete"] = all_items_complete and bool(pages)
    if block.get("budget_exhausted") or any(
            row.get("kind") == "budget_exhausted" for row in folder_failures):
        block["budget_exhausted"] = True
        result["partial"] = True
        result["budget_exhausted"] = True
    if scope_progress is not None:
        # 目录水位线：逐页推进——页内夹全部终结才越过该页；否则停在该页起始，
        # 下一轮从水位线重读（已完成夹按哈希身份跳过，重放幂等，不漏夹不漏项）。
        new_list_cursor = list_cursor
        for boundary in page_boundaries:
            if boundary["invalid"] or not all(_folder_terminal(fid) for fid in boundary["folders"]):
                # 该页存在无标识记录或夹未全部终结：水位线停在本页起始重读重试
                new_list_cursor = boundary["start"]
                break
            if boundary["next"] is None:
                # 该页全部终结且列表已读尽（或响应无有效游标，无法继续推进）
                new_list_cursor = 0 if folders_complete else boundary["start"]
                break
            new_list_cursor = boundary["next"]
        scope_progress["list_cursor"] = new_list_cursor
        scope_progress["complete"] = bool(block["complete"])
        scope_progress["folders_complete"] = bool(folders_complete)


async def _fetch_folder_items_raw(handler, folder_id: str, *, label: str, args, warnings: list,
                                  start_cursor: int = 0, deadline: float | None = None,
                                  on_cursor=None, on_partial=None):
    """逐页直读爬虫原始响应获取夹内作品（不经过 handler 生成器）。

    真实 F2 的 `fetch_user_collects_videos` 生成器对空 `aweme_list` 在 yield 前
    就结束，无法区分"合法空夹"与"上游失败"。这里直接调用底层 crawler 并对原始
    响应严格校验：
    - status_code=8 → login_expired（夹内登录失效正确分类）；
    - aweme_list=null 且 has_more=0 → 夹内作品不可访问（夹存在但内容取不出），
      返回 unavailable 标记由调用方跨轮计 streak（连续多轮才按不可访问收尾）；
    - aweme_list 缺失/其他非列表 → platform_error；
    - `aweme_list=[]` 且 has_more=0 → 合法空夹（complete，继续其他夹）；
    - has_more 键缺失 → 不完整；页数/条数预算 → 不完整；
    - 整体截止到期 → 返回已读页并标记 budget_exhausted，`on_cursor` 记录游标
      （只记内存，确认落盘由父进程在业务数据提交后完成）。
    - `on_partial` 在游标推进后回调（works 为存活列表）：把已读页作品增量挂到
      调用方结果上；硬超时/取消落在页间或 crawler 关闭中时，结果与游标仍一致。
    """
    from f2.apps.douyin.model import UserCollectsVideo

    # 夹内作品同样携带登录态：使用禁止跟随重定向的 crawler（3xx 明确失败）
    crawler_cls = _safe_douyin_crawler()

    works: list[dict] = []
    seen: set[str] = set()
    skipped: list[dict] = []
    pages = 0
    oversize_pages = 0
    finished_cleanly = False
    stopped_by_budget = False
    budget_stop = False
    has_more_missing = False
    cursor = max(0, int(start_cursor or 0))

    async with crawler_cls(handler.kwargs) as crawler:
        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    budget_stop = True
                    warnings.append(f"{label} 整体时间预算到期，按不完整处理")
                    break
            params = UserCollectsVideo(
                cursor=cursor, count=int(args.page_counts), collects_id=str(folder_id),
            )
            if deadline is not None:
                try:
                    response = await asyncio.wait_for(
                        crawler.fetch_user_collects_video(params), timeout=remaining)
                except asyncio.TimeoutError:
                    budget_stop = True
                    warnings.append(f"{label} 整体时间预算到期，按不完整处理")
                    break
            else:
                response = await crawler.fetch_user_collects_video(params)
            pages += 1
            status_code = response.get("status_code") if isinstance(response, dict) else None
            status_error = _payload_status_kind(status_code)
            if status_error is not None:
                raise ScopeError(status_error[0], f"{label} {status_error[1]}")
            if not isinstance(response, dict) or "aweme_list" not in response:
                raise ScopeError("platform_error", f"{label} 响应缺少 aweme_list 字段")
            if not isinstance(response.get("aweme_list"), list):
                if _is_unavailable_null_list(response):
                    # 夹存在但内容取不出（aweme_list=null）：交由调用方跨轮计
                    # streak，连续多轮仍不可读才按不可访问收尾；has_more=1 时
                    # 的 null 仍按真异常报错（无法安全解释）。
                    warnings.append(
                        f"{label} 响应 aweme_list 为 {type(response.get('aweme_list')).__name__}"
                        "（夹内作品不可访问或瞬时不可读）")
                    return [], {"pages": pages, "items": 0, "complete": False, "unavailable": True}
                raise ScopeError("platform_error", f"{label} 响应 aweme_list 不是列表")
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
            next_cursor = response.get("cursor")
            cursor = int(next_cursor) if isinstance(next_cursor, (int, float)) \
                and not isinstance(next_cursor, bool) and next_cursor > 0 else 0
            if on_cursor is not None:
                try:
                    on_cursor(cursor)
                except Exception:  # noqa: BLE001 - 续传写入失败不丢弃已读数据
                    warnings.append(f"{label} 续传状态写入失败，本次继续按不完整处理")
            if on_partial is not None:
                # 增量挂载紧随游标推进：任意中断点下结果与游标严格一致
                try:
                    on_partial(works, pages, oversize_pages)
                except Exception:  # noqa: BLE001 - 挂载失败不影响取数
                    warnings.append(f"{label} 已读页结果挂载失败，本次继续按不完整处理")
            if pages >= args.max_pages_per_list:
                warnings.append(f"{label} 达到页数上限 {args.max_pages_per_list}，列表可能不完整")
                stopped_by_budget = True
                break
            if len(works) >= args.max_items_per_list:
                warnings.append(f"{label} 达到条数上限 {args.max_items_per_list}，列表可能不完整")
                stopped_by_budget = True
                break
            wait = max(0, int(args.timeout))
            if deadline is not None:
                wait = min(wait, max(0.0, deadline - time.monotonic()))
            await asyncio.sleep(wait)

    if pages == 0:
        if budget_stop:
            return [], {"pages": 0, "items": 0, "complete": False,
                        "server_oversize_pages": 0, "budget_exhausted": True}
        # crawler 调用循环至少执行一次，除非构造即失败——这里只可能是异常路径
        raise ScopeError("platform_error", f"{label} 未返回任何数据页")
    if not finished_cleanly and not stopped_by_budget and not budget_stop and not has_more_missing:
        warnings.append(f"{label} 上游在 has_more=1 时提前结束，按不完整处理")
    meta = {
        "pages": pages,
        "items": len(works),
        "complete": finished_cleanly,
        "server_oversize_pages": oversize_pages,
    }
    if budget_stop:
        meta["budget_exhausted"] = True
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


def _http_urls(values) -> list[str]:
    return [value for value in values or []
            if isinstance(value, str) and value.startswith(("http://", "https://"))]


def _fallback_video_candidates(video: dict) -> list[str]:
    """F2 选优函数不可用时的本地降级：按分辨率、码率取最高档，再回退 play_addr。"""
    best_urls: list[str] = []
    best_key: tuple[int, int] | None = None
    rows = video.get("bit_rate") if isinstance(video.get("bit_rate"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        addr = row.get("play_addr") if isinstance(row.get("play_addr"), dict) else {}
        urls = _http_urls(addr.get("url_list") if isinstance(addr.get("url_list"), list) else [])
        if not urls:
            continue
        width = addr.get("width") if isinstance(addr.get("width"), int) else 0
        height = addr.get("height") if isinstance(addr.get("height"), int) else 0
        rate = row.get("bit_rate") if isinstance(row.get("bit_rate"), int) else 0
        key = (max(width, 0) * max(height, 0), rate)
        if best_key is None or key > best_key:
            best_key, best_urls = key, urls
    if best_urls:
        return best_urls
    primary = video.get("play_addr") if isinstance(video.get("play_addr"), dict) else {}
    return _http_urls(primary.get("url_list") if isinstance(primary.get("url_list"), list) else [])


def _select_video_url(video: dict) -> list[str]:
    """按固定版 F2 的契约返回视频候选直链（保持选优顺序，可依次尝试）。

    固定版 F2 的 `get_video_play_urls` 先按 bit_rate 的分辨率/码率选最高质量、
    再回退 play_addr；这里复用同一实现，不自行为读 play_addr.url_list 丢失选优。
    返回全部 http(s) 候选（首个为最优）；无法选优时返回空列表（如实不下载）。
    """
    candidates: list[str] = []
    try:
        from f2.apps.douyin.utils import get_video_play_urls

        selected = get_video_play_urls(video)
        if isinstance(selected, list):
            candidates = _http_urls(selected)
    except ImportError:  # pragma: no cover - 非 F2 运行时（离线测试/防御）
        candidates = []
    except Exception:  # noqa: BLE001 - 选优异常不阻塞图集详情，退回本地等价规则
        candidates = []
    if not candidates:
        candidates = _fallback_video_candidates(video)
    return candidates


def run_detail(args: argparse.Namespace) -> dict:
    """单作品详情（图文/视频直取的数据来源）：只调 aweme/detail 只读接口，不打开网页。

    同步函数（内部自管事件循环，调用方需在工作线程中执行）。
    状态码从**原始响应**读取——真实固定版 PostDetailFilter 暴露的是
    `api_status_code` 而无 `status_code` 属性，不能依赖 Filter 属性语义。
    输出白名单字段：作品身份、图片直链列表、视频直链（图集/视频二选一或皆无）。
    失败语义与列表模式一致（kind 明确，绝不伪装成功）：
    - 接口 3xx → redirect_refused（拒绝跟随，不把登录态标识带到跳转目标）；
    - 图集逐行严格校验：任何一行缺 url_list/空直链 → incomplete_album（不静默跳过）；
    - 作品身份与请求的 aweme_id 不一致 → identity_mismatch（不下载错作品）。
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
        with _refuse_redirects_for_handler():
            try:
                page = await handler.fetch_one_video(aweme_id)
            except ScopeError:
                raise
            except Exception as exc:  # noqa: BLE001 - 统一翻译（删除/私密/接口异常），消息不含作品 ID
                raise scope_error_from_exception(exc, "作品详情", "detail") from None
        raw = page._to_raw()
        if not isinstance(raw, dict):
            raise ScopeError("platform_error", "作品详情响应不是 JSON 对象")
        status_error = _payload_status_kind(raw.get("status_code"))
        if status_error is not None:
            raise ScopeError(status_error[0], f"作品详情 {status_error[1]}")
        detail = raw.get("aweme_detail")
        if not isinstance(detail, dict):
            raise ScopeError("platform_error", "作品详情响应缺少 aweme_detail 字段")
        external_id = str(detail.get("aweme_id") or "").strip()
        if not external_id:
            raise ScopeError("platform_error", "作品详情缺少 aweme_id")
        if external_id != aweme_id:
            raise ScopeError("identity_mismatch", "作品详情身份与请求不一致（详情返回了另一个作品）")
        # 图集逐行严格校验：缺行/空直链都算不完整，绝不静默跳过
        images: list[str] = []
        rows = detail.get("images") if isinstance(detail.get("images"), list) else []
        for index, row in enumerate(rows, start=1):
            url = None
            if isinstance(row, dict) and isinstance(row.get("url_list"), list) and row["url_list"]:
                first = row["url_list"][0]
                if isinstance(first, str) and first.startswith(("http://", "https://")):
                    url = first
            if url is None:
                raise ScopeError("incomplete_album", f"作品图集第 {index} 行缺少有效图片直链（整册按不完整处理）")
            images.append(url)
        # 视频直链：**直接采用固定版 F2 的选优结果**（get_video_play_urls：
        # 先按 bit_rate 分辨率/码率选最高质量，回退 play_addr）——与
        # PostDetailFilter.video_play_addr 同一实现，不用自行读 url_list。
        # 保留完整候选顺序，下载层可对首个失败依次尝试后续地址。
        video_candidates = _select_video_url(
            detail.get("video") if isinstance(detail.get("video"), dict) else {})
        author = detail.get("author") if isinstance(detail.get("author"), dict) else {}
        return {
            "external_id": external_id,
            "title": (detail.get("desc") or "").strip() or None,
            "author": (author.get("nickname") or "").strip() or None,
            "published_at": iso_utc_from_unix(detail.get("create_time")),
            "images": images,
            "video_url": video_candidates[0] if video_candidates else None,
            "video_urls": video_candidates,
        }

    detail = asyncio.run(asyncio.wait_for(_fetch(), timeout=args.budget_seconds))
    return {"ok": True, "detail": detail}


def _run_list_mode(args: argparse.Namespace) -> dict:
    """列表模式：内部截止优先，硬超时兜底；超时但已读到数据时收尾为 partial。

    内部截止让已完成的点赞/收藏/夹结果正常返回；硬超时只用于卡住的请求，
    此时从 progress 中取出已完成结果并标记剩余范围不完整，不再丢弃全部数据。
    """
    progress: dict = {}
    budget = max(0, int(getattr(args, "budget_seconds", 0) or 0))
    deadline = time.monotonic() + budget

    async def bounded() -> dict:
        return await asyncio.wait_for(
            run_scopes(args, progress=progress, deadline=deadline),
            timeout=budget + _budget_grace(args),
        )

    try:
        return asyncio.run(bounded())
    except asyncio.TimeoutError:
        partial = progress.get("result")
        if _has_partial_data(partial):
            return _finalize_budget_partial(args, partial)
        raise


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    try:
        acquire_lock(args.lock_file)
    except ScopeError as exc:
        write_output(args.output, {"ok": False, "error": {"kind": exc.kind, "message": redact_sensitive_text(str(exc))}})
        return 1

    async def bounded_detail() -> dict:
        # run_detail 内部自管事件循环：放到工作线程执行，避免嵌套 asyncio.run
        return await asyncio.wait_for(asyncio.to_thread(run_detail, args), timeout=args.budget_seconds)

    try:
        result = asyncio.run(bounded_detail()) if args.mode == "detail" else _run_list_mode(args)
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
