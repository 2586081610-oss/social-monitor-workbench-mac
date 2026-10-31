"""A 方案页面复用单元测试：只读已打开作品页，未允许时不新开页。"""
from __future__ import annotations

from monitor.platforms.xhs_voice import (
    CDP_DEFAULT,
    acquire_note_page,
    decide_page_mode,
    match_note_page,
)

NOTE = "6aabbccd0000000000000011"


class FakePage:
    def __init__(self, url: str = "") -> None:
        self.url = url
        self.goto_calls: list[tuple[str, dict]] = []
        self.closed = False
        self.goto_error: Exception | None = None

    def goto(self, url: str, **kwargs) -> None:
        self.goto_calls.append((url, kwargs))
        if self.goto_error is not None:
            raise self.goto_error

    def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, pages: list[FakePage] | None = None) -> None:
        self.pages = pages or []
        self.created_pages: list[FakePage] = []

    def new_page(self) -> FakePage:
        page = FakePage()
        self.created_pages.append(page)
        return page


class FakeBrowser:
    def __init__(self, contexts: list[FakeContext] | None = None) -> None:
        self.contexts = contexts or []
        self.created_contexts: list[FakeContext] = []

    def new_context(self) -> FakeContext:
        context = FakeContext()
        self.created_contexts.append(context)
        return context


def test_default_cdp_endpoint_is_local():
    assert CDP_DEFAULT == "http://127.0.0.1:9222"


def test_match_note_page_accepts_explore_and_discovery_only():
    explore = FakePage(f"https://www.xiaohongshu.com/explore/{NOTE}")
    discovery = FakePage(f"https://www.xiaohongshu.com/discovery/item/{NOTE}?xsec_token=synthetic")
    other_note = FakePage("https://www.xiaohongshu.com/explore/6aabbccd0000000000000099")
    profile = FakePage("https://www.xiaohongshu.com/user/profile/abc")
    foreign = FakePage(f"https://example.com/explore/{NOTE}")

    page, count = match_note_page([profile, other_note, explore, discovery, foreign], NOTE)

    assert page is explore
    assert count == 2


def test_match_note_page_does_not_match_prefix_collisions():
    page = FakePage(f"https://www.xiaohongshu.com/explore/{NOTE}extra")
    matched, count = match_note_page([page], NOTE)
    assert matched is None
    assert count == 0


def test_decide_page_mode_matrix():
    assert decide_page_mode(cdp_available=False, found_page=True, allow_open_page=True) == "cdp_unavailable"
    assert decide_page_mode(cdp_available=True, found_page=True, allow_open_page=False) == "reused"
    assert decide_page_mode(cdp_available=True, found_page=False, allow_open_page=False) == "not_found"
    assert decide_page_mode(cdp_available=True, found_page=False, allow_open_page=True) == "opened"


def test_acquire_reuses_open_page_without_navigation_or_new_tab():
    target = FakePage(f"https://www.xiaohongshu.com/explore/{NOTE}")
    context = FakeContext([target])
    browser = FakeBrowser([context])

    acquisition = acquire_note_page(
        browser,
        note_id=NOTE,
        note_url=target.url,
        allow_open_page=True,
    )

    assert acquisition.page is target
    assert acquisition.page_mode == "reused"
    assert acquisition.opened_new_page is False
    assert acquisition.pages_scanned == 1
    assert acquisition.matched_page_count == 1
    assert target.goto_calls == []
    assert context.created_pages == []


def test_acquire_does_not_open_when_not_allowed():
    browser = FakeBrowser([FakeContext([])])

    acquisition = acquire_note_page(
        browser,
        note_id=NOTE,
        note_url=f"https://www.xiaohongshu.com/explore/{NOTE}",
        allow_open_page=False,
    )

    assert acquisition.page is None
    assert acquisition.page_mode == "not_found"
    assert acquisition.opened_new_page is False
    assert "未找到已打开的作品页" in (acquisition.failure_reason or "")
    assert browser.contexts[0].created_pages == []


def test_acquire_opens_only_one_tab_when_allowed():
    context = FakeContext([])
    browser = FakeBrowser([context])
    note_url = f"https://www.xiaohongshu.com/explore/{NOTE}"

    acquisition = acquire_note_page(
        browser,
        note_id=NOTE,
        note_url=note_url,
        allow_open_page=True,
    )

    assert acquisition.page is not None
    assert acquisition.page_mode == "opened"
    assert acquisition.opened_new_page is True
    assert len(context.created_pages) == 1
    assert acquisition.page.goto_calls == [(note_url, {"wait_until": "domcontentloaded", "timeout": 60000})]


def test_acquire_closes_new_tab_when_navigation_fails():
    context = FakeContext([])
    browser = FakeBrowser([context])
    note_url = f"https://www.xiaohongshu.com/explore/{NOTE}"

    original_new_page = context.new_page

    def new_page_with_error() -> FakePage:
        page = original_new_page()
        page.goto_error = TimeoutError("synthetic")
        return page

    context.new_page = new_page_with_error  # type: ignore[method-assign]

    acquisition = acquire_note_page(
        browser,
        note_id=NOTE,
        note_url=note_url,
        allow_open_page=True,
    )

    assert acquisition.page is None
    assert acquisition.page_mode == "opened_failed"
    assert acquisition.opened_new_page is True
    assert context.created_pages[0].closed is True
    assert "打开作品页失败" in (acquisition.failure_reason or "")


def test_acquire_scans_all_contexts_but_returns_first_match():
    first = FakePage("https://www.xiaohongshu.com/explore/6aabbccd0000000000000099")
    second = FakePage(f"https://www.xiaohongshu.com/discovery/item/{NOTE}")
    browser = FakeBrowser([FakeContext([first]), FakeContext([second])])

    acquisition = acquire_note_page(browser, note_id=NOTE, note_url=second.url, allow_open_page=False)

    assert acquisition.page is second
    assert acquisition.pages_scanned == 2
    assert acquisition.page_mode == "reused"
