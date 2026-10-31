"""小红书评论语音：复用已打开浏览器页面（CDP）的页面查找与获取逻辑。

本模块不负责 DOM 提取，只负责：
- 判断哪个已打开标签页是目标作品页；
- 决定复用 / 新开 / 未找到 / CDP 不可用；
- 在允许新开时创建并导航一个新标签页（失败时关闭并返回明确原因）。

不读取无关标签页内容，不关闭用户浏览器。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

CDP_DEFAULT = "http://127.0.0.1:9222"
XHS_HOSTS = {"xiaohongshu.com", "www.xiaohongshu.com"}
NOTE_PATH_PREFIXES = ("/explore/", "/discovery/item/")


@dataclass
class PageAcquisition:
    page: Any | None
    page_mode: str
    pages_scanned: int
    matched_page_count: int
    opened_new_page: bool
    failure_reason: str | None = None


def _page_url(page: Any) -> str:
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:
        return ""


def match_note_page(pages: list[Any], note_id: str) -> tuple[Any | None, int]:
    """按 URL 路径精确匹配目标作品页；返回 (第一个匹配页, 匹配数量)。"""
    matches: list[Any] = []
    for page in pages:
        url = _page_url(page)
        if not url:
            continue
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        host = (parts.hostname or "").lower()
        if host not in XHS_HOSTS:
            continue
        path = parts.path.rstrip("/")
        for prefix in NOTE_PATH_PREFIXES:
            exact = f"{prefix}{note_id}"
            if path == exact or path.startswith(exact + "/"):
                matches.append(page)
                break
    return (matches[0] if matches else None, len(matches))


def decide_page_mode(*, cdp_available: bool, found_page: bool, allow_open_page: bool) -> str:
    """页面模式决策：cdp_unavailable / reused / opened / not_found。"""
    if not cdp_available:
        return "cdp_unavailable"
    if found_page:
        return "reused"
    if allow_open_page:
        return "opened"
    return "not_found"


def acquire_note_page(
    browser: Any,
    *,
    note_id: str,
    note_url: str,
    allow_open_page: bool,
) -> PageAcquisition:
    """优先复用已打开的目标作品页；仅在明确允许时新开一个标签页。"""
    contexts: list[Any] = []
    try:
        contexts = list(getattr(browser, "contexts", None) or [])
    except Exception:
        contexts = []
    pages: list[Any] = []
    for context in contexts:
        try:
            pages.extend(list(getattr(context, "pages", None) or []))
        except Exception:
            continue

    page, matched = match_note_page(pages, note_id)
    if page is not None:
        return PageAcquisition(page, "reused", len(pages), matched, False)
    if not allow_open_page:
        return PageAcquisition(
            None,
            "not_found",
            len(pages),
            matched,
            False,
            "未找到已打开的作品页，本次不新开页面；请先在浏览器打开该作品后重试",
        )

    try:
        context = contexts[0] if contexts else browser.new_context()
        new_page = context.new_page()
    except Exception as exc:
        return PageAcquisition(
            None,
            "opened_failed",
            len(pages),
            matched,
            True,
            f"无法创建作品标签页（{type(exc).__name__}）",
        )
    try:
        new_page.goto(note_url, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        try:
            new_page.close()
        except Exception:
            pass
        return PageAcquisition(
            None,
            "opened_failed",
            len(pages),
            matched,
            True,
            f"打开作品页失败（{type(exc).__name__}）",
        )
    return PageAcquisition(new_page, "opened", len(pages), matched, True)
