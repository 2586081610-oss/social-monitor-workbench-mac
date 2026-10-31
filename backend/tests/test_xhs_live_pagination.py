"""真实路径分页语义回归测试：平台在尽头重复返回同一游标时不得误报不完整/中断。

背景（2026-10-31 真实验收发现）：小红书子回复接口在取尽后可能仍返回 has_more=true 且
游标与上一页相同——旧行为把它当成"翻页中断"，导致数据实际齐全却被标为 incomplete。
新语义：紧邻游标重复=分页结束；回跳到更早游标=真中断；空游标=中断。
漏取仍有兜底：reply_shortfall 按平台声明的回复数逐楼核对。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.xhs_comment_collect import _collect_live_async  # noqa: E402


def _comment(cid: str, *, sub_count: int = 0) -> dict:
    return {"id": cid, "content": f"内容{cid}", "like_count": 1, "sub_comment_count": sub_count}


def _noop_progress(**kwargs) -> None:
    return None


def _noop_warn(code: str, target: list[str]) -> None:
    if code not in target:
        target.append(code)


class FakeLiveClient:
    """按序吐出预置页面；行为与 XiaoHongShuClient 的调用面保持一致。"""

    def __init__(self, main_pages: list[dict], sub_pages: dict[str, list[dict]] | None = None) -> None:
        self.main_pages = main_pages
        self.sub_pages = sub_pages or {}
        self._main_idx = 0
        self._sub_idx: dict[str, int] = {}

    async def get_note_by_id(self, note_id: str, xsec_source: str, xsec_token: str):  # noqa: ANN201
        return None

    async def get_note_comments(self, note_id: str, xsec_token: str, cursor: str = ""):  # noqa: ANN201
        page = self.main_pages[self._main_idx]
        self._main_idx += 1
        return page

    async def get_note_sub_comments(self, note_id: str, root_comment_id: str, xsec_token: str, cursor: str = "", num: int = 10):  # noqa: ANN201
        pages = self.sub_pages[root_comment_id]
        idx = self._sub_idx.get(root_comment_id, 0)
        self._sub_idx[root_comment_id] = idx + 1
        return pages[idx]

    async def close(self) -> None:
        return None


async def _run(client: FakeLiveClient, tmp_path: Path) -> dict:
    return await _collect_live_async(
        client=client,
        url="https://www.xiaohongshu.com/explore/6adf401300000000503a3b8",
        note_id="6adf401300000000503a3b8",
        store=tmp_path,
        xsec_token="token",
        xsec_source="pc_search",
        include_images=False,
        include_voice=False,
        allow_open_page=False,
        max_pages=50,
        max_comments=2000,
        warn=_noop_warn,
        progress=_noop_progress,
    )


def test_sub_page_repeated_last_cursor_means_end_not_incomplete(tmp_path: Path) -> None:
    """子回复尽头重复同一游标：视为分页结束，不误报 reply_incomplete。"""
    client = FakeLiveClient(
        main_pages=[
            {"comments": [_comment("m1", sub_count=2)], "has_more": False, "cursor": "", "total": 3},
        ],
        sub_pages={
            "m1": [
                {"comments": [_comment("r1"), _comment("r2")], "has_more": True, "cursor": "c1"},
                # 平台尽头：重复同一页内容与游标，has_more 仍为真
                {"comments": [_comment("r1"), _comment("r2")], "has_more": True, "cursor": "c1"},
            ],
        },
    )
    result = asyncio.run(_run(client, tmp_path))
    assert result["sub_pagination_complete"] is True
    assert "reply_incomplete" not in result["reasons"]
    replies = [c for c in result["comments"] if c["parent_comment_id"]]
    assert len(replies) == 4  # 原始两条 × 2 页（去重在 build_result 阶段做）
    assert {c["comment_id"] for c in replies} == {"r1", "r2"}


def test_sub_page_empty_cursor_still_incomplete(tmp_path: Path) -> None:
    """has_more 为真但游标为空：如实标记不完整。"""
    client = FakeLiveClient(
        main_pages=[
            {"comments": [_comment("m1", sub_count=5)], "has_more": False, "cursor": ""},
        ],
        sub_pages={
            "m1": [
                {"comments": [_comment("r1")], "has_more": True, "cursor": ""},
            ],
        },
    )
    result = asyncio.run(_run(client, tmp_path))
    assert result["sub_pagination_complete"] is False
    assert "reply_incomplete" in result["reasons"]


def test_main_page_repeated_last_cursor_means_end_not_interrupted(tmp_path: Path) -> None:
    """主评论尽头重复同一游标：视为分页结束，不误报 page_interrupted。"""
    client = FakeLiveClient(
        main_pages=[
            {"comments": [_comment("m1")], "has_more": True, "cursor": "k1"},
            {"comments": [_comment("m1")], "has_more": True, "cursor": "k1"},
        ],
    )
    result = asyncio.run(_run(client, tmp_path))
    assert result["page_end_reached"] is True
    assert "page_interrupted" not in result["reasons"]


def test_main_page_cursor_jump_back_is_real_interruption(tmp_path: Path) -> None:
    """游标回跳到更早页：真中断，如实标记 page_interrupted。"""
    client = FakeLiveClient(
        main_pages=[
            {"comments": [_comment("m1")], "has_more": True, "cursor": "k1"},
            {"comments": [_comment("m2")], "has_more": True, "cursor": "k2"},
            {"comments": [_comment("m3")], "has_more": True, "cursor": "k1"},
        ],
    )
    result = asyncio.run(_run(client, tmp_path))
    assert result["page_end_reached"] is False
    assert "page_interrupted" in result["reasons"]


def test_sub_page_earlier_cursor_jump_is_real_incomplete(tmp_path: Path) -> None:
    """子回复游标回跳到更早页（非紧邻重复）：真不完整。"""
    client = FakeLiveClient(
        main_pages=[
            {"comments": [_comment("m1", sub_count=9)], "has_more": False, "cursor": ""},
        ],
        sub_pages={
            "m1": [
                {"comments": [_comment("r1"), _comment("r2")], "has_more": True, "cursor": "s1"},
                {"comments": [_comment("r3")], "has_more": True, "cursor": "s2"},
                {"comments": [_comment("r4")], "has_more": True, "cursor": "s1"},
            ],
        },
    )
    result = asyncio.run(_run(client, tmp_path))
    assert result["sub_pagination_complete"] is False
    assert "reply_incomplete" in result["reasons"]


class _FailingLastPageClient(FakeLiveClient):
    """子回复末页请求抛异常（平台尽头行为的另一种表现）。"""

    def __init__(self, main_pages, sub_pages) -> None:
        super().__init__(main_pages, sub_pages)

    async def get_note_sub_comments(self, note_id: str, root_comment_id: str, xsec_token: str, cursor: str = "", num: int = 10):  # noqa: ANN201
        pages = self.sub_pages[root_comment_id]
        idx = self._sub_idx.get(root_comment_id, 0)
        self._sub_idx[root_comment_id] = idx + 1
        if idx >= len(pages):
            raise RuntimeError("boom")
        return pages[idx]


def test_sub_page_final_probe_failure_with_declared_met_is_complete(tmp_path: Path) -> None:
    """已取满平台声明回复数后末页探测失败：视为取尽，不误报。"""
    client = _FailingLastPageClient(
        main_pages=[
            {"comments": [_comment("m1", sub_count=3)], "has_more": False, "cursor": ""},
        ],
        sub_pages={
            "m1": [
                {"comments": [_comment("r1"), _comment("r2")], "has_more": True, "cursor": "s1"},
                {"comments": [_comment("r3")], "has_more": True, "cursor": "s2"},
            ],
        },
    )
    result = asyncio.run(_run(client, tmp_path))
    assert result["sub_pagination_complete"] is True
    assert "reply_incomplete" not in result["reasons"]


def test_sub_page_failure_with_deficit_is_incomplete(tmp_path: Path) -> None:
    """未取满声明数就失败：如实标记不完整。"""
    client = _FailingLastPageClient(
        main_pages=[
            {"comments": [_comment("m1", sub_count=9)], "has_more": False, "cursor": ""},
        ],
        sub_pages={
            "m1": [
                {"comments": [_comment("r1"), _comment("r2")], "has_more": True, "cursor": "s1"},
                {"comments": [_comment("r3")], "has_more": True, "cursor": "s2"},
            ],
        },
    )
    result = asyncio.run(_run(client, tmp_path))
    assert result["sub_pagination_complete"] is False
    assert "reply_incomplete" in result["reasons"]


def test_sub_page_ceiling_with_declared_met_is_complete(tmp_path: Path) -> None:
    """达到单楼页数上限但声明数已取齐：不误报。"""
    client = FakeLiveClient(
        main_pages=[
            {"comments": [_comment("m1", sub_count=2)], "has_more": False, "cursor": ""},
        ],
        sub_pages={
            "m1": [
                {"comments": [_comment(f"r{i}") for i in range(10)], "has_more": True, "cursor": "s1"},
                {"comments": [_comment("r1"), _comment("r2")], "has_more": True, "cursor": "s2"},
            ],
        },
    )
    result = asyncio.run(_run(client, tmp_path))
    assert result["sub_pagination_complete"] is True
    assert "reply_incomplete" not in result["reasons"]


from monitor.platforms import xhs_comments as core  # noqa: E402


def test_expand_comments_uses_real_wheel_and_stops_on_stalled_list(monkeypatch, tmp_path):
    """语音滚动：必须发真实滚轮事件；渲染条数停滞后停止，不死循环。"""
    import tools.xhs_comment_collect as collector

    monkeypatch.setattr(collector, "VOICE_SCROLL_INTERVAL", 0)
    monkeypatch.setattr(collector, "VOICE_SCROLL_MAX_ROUNDS", 50)

    class FakeMouse:
        def __init__(self) -> None:
            self.moves = 0
            self.wheels = 0

        def move(self, x: int, y: int) -> None:
            self.moves += 1

        def wheel(self, dx: int, dy: int) -> None:
            assert dy > 0
            self.wheels += 1

    class FakePage:
        def __init__(self, rounds: int) -> None:
            self.mouse = FakeMouse()
            self._round = 0
            self._rounds = rounds

        def evaluate(self, js: str, *args):  # noqa: ANN001, ANN202
            self._round += 1
            if "scrollHeight > el.clientHeight" in js:
                return {"x": 100, "y": 200, "items": self._round}
            if "voiceCount" in js:
                items = min(self._round, self._rounds)
                return {"items": items, "voiceCount": 0}
            if "show-more" in js:
                return min(self._round, self._rounds)
            if ".audio-comment-container" in js:
                return []
            return {}

    page = FakePage(rounds=6)
    collector._expand_comments(page)
    assert page.mouse.wheels >= 6
    assert page.mouse.moves >= 1
    # 列表在第 6 轮后停滞，STALE_LIMIT=8 → 约 14 轮内停止，远小于 50 上限
    assert page.mouse.wheels < 50


def test_voice_rows_carry_comment_id_for_exact_association():
    """语音行提取应带评论元素 id 里的 comment_id，走精确关联而不是启发式。"""
    comment = core.normalize_comment({"id": "r9", "content": "", "like_count": "2", "target_comment_id": "m9", "user_info": {"nickname": "用户r9"}})
    rows = [{"text": "转写文本", "nickname": "用户r9", "like_count": "2", "comment_id": "r9"}]
    attached, unassociated = core.associate_voice([comment], rows)
    assert attached == 1
    assert comment["voice"]["status"] == "transcript_available"
    assert comment["voice"]["association"] == "exact"
    assert not unassociated
