"""单条小红书作品的评论采集执行器（后台任务子进程）。

只处理用户提交的这一条作品：不扫描其他作品、不写全局触发文件、不启动定时任务。
- 真实模式：复用 MediaCrawler 的小红书 API 客户端（xhshow 签名）与项目登录态 cookie，
  自行分页获取主评论、回复、图片与可用语音转写；
- 模拟模式（``--fixture``）：读取本地构造的分页数据，完全不联网，用于隔离验收。

退出时把结构化结果与报告原子写入作品目录；失败写明原因，不伪造成功。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor.platforms import xhs_comments as core  # noqa: E402
from monitor.platforms import xhs_voice  # noqa: E402

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
MAX_SUB_PAGES_PER_ROOT = 10
MAX_IMAGES_PER_COMMENT = 20
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REFERER = "https://www.xiaohongshu.com/"


class CollectorError(Exception):
    """可安全展示给用户的采集失败原因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_json(path: Path, data: Any) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


class JobState:
    """任务进度文件（原子更新，后端可轮询读取）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict = json.loads(path.read_text(encoding="utf-8"))

    def update(
        self,
        *,
        phase: str | None = None,
        status: str | None = None,
        error: str | None = None,
        result_available: bool | None = None,
        progress: dict | None = None,
    ) -> None:
        if phase is not None:
            self.data["phase"] = phase
        if status is not None:
            self.data["status"] = status
        if error is not None:
            self.data["error"] = error
        if result_available is not None:
            self.data["result_available"] = result_available
        if progress:
            self.data.setdefault("progress", {}).update(progress)
        self.data["updated_at"] = core.now_iso()
        _atomic_write_json(self.path, self.data)


def _note_id_from_url(url: str) -> str:
    match = re.search(r"/(?:explore|discovery/item)/([0-9A-Za-z]+)", urlsplit(url).path)
    return match.group(1) if match else ""


def _xsec_token_from_url(url: str) -> str:
    try:
        values = parse_qs(urlsplit(url).query)
    except ValueError:
        return ""
    return (values.get("xsec_token") or [""])[0]


def _xsec_source_from_url(url: str) -> str:
    try:
        values = parse_qs(urlsplit(url).query)
    except ValueError:
        return ""
    return (values.get("xsec_source") or [""])[0]


def _normalize_rows(
    rows: list[dict],
    *,
    parent_comment_id: str | None,
    start_order: int,
    comments: list[dict],
    on_main: Callable[[dict], None] | None = None,
) -> int:
    """把一页原始评论并入 comments；返回下一个主评论的 platform_order。"""
    order = start_order
    for row in rows:
        if not isinstance(row, dict):
            continue
        main = core.normalize_comment(row, platform_order=order if parent_comment_id is None else None)
        if not main:
            continue
        comments.append(main)
        if parent_comment_id is None:
            order += 1
            if on_main:
                on_main(main)
        for sub in row.get("sub_comments") or row.get("subComments") or []:
            if not isinstance(sub, dict):
                continue
            reply = core.normalize_comment(sub, parent_comment_id=main["comment_id"])
            if reply:
                comments.append(reply)
    return order


def _progress_snapshot(comments: list[dict], images_total: int, images_done: int, images_failed: int) -> dict:
    mains = len([c for c in comments if not c["parent_comment_id"]])
    replies = len([c for c in comments if c["parent_comment_id"]])
    return {
        "comments_fetched": len(comments),
        "main_comments": mains,
        "replies_fetched": replies,
        "images_total": images_total,
        "images_done": images_done,
        "images_failed": images_failed,
    }


# --------------------------------------------------------------------------
# 图片
# --------------------------------------------------------------------------

def _image_ext(source: str | Path, content_type: str = "") -> str:
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    if "gif" in content_type:
        return ".gif"
    if "avif" in content_type:
        return ".avif"
    suffix = Path(str(source)).suffix.lower()
    return suffix if suffix in IMAGE_EXT else ".jpg"


def _image_pairs(comments: list[dict]):
    for comment in comments:
        for index, picture in enumerate(comment.get("pictures") or [], 1):
            yield comment["comment_id"], index, picture


def _target_image_path(store: Path, comment_id: str, index: int, ext: str) -> Path:
    return store / "images" / f"c_{comment_id}_{index}{ext}"


def download_fixture_images(
    store: Path,
    comments: list[dict],
    image_files: dict[str, str],
    fixture_dir: Path,
    include_images: bool,
    warn: Callable[[str], None],
    progress: Callable[..., None],
) -> None:
    """模拟模式：从 fixture 目录复制图片；未提供映射的图片标记失败（可重试）。"""
    images = list(_image_pairs(comments))
    if not include_images:
        for _cid, _index, picture in images:
            picture["status"] = "skipped"
            picture["error"] = "not_requested"
        return
    done = failed = 0
    progress(images_total=len(images))
    for comment_id, index, picture in images:
        picture["attempts"] = int(picture.get("attempts") or 0) + 1
        source_name = image_files.get(picture["url"])
        source = (fixture_dir / source_name) if source_name else None
        if source and source.is_file() and source.stat().st_size > 0:
            ext = _image_ext(source)
            target = _target_image_path(store, comment_id, index, ext)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            picture.update(
                {
                    "status": "downloaded",
                    "local_path": f"images/{target.name}",
                    "error": None,
                    "bytes": target.stat().st_size,
                }
            )
            done += 1
        else:
            picture.update({"status": "failed", "local_path": None, "error": "fixture_image_missing"})
            failed += 1
        progress(images_done=done, images_failed=failed)
    if failed:
        warn("image_download_failed")


def download_live_images(
    store: Path,
    comments: list[dict],
    include_images: bool,
    warn: Callable[[str], None],
    progress: Callable[..., None],
) -> None:
    """真实模式：逐张下载评论图片；失败保留原链接与失败状态，便于重试。"""
    images = list(_image_pairs(comments))
    if not include_images:
        for _cid, _index, picture in images:
            picture["status"] = "skipped"
            picture["error"] = "not_requested"
        return
    if not images:
        return
    import httpx

    done = failed = 0
    headers = {"User-Agent": UA, "Referer": REFERER}
    progress(images_total=len(images))
    with httpx.Client(follow_redirects=True, timeout=25.0, headers=headers) as client:
        for comment_id, index, picture in images:
            picture["attempts"] = int(picture.get("attempts") or 0) + 1
            url = picture.get("url") or ""
            if not url.startswith(("http://", "https://")):
                picture.update({"status": "failed", "error": "invalid_url"})
                failed += 1
                continue
            try:
                response = client.get(url)
            except Exception:
                picture.update({"status": "failed", "error": "download_error"})
                failed += 1
                progress(images_done=done, images_failed=failed)
                continue
            if response.status_code != 200:
                picture.update({"status": "failed", "error": f"http_{response.status_code}"})
                failed += 1
                progress(images_done=done, images_failed=failed)
                continue
            content = response.content
            if len(content) < 500:
                picture.update({"status": "failed", "error": "content_too_small"})
                failed += 1
                progress(images_done=done, images_failed=failed)
                continue
            ext = _image_ext(url, response.headers.get("content-type", ""))
            target = _target_image_path(store, comment_id, index, ext)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".part")
            tmp.write_bytes(content)
            os.replace(tmp, target)
            picture.update(
                {
                    "status": "downloaded",
                    "local_path": f"images/{target.name}",
                    "error": None,
                    "bytes": len(content),
                }
            )
            done += 1
            progress(images_done=done, images_failed=failed)
    if failed:
        warn("image_download_failed")


# --------------------------------------------------------------------------
# 语音转写
# --------------------------------------------------------------------------

def _voice_not_requested(comments: list[dict]) -> dict:
    for comment in comments:
        if (comment.get("voice") or {}).get("status") == "not_extracted":
            comment["voice"]["status"] = "not_requested"
    return {
        "requested": False,
        "status": "not_requested",
        "page_mode": None,
        "cdp_available": False,
        "opened_new_page": False,
        "pages_scanned": 0,
        "matched_page_count": 0,
        "extracted": 0,
        "associated": 0,
        "unassociated": [],
        "failure_reason": None,
    }


def voice_fixture(
    store: Path,
    comments: list[dict],
    payload: dict | None,
    include_voice: bool,
    warn: Callable[[str], None],
) -> dict:
    if not include_voice:
        return _voice_not_requested(comments)
    if not payload:
        warn("voice_extraction_failed")
        for comment in comments:
            if (comment.get("voice") or {}).get("status") == "not_extracted":
                comment["voice"]["status"] = "failed"
        return {
            "requested": True,
            "status": "failed",
            "page_mode": "fixture",
            "cdp_available": False,
            "opened_new_page": False,
            "pages_scanned": 0,
            "matched_page_count": 0,
            "extracted": 0,
            "associated": 0,
            "unassociated": [],
            "failure_reason": "模拟数据未提供语音结果",
        }
    if payload.get("status") == "failed":
        warn("voice_extraction_failed")
        for comment in comments:
            if (comment.get("voice") or {}).get("status") == "not_extracted":
                comment["voice"]["status"] = "failed"
        return {
            "requested": True,
            "status": "failed",
            "page_mode": "fixture",
            "cdp_available": False,
            "opened_new_page": False,
            "pages_scanned": 0,
            "matched_page_count": 0,
            "extracted": 0,
            "associated": 0,
            "unassociated": [],
            "failure_reason": payload.get("reason") or "模拟语音提取失败",
        }
    rows = payload.get("rows") or []
    extracted = len([r for r in rows if str(r.get("text") or "").strip()])
    attached, unassociated = core.associate_voice(comments, rows)
    _atomic_write_json(
        store / "voice_raw.json",
        {
            "status": "extracted",
            "source": "fixture",
            "page_mode": "fixture",
            "opened_new_page": False,
            "pages_scanned": 0,
            "matched_page_count": 0,
            "rows": rows,
            "unassociated": unassociated,
        },
    )
    for comment in comments:
        if (comment.get("voice") or {}).get("status") == "not_extracted":
            comment["voice"]["status"] = "not_extracted"
    return {
        "requested": True,
        "status": "extracted",
        "page_mode": "fixture",
        "cdp_available": False,
        "opened_new_page": False,
        "pages_scanned": 0,
        "matched_page_count": 0,
        "extracted": extracted,
        "associated": attached,
        "unassociated": unassociated,
        "failure_reason": None,
    }


VOICE_SCROLL_TARGET_JS = """() => {
    // 小红书评论列表是虚拟滚动：scrollTop 直设不触发加载，必须用真实滚轮事件。
    // 这里定位"包含评论条目最多的可滚动容器"，把鼠标移到它中心再发滚轮。
    let best = null, bestN = 0;
    document.querySelectorAll('*').forEach(el => {
        if (el.scrollHeight > el.clientHeight + 50) {
            const n = el.querySelectorAll('.comment-item').length;
            if (n > bestN) { bestN = n; best = el; }
        }
    });
    if (!best) return {x: window.innerWidth / 2, y: window.innerHeight / 2, items: bestN};
    const r = best.getBoundingClientRect();
    return {x: r.x + r.width / 2, y: r.y + Math.min(r.height / 2, 400), items: bestN};
}"""

VOICE_EXPAND_JS = """() => {
    document.querySelectorAll('.show-more, [class*=expand-comment], [class*=show-more], [class*="more-btn"]').forEach(el => {
        try { el.click(); } catch (e) {}
    });
    return document.querySelectorAll('.comment-item').length;
}"""

VOICE_PROGRESS_JS = """() => ({
    items: document.querySelectorAll('.comment-item').length,
    voiceCount: document.querySelectorAll('.audio-comment-container').length,
})"""

VOICE_SCROLL_INTERVAL = 0.6
VOICE_SCROLL_MAX_ROUNDS = 90
VOICE_SCROLL_STALE_LIMIT = 8

VOICE_ROWS_JS = """() => {
    const out = [];
    document.querySelectorAll('.audio-comment-container').forEach(el => {
        const text = (el.textContent || '').trim();
        if (!text) return;
        const box = el.closest('.comment-item, [class*=comment-item]');
        let comment_id = '';
        if (box && box.id && box.id.startsWith('comment-')) {
            comment_id = box.id.slice('comment-'.length);
        }
        const nickname = box ? (box.querySelector('[class*=nickname]')?.textContent || '').trim() : '';
        const likes = box ? (box.querySelector('[class*=like]')?.textContent || '').trim() : '';
        out.push({text, nickname, like_count: likes, comment_id, truncated: /展开\\s*$/.test(text)});
    });
    return out;
}"""

LOGIN_HINT_JS = """() => {
    const text = document.body ? document.body.innerText : '';
    return text.includes('登录后查看评论') || text.includes('登录后评论') || text.includes('请先登录');
}"""


def _set_voice_status(comments: list[dict], status: str) -> None:
    for comment in comments:
        voice = comment.get("voice") or {}
        if voice.get("status") == "not_extracted" and comment.get("content_status") == "empty":
            voice["status"] = status


def _voice_payload(
    status: str,
    page_mode: str | None,
    *,
    cdp_available: bool = True,
    opened_new_page: bool = False,
    pages_scanned: int = 0,
    matched_page_count: int = 0,
    extracted: int = 0,
    associated: int = 0,
    unassociated: list[dict] | None = None,
    failure_reason: str | None = None,
) -> dict:
    return {
        "requested": True,
        "status": status,
        "page_mode": page_mode,
        "cdp_available": cdp_available,
        "opened_new_page": opened_new_page,
        "pages_scanned": pages_scanned,
        "matched_page_count": matched_page_count,
        "extracted": extracted,
        "associated": associated,
        "unassociated": unassociated or [],
        "failure_reason": failure_reason,
    }


def _write_voice_raw(store: Path, payload: dict, rows: list[dict] | None = None) -> None:
    _atomic_write_json(
        store / "voice_raw.json",
        {
            "status": payload.get("status"),
            "source": "xhs_web_dom",
            "page_mode": payload.get("page_mode"),
            "cdp_available": payload.get("cdp_available"),
            "opened_new_page": payload.get("opened_new_page"),
            "pages_scanned": payload.get("pages_scanned"),
            "matched_page_count": payload.get("matched_page_count"),
            "failure_reason": payload.get("failure_reason"),
            "rows": rows or [],
            "unassociated": payload.get("unassociated") or [],
            "extracted_at": core.now_iso(),
        },
    )


def _expand_comments(page) -> None:
    """用真实滚轮事件推进评论虚拟列表、展开回复，读尽转写行。

    停止判据用"渲染的评论条数"而不是音频行数：音频评论稀疏，列表推进很多轮
    也未必新增一行；条数连续多轮不增长说明列表已读尽。
    """
    last_items = -1
    stale = 0
    for _ in range(VOICE_SCROLL_MAX_ROUNDS):
        try:
            page.evaluate(VOICE_EXPAND_JS)
        except Exception:  # noqa: BLE001 - 展开按钮结构变化不阻塞滚动
            pass
        try:
            metrics = page.evaluate(VOICE_PROGRESS_JS)
        except Exception:  # noqa: BLE001
            metrics = {}
        items = int(metrics.get("items") or 0) if isinstance(metrics, dict) else 0
        if items == last_items:
            stale += 1
            if stale >= VOICE_SCROLL_STALE_LIMIT:
                break
        else:
            last_items = items
            stale = 0
        try:
            target = page.evaluate(VOICE_SCROLL_TARGET_JS)
        except Exception:  # noqa: BLE001
            target = None
        if isinstance(target, dict) and target.get("x") is not None:
            try:
                page.mouse.move(int(target["x"]), int(target["y"]))
            except Exception:  # noqa: BLE001
                pass
        try:
            page.mouse.wheel(0, 800)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(VOICE_SCROLL_INTERVAL)


def _page_login_hint(page) -> bool:
    try:
        return bool(page.evaluate(LOGIN_HINT_JS))
    except Exception:
        return False


def voice_live(
    store: Path,
    comments: list[dict],
    note_url: str,
    note_id: str,
    include_voice: bool,
    voice_authorized: bool,
    allow_open_page: bool,
    warn: Callable[[str], None],
    progress: Callable[..., None],
) -> dict:
    """真实模式：复用已打开浏览器页面，读取平台已生成的语音转写。

    - 找到目标作品页：不导航、不刷新、不新开页；
    - 未找到且允许新开：在该浏览器新开一个标签页，提取后只关闭这个新页；
    - 未找到且不允许新开：返回 not_found，不打开页面；
    - CDP 未就绪：返回 cdp_unavailable，不启动新浏览器。
    不调用 browser.close()，不关闭用户浏览器。
    """
    if not include_voice:
        return _voice_not_requested(comments)
    if not voice_authorized:
        payload = _voice_payload("failed", None, failure_reason="未授权本次使用已登录浏览器会话")
        _set_voice_status(comments, "failed")
        _write_voice_raw(store, payload)
        progress(voice_status="failed", voice_phase="failed")
        return payload

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        warn("voice_extraction_failed")
        payload = _voice_payload("failed", None, failure_reason="未安装 Playwright，无法读取浏览器页面")
        _set_voice_status(comments, "failed")
        _write_voice_raw(store, payload)
        progress(voice_status="failed", voice_phase="failed")
        return payload

    cdp_url = (os.getenv("XHS_CDP_URL") or xhs_voice.CDP_DEFAULT).strip()
    progress(voice_status="lookup_page", voice_phase="lookup_page", voice_page_mode=None)
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=15000)
            except Exception as exc:
                warn("voice_cdp_unavailable")
                payload = _voice_payload(
                    "not_extracted",
                    "cdp_unavailable",
                    cdp_available=False,
                    failure_reason=f"浏览器调试端口未就绪（{type(exc).__name__}）；请先开启 CDP 并登录小红书",
                )
                _write_voice_raw(store, payload)
                progress(
                    voice_status="cdp_unavailable",
                    voice_phase="failed",
                    voice_page_mode="cdp_unavailable",
                )
                return payload

            acquisition = None
            try:
                acquisition = xhs_voice.acquire_note_page(
                    browser,
                    note_id=note_id,
                    note_url=note_url,
                    allow_open_page=allow_open_page,
                )
                progress(
                    voice_pages_scanned=acquisition.pages_scanned,
                    voice_page_mode=acquisition.page_mode,
                )
                if acquisition.page is None:
                    code = "voice_page_not_found" if acquisition.page_mode == "not_found" else "voice_extraction_failed"
                    warn(code)
                    if acquisition.page_mode == "not_found":
                        payload = _voice_payload(
                            "not_extracted",
                            "not_found",
                            pages_scanned=acquisition.pages_scanned,
                            matched_page_count=acquisition.matched_page_count,
                            failure_reason=acquisition.failure_reason,
                        )
                        progress(voice_status="not_found", voice_phase="completed")
                    else:
                        _set_voice_status(comments, "failed")
                        payload = _voice_payload(
                            "failed",
                            acquisition.page_mode,
                            opened_new_page=acquisition.opened_new_page,
                            pages_scanned=acquisition.pages_scanned,
                            matched_page_count=acquisition.matched_page_count,
                            failure_reason=acquisition.failure_reason,
                        )
                        progress(
                            voice_status="failed",
                            voice_phase="failed",
                            voice_page_mode=acquisition.page_mode,
                        )
                    _write_voice_raw(store, payload)
                    return payload

                page = acquisition.page
                if acquisition.page_mode == "reused":
                    progress(voice_status="reusing_page", voice_phase="reusing_page", voice_page_mode="reused")
                else:
                    progress(voice_status="opening_page", voice_phase="opening_page", voice_page_mode="opened")
                time.sleep(5 if acquisition.opened_new_page else 1)

                try:
                    page.wait_for_selector(".comments-container, .comment-item", timeout=12000)
                except Exception:
                    login = _page_login_hint(page)
                    reason = (
                        "登录态失效或未登录，无法读取平台语音转写"
                        if login
                        else "未找到评论容器（页面可能未加载完成）"
                    )
                    warn("voice_extraction_failed")
                    _set_voice_status(comments, "failed")
                    payload = _voice_payload(
                        "failed",
                        acquisition.page_mode,
                        opened_new_page=acquisition.opened_new_page,
                        pages_scanned=acquisition.pages_scanned,
                        matched_page_count=acquisition.matched_page_count,
                        failure_reason=reason,
                    )
                    _write_voice_raw(store, payload)
                    progress(voice_status="failed", voice_phase="failed", voice_page_mode=acquisition.page_mode)
                    return payload

                progress(voice_status="extracting", voice_phase="extracting")
                _expand_comments(page)
                rows = page.evaluate(VOICE_ROWS_JS) or []
                if not isinstance(rows, list):
                    rows = []
                extracted = len([r for r in rows if isinstance(r, dict) and str(r.get("text") or "").strip()])
                attached, unassociated = core.associate_voice(comments, rows)
                payload = _voice_payload(
                    "extracted",
                    acquisition.page_mode,
                    opened_new_page=acquisition.opened_new_page,
                    pages_scanned=acquisition.pages_scanned,
                    matched_page_count=acquisition.matched_page_count,
                    extracted=extracted,
                    associated=attached,
                    unassociated=unassociated,
                )
                _write_voice_raw(store, payload, rows)
                progress(
                    voice_status="extracted",
                    voice_phase="completed",
                    voice_rows=extracted,
                    voice_associated=attached,
                )
                return payload
            finally:
                # 只关闭本次新开的标签页；绝不关闭用户浏览器或已有页面
                if acquisition and acquisition.opened_new_page and acquisition.page is not None:
                    try:
                        acquisition.page.close()
                    except Exception:
                        pass
    except Exception as exc:
        warn("voice_extraction_failed")
        _set_voice_status(comments, "failed")
        payload = _voice_payload(
            "failed",
            None,
            failure_reason=f"读取浏览器语音转写失败（{type(exc).__name__}: {str(exc)[:120]}）",
        )
        _write_voice_raw(store, payload)
        progress(voice_status="failed", voice_phase="failed")
        return payload


# --------------------------------------------------------------------------
# 模拟数据采集
# --------------------------------------------------------------------------

def collect_fixture(
    *,
    fixture_dir: Path,
    store: Path,
    include_images: bool,
    include_voice: bool,
    max_pages: int,
    max_comments: int,
    job: JobState,
) -> dict:
    pages_path = fixture_dir / "pages.json"
    if not pages_path.is_file():
        raise CollectorError("fixture_missing", "模拟数据目录缺少 pages.json")
    doc = json.loads(pages_path.read_text(encoding="utf-8"))
    note = doc.get("note") or {}
    comments: list[dict] = []
    reasons: list[str] = []

    def warn(code: str) -> None:
        if code not in reasons:
            reasons.append(code)

    def progress(**kwargs: Any) -> None:
        job.update(progress=kwargs)

    job.update(phase="fetching_comments")
    pages_fetched = 0
    page_end = False
    platform_total = note.get("platform_total", note.get("comment_count_platform"))
    main_order = 0
    pages = doc.get("pages") or []
    interrupt = doc.get("interrupt") or {}
    for index, page in enumerate(pages):
        if interrupt and int(interrupt.get("page_index", -1)) == index:
            code = str(interrupt.get("reason") or "network_timeout")
            warn(code if code in core.BLOCKING_REASONS else "page_interrupted")
            break
        if page.get("error"):
            code = str(page["error"])
            warn(code if code in core.BLOCKING_REASONS else "page_interrupted")
            break
        rows = page.get("comments") or []
        main_order = _normalize_rows(
            rows,
            parent_comment_id=None,
            start_order=main_order,
            comments=comments,
        )
        pages_fetched += 1
        if page.get("total") not in (None, ""):
            platform_total = core.to_int(page.get("total"))
        page_end = not bool(page.get("has_more"))
        progress(pages_fetched=pages_fetched, **_progress_snapshot(comments, 0, 0, 0))
        if len(comments) >= max_comments:
            warn("max_comments_reached")
            break
        if index == max_pages - 1 and page.get("has_more"):
            warn("max_pages_reached")
            break

    if pages and not page_end and not [r for r in reasons if r in core.BLOCKING_REASONS]:
        warn("page_interrupted")

    job.update(phase="fetching_replies")
    sub_complete = True
    for item in doc.get("replies") or []:
        if item.get("error"):
            sub_complete = False
            warn("reply_incomplete")
            continue
        parent_id = str(item.get("parent_id") or "")
        rows = [r for r in item.get("comments") or [] if isinstance(r, dict)]
        for row in rows:
            reply = core.normalize_comment(row, parent_comment_id=parent_id)
            if reply:
                comments.append(reply)
        if item.get("has_more"):
            sub_complete = False
            warn("reply_incomplete")
        progress(**_progress_snapshot(comments, 0, 0, 0))

    job.update(phase="downloading_images")
    download_fixture_images(
        store,
        comments,
        doc.get("image_files") or {},
        fixture_dir,
        include_images,
        warn,
        progress,
    )

    job.update(phase="extracting_voice")
    voice_run = voice_fixture(store, comments, doc.get("voice"), include_voice, warn)

    return {
        "comments": comments,
        "note_meta": {
            "status": "available" if note.get("title") else "unavailable",
            "title": note.get("title"),
            "author": note.get("author"),
            "platform_total": core.to_int(platform_total) if platform_total not in (None, "") else None,
        },
        "platform_total": core.to_int(platform_total) if platform_total not in (None, "") else None,
        "pages_fetched": pages_fetched,
        "page_end_reached": page_end,
        "sub_pagination_complete": sub_complete,
        "reasons": reasons,
        "voice_run": voice_run,
    }


# --------------------------------------------------------------------------
# 真实采集
# --------------------------------------------------------------------------

def _load_xhs_cookie() -> str:
    path = ROOT / "config" / "platform_cookies.json"
    if path.is_file():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
            cookie = str((config.get("xhs") or {}).get("cookie") or "").strip()
            if cookie:
                return cookie
        except (json.JSONDecodeError, OSError):
            pass
    return str(os.getenv("XHS_COOKIE") or "").strip()


def _mediacrawler_dir() -> Path:
    configured = os.getenv("MEDIACRAWLER_DIR")
    return Path(configured).resolve() if configured else ROOT / "tools" / "mediacrawler"


def _import_mediacrawler():
    mc_dir = _mediacrawler_dir()
    if not (mc_dir / "media_platform" / "xhs" / "client.py").is_file():
        raise CollectorError("collector_error", "未找到 MediaCrawler 采集依赖，无法执行真实采集")
    if str(mc_dir) not in sys.path:
        sys.path.insert(0, str(mc_dir))
    os.chdir(mc_dir)  # MediaCrawler 配置以相对路径读取自身资源
    import config as mc_config  # type: ignore  # noqa: PLC0415
    from media_platform.xhs.client import XiaoHongShuClient  # type: ignore  # noqa: PLC0415
    from tools.crawler_util import convert_str_cookie_to_dict  # type: ignore  # noqa: PLC0415

    mc_config.ENABLE_GET_SUB_COMMENTS = True
    return XiaoHongShuClient, convert_str_cookie_to_dict


def _unwrap_retry_error(exc: BaseException) -> BaseException:
    """tenacity RetryError 只暴露包装形态；解包找到真实异常再分类。"""
    current: BaseException | None = exc
    for _ in range(4):
        if current is None:
            break
        inner_obj = getattr(current, "last_attempt", None)
        if inner_obj is None:
            inner_obj = getattr(current, "last", None)
        if inner_obj is not None:
            getter = getattr(inner_obj, "exception", None)
            if callable(getter):
                try:
                    inner = getter()
                except Exception:  # noqa: BLE001 - 未完成的 Future 等
                    inner = None
                if isinstance(inner, BaseException):
                    current = inner
                    continue
        nxt = current.__cause__ or current.__context__
        if isinstance(nxt, BaseException) and nxt is not current:
            current = nxt
            continue
        break
    return current if current is not None else exc


def _map_api_error(exc: Exception) -> str:
    err = _unwrap_retry_error(exc)
    name = type(err).__name__
    text = str(err).lower()
    if name == "NoteNotFoundError":
        return "note_unavailable"
    if name == "IPBlockError":
        return "rate_limited"
    if name == "PlatformAccessError":
        return "rate_limited" if "security" in text else "access_denied"
    # 平台返回 461/471 且未带 Verifytype 响应头时，MediaCrawler 抛 KeyError('Verifytype')——同样是风控
    if name == "KeyError" and "verifytype" in text:
        return "rate_limited"
    if name in {"TimeoutException", "ReadTimeout", "ConnectTimeout", "TimeoutError"}:
        return "network_timeout"
    if "461" in text or "471" in text or "captcha" in text or "verify" in text:
        return "rate_limited"
    if "login" in text or "登录" in str(err) or "300012" in text:
        return "login_required"
    if "300031" in text or "暂时无法浏览" in str(err):
        return "note_unavailable"
    return "page_interrupted"


def collect_live(
    *,
    url: str,
    note_id: str,
    store: Path,
    include_images: bool,
    include_voice: bool,
    allow_open_page: bool,
    max_pages: int,
    max_comments: int,
    job: JobState,
) -> dict:
    cookie = _load_xhs_cookie()
    if not cookie:
        raise CollectorError(
            "login_required",
            "未配置小红书登录态（config/platform_cookies.json 缺少 xhs.cookie），无法采集真实评论",
        )
    XiaoHongShuClient, convert_str_cookie_to_dict = _import_mediacrawler()
    xsec_token = _xsec_token_from_url(url)
    xsec_source = _xsec_source_from_url(url) or "pc_search"
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "application/json;charset=UTF-8",
        "origin": "https://www.xiaohongshu.com",
        "referer": "https://www.xiaohongshu.com/",
        "user-agent": UA,
        "Cookie": cookie,
    }
    client = XiaoHongShuClient(
        headers=headers,
        playwright_page=None,  # 纯算法签名，不依赖浏览器页面
        cookie_dict=convert_str_cookie_to_dict(cookie),
    )

    def warn(code: str, target: list[str]) -> None:
        if code not in target:
            target.append(code)

    def progress(**kwargs: Any) -> None:
        job.update(progress=kwargs)

    return asyncio.run(
        _collect_live_async(
            client=client,
            url=url,
            note_id=note_id,
            store=store,
            xsec_token=xsec_token,
            xsec_source=xsec_source,
            include_images=include_images,
            include_voice=include_voice,
            allow_open_page=allow_open_page,
            max_pages=max_pages,
            max_comments=max_comments,
            warn=warn,
            progress=progress,
        )
    )


async def _collect_live_async(
    *,
    client,
    url: str,
    note_id: str,
    store: Path,
    xsec_token: str,
    xsec_source: str,
    include_images: bool,
    include_voice: bool,
    allow_open_page: bool,
    max_pages: int,
    max_comments: int,
    warn,
    progress,
) -> dict:
    reasons: list[str] = []
    comments: list[dict] = []
    note_meta: dict = {"status": "unavailable", "title": None, "author": None, "platform_total": None}

    progress(phase="fetching_note")
    try:
        note_card = await client.get_note_by_id(note_id, xsec_source, xsec_token)
        if note_card:
            interact = note_card.get("interact_info") or note_card.get("interactInfo") or {}
            user = note_card.get("user") or {}
            total = interact.get("comment_count") or interact.get("commentCount")
            note_meta = {
                "status": "available",
                "title": note_card.get("title") or None,
                "author": user.get("nickname") or user.get("nickName") or None,
                "platform_total": core.to_int(total) if total not in (None, "") else None,
            }
        else:
            warn("note_meta_unavailable", reasons)
    except Exception:
        warn("note_meta_unavailable", reasons)

    progress(phase="fetching_comments")
    pages_fetched = 0
    page_end = False
    platform_total = note_meta.get("platform_total")
    main_order = 0
    cursor = ""
    seen_cursors: set[str] = set()
    while True:
        if pages_fetched >= max_pages:
            warn("max_pages_reached", reasons)
            break
        try:
            page = await client.get_note_comments(note_id=note_id, xsec_token=xsec_token, cursor=cursor)
        except Exception as exc:
            warn(_map_api_error(exc), reasons)
            break
        rows = page.get("comments") or []
        main_order = _normalize_rows(rows, parent_comment_id=None, start_order=main_order, comments=comments)
        pages_fetched += 1
        if page.get("total") not in (None, ""):
            platform_total = core.to_int(page.get("total"))
        progress(pages_fetched=pages_fetched, **_progress_snapshot(comments, 0, 0, 0))
        if len(comments) >= max_comments:
            warn("max_comments_reached", reasons)
            break
        if not page.get("has_more"):
            page_end = True
            break
        next_cursor = str(page.get("cursor") or "")
        if not next_cursor:
            warn("page_interrupted", reasons)
            break
        if next_cursor == cursor:
            # 平台在尽头会重复返回同一游标且 has_more 仍为真：主评论分页已无新数据，视为结束
            page_end = True
            break
        if next_cursor in seen_cursors:
            # 回跳到更早的游标：真正的翻页中断，如实标记
            warn("page_interrupted", reasons)
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        await asyncio.sleep(random.uniform(1.0, 2.0))

    # 跨页重复的评论先合并：避免同楼重复抓回复、图片按重复份数重复下载相互覆盖
    comments = core.dedupe_comments(comments)

    progress(phase="fetching_replies")
    sub_complete = True
    mains = [c for c in comments if not c["parent_comment_id"]]

    def _unique_reply_count(root_id: str) -> int:
        return len({c["comment_id"] for c in comments if c.get("parent_comment_id") == root_id})

    for main in mains:
        root_id = main["comment_id"]
        # 主评论页可能带有 sub_comment_has_more 标记；没有标记时按 sub_comment_count 判断
        declared = int(main.get("sub_comment_count") or 0)
        if declared <= 0:
            continue

        def _short_of_declared() -> bool:
            return _unique_reply_count(root_id) < declared

        cursor = ""
        for _ in range(MAX_SUB_PAGES_PER_ROOT):
            try:
                sub_page = await client.get_note_sub_comments(
                    note_id=note_id,
                    root_comment_id=root_id,
                    xsec_token=xsec_token,
                    cursor=cursor,
                )
            except Exception:
                # 末页探测失败：已取满平台声明数即视为取尽，否则如实标记不完整
                if _short_of_declared():
                    sub_complete = False
                    warn("reply_incomplete", reasons)
                break
            rows = [r for r in sub_page.get("comments") or [] if isinstance(r, dict)]
            for row in rows:
                reply = core.normalize_comment(row, parent_comment_id=root_id)
                if reply:
                    comments.append(reply)
            if not sub_page.get("has_more"):
                break
            next_cursor = str(sub_page.get("cursor") or "")
            if not next_cursor:
                if _short_of_declared():
                    sub_complete = False
                    warn("reply_incomplete", reasons)
                break
            if next_cursor == cursor:
                # 平台在尽头会重复返回同一游标且 has_more 仍为真：该楼已无新数据，视为分页结束
                break
            cursor = next_cursor
            await asyncio.sleep(random.uniform(0.6, 1.2))
        else:
            # 达到单楼页数上限仍有更多：取满声明数则视为取尽，否则如实标记
            if _short_of_declared():
                sub_complete = False
                warn("reply_incomplete", reasons)
        progress(**_progress_snapshot(comments, 0, 0, 0))
        if len(comments) >= max_comments:
            warn("max_comments_reached", reasons)
            break

    progress(phase="downloading_images")
    download_live_images(store, comments, include_images, lambda code: warn(code, reasons), progress)

    progress(phase="extracting_voice")
    voice_run = voice_live(
        store,
        comments,
        url,
        note_id,
        include_voice,
        True,
        allow_open_page,
        lambda code: warn(code, reasons),
        progress,
    )

    return {
        "comments": comments,
        "note_meta": note_meta,
        "platform_total": platform_total,
        "pages_fetched": pages_fetched,
        "page_end_reached": page_end,
        "sub_pagination_complete": sub_complete,
        "reasons": reasons,
        "voice_run": voice_run,
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def _persist(store: Path, result: dict) -> None:
    _atomic_write_json(store / "comments.json", result)
    _atomic_write_text(store / "report.md", core.build_report(result))


def main() -> int:
    parser = argparse.ArgumentParser(description="采集单条小红书作品的评论（不触碰全局队列）")
    parser.add_argument("--job", required=True)
    parser.add_argument("--store-dir", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--note-id", default="")
    parser.add_argument("--fixture", default="")
    parser.add_argument("--include-images", action="store_true")
    parser.add_argument("--include-voice", action="store_true")
    parser.add_argument("--voice-allow-open-page", action="store_true")
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--max-comments", type=int, default=2000)
    args = parser.parse_args()

    job = JobState(Path(args.job))
    note_id = args.note_id or _note_id_from_url(args.url)
    if not core.note_id_valid(note_id):
        job.update(status="failed", phase="failed", error="作品 ID 无效，无法采集")
        return 2
    store = Path(args.store_dir) / note_id
    store.mkdir(parents=True, exist_ok=True)
    job.update(status="running", phase="starting", error=None, progress={"message": "开始采集这一条作品"})

    mode = "fixture" if args.fixture else "live"
    collector = {
        "mode": mode,
        "engine": "fixture" if mode == "fixture" else "mediacrawler_xhs_api",
        "started_at": core.now_iso(),
    }
    try:
        if mode == "fixture":
            collected = collect_fixture(
                fixture_dir=Path(args.fixture),
                store=store,
                include_images=args.include_images,
                include_voice=args.include_voice,
                max_pages=args.max_pages,
                max_comments=args.max_comments,
                job=job,
            )
        else:
            collected = collect_live(
                url=args.url,
                note_id=note_id,
                store=store,
                include_images=args.include_images,
                include_voice=args.include_voice,
                allow_open_page=args.voice_allow_open_page,
                max_pages=args.max_pages,
                max_comments=args.max_comments,
                job=job,
            )
    except CollectorError as exc:
        job.update(status="failed", phase="failed", error=exc.message)
        result = core.build_result(
            note_id=note_id,
            source_url=args.url,
            comments=[],
            note_meta=None,
            collector=collector,
            pages_fetched=0,
            page_end_reached=False,
            platform_total=None,
            sub_pagination_complete=False,
            reasons=[exc.code],
            fatal_error=exc.code,
            voice_run={"requested": args.include_voice, "status": "failed" if args.include_voice else "not_requested", "extracted": 0, "associated": 0, "unassociated": []},
        )
        _persist(store, result)
        job.update(result_available=True)
        return 1
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        job.update(status="failed", phase="failed", error=f"采集器异常退出（{type(exc).__name__}）")
        result = core.build_result(
            note_id=note_id,
            source_url=args.url,
            comments=[],
            note_meta=None,
            collector=collector,
            pages_fetched=0,
            page_end_reached=False,
            platform_total=None,
            sub_pagination_complete=False,
            reasons=["collector_error"],
            fatal_error="collector_error",
            voice_run={"requested": args.include_voice, "status": "failed" if args.include_voice else "not_requested", "extracted": 0, "associated": 0, "unassociated": []},
        )
        _persist(store, result)
        job.update(result_available=True)
        return 1

    job.update(phase="writing_result")
    collected_at = core.now_iso()
    result = core.build_result(
        note_id=note_id,
        source_url=args.url,
        comments=collected["comments"],
        note_meta=collected.get("note_meta"),
        collector=collector,
        pages_fetched=collected.get("pages_fetched", 0),
        page_end_reached=collected.get("page_end_reached", False),
        platform_total=collected.get("platform_total"),
        sub_pagination_complete=collected.get("sub_pagination_complete", False),
        reasons=collected.get("reasons") or [],
        voice_run=collected.get("voice_run"),
        collected_at=collected_at,
    )
    existing_path = store / "comments.json"
    existing = None
    if existing_path.is_file():
        try:
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = None
    result = core.merge_result(existing, result)
    result["collector"] = collector
    _persist(store, result)

    status = result["completeness"]["status"]
    job_status = {"complete": "completed", "incomplete": "incomplete", "failed": "failed"}[status]
    reason_texts = result["completeness"].get("reason_texts") or []
    failed_error = "；".join(reason_texts) if reason_texts else "本次未取得可用评论"
    job.update(
        phase="completed" if status != "failed" else "failed",
        status=job_status,
        error=None if status != "failed" else failed_error,
        result_available=True,
        progress={"message": "采集完成" if status == "complete" else "采集结束（结果不完整）" if status == "incomplete" else "采集失败"},
    )
    print(
        json.dumps(
            {
                "note_id": note_id,
                "status": status,
                "comments": result["counts"]["total_comments"],
                "pages_fetched": result["completeness"]["pages_fetched"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
