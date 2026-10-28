"""f2_fetch.py 取数脚本核心逻辑单测（纯函数，不 import f2、不联网）。

覆盖：作品提取、分页消费（has_more/页数上限/超量页/去重）、平台 status_code
的登录过期识别、错误翻译。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

from monitor.platforms import f2_fetch as fetch  # noqa: E402


class _FakePage:
    """模拟 F2 Filter：_to_raw + status_code + has_more。"""

    def __init__(self, raw: dict, has_more=None, status_code=0):
        self._raw = raw
        self._has_more = has_more
        self.status_code = status_code

    def _to_raw(self) -> dict:
        return self._raw

    @property
    def has_more(self):
        return self._has_more


def _raw_page(ids: list[str]) -> dict:
    return {"aweme_list": [{"aweme_id": i, "desc": f"作品{i}", "create_time": 1746000000 + int(i),
                            "author": {"nickname": "作者", "sec_uid": "sec-1"},
                            "video": {"cover": {"url_list": ["https://c.example/1.jpg"]}}} for i in ids]}


def _gen(pages: list[_FakePage]):
    async def _make_gen(handler):
        for page in pages:
            yield page
    return _make_gen


def test_extract_work_video_and_image_shapes():
    video = {"aweme_id": "1", "desc": "标题", "create_time": 1746000000,
             "author": {"nickname": "作者", "sec_uid": "sec-1"},
             "video": {"cover": {"url_list": ["https://c.example/v.jpg"]}}}
    work = fetch.extract_work(video)[0]
    assert work["external_id"] == "1" and work["media_type"] == "video"
    assert work["published_at"] == "2025-04-30T08:00:00Z"
    assert work["url"] == "https://www.douyin.com/video/1"

    image = {"aweme_id": 2, "images": [{"url_list": ["https://c.example/i.jpg"]}], "desc": ""}
    work = fetch.extract_work(image)[0]
    assert work["external_id"] == "2" and work["media_type"] == "image"
    assert work["cover_url"] == "https://c.example/i.jpg"
    assert work["title"] is None  # 空 desc → None，不是空串


def test_extract_work_missing_identity_or_garbage():
    assert fetch.extract_work({"desc": "无ID"})[1]["reason"] == "missing_identity"
    assert fetch.extract_work("not-a-dict")[1]["reason"] == "not_an_object"
    assert fetch.extract_work({"aweme_id": None, "desc": "x"})[1]["reason"] == "missing_identity"


def test_extract_work_bad_create_time_is_unknown():
    work = fetch.extract_work({"aweme_id": "9", "create_time": "bad", "desc": "t"})[0]
    assert work["published_at"] is None
    work = fetch.extract_work({"aweme_id": "9", "create_time": 0, "desc": "t"})[0]
    assert work["published_at"] is None


def test_consume_pages_complete_end_and_dedup():
    pages = [_FakePage(_raw_page(["1", "2"]), has_more=True), _FakePage(_raw_page(["2", "3"]), has_more=False)]
    warnings: list[str] = []
    works, meta = asyncio.run(fetch.consume_video_pages(
        None, _gen(pages), label="like", requested_page_counts=20,
        max_pages=100, max_items=100, warnings=warnings))
    assert [w["external_id"] for w in works] == ["1", "2", "3"]  # 跨页去重
    assert meta == {"pages": 2, "items": 3, "complete": True, "server_oversize_pages": 0}


def test_consume_pages_counts_oversize_pages():
    pages = [_FakePage(_raw_page(["1", "2", "3", "4", "5"]), has_more=False)]  # 请求 2 返回 5
    works, meta = asyncio.run(fetch.consume_video_pages(
        None, _gen(pages), label="like", requested_page_counts=2,
        max_pages=10, max_items=100, warnings=[]))
    assert meta["server_oversize_pages"] == 1
    assert len(works) == 5  # 超量返回全部接受，不丢数据


def test_consume_pages_caps_at_max_pages_with_warning():
    pages = [_FakePage(_raw_page([str(i)]), has_more=True) for i in range(5)]
    warnings: list[str] = []
    works, meta = asyncio.run(fetch.consume_video_pages(
        None, _gen(pages), label="collection", requested_page_counts=20,
        max_pages=3, max_items=100, warnings=warnings))
    assert meta["pages"] == 3 and meta["complete"] is False
    assert any("页数上限" in w for w in warnings)


def test_consume_pages_missing_has_more_is_incomplete():
    pages = [_FakePage(_raw_page(["1"]), has_more=None)]
    warnings: list[str] = []
    works, meta = asyncio.run(fetch.consume_video_pages(
        None, _gen(pages), label="like", requested_page_counts=20,
        max_pages=10, max_items=100, warnings=warnings))
    assert meta["complete"] is False and any("has_more" in w for w in warnings)


def test_consume_pages_payload_status_8_maps_to_login_expired():
    pages = [_FakePage(_raw_page(["1"]), has_more=False, status_code=8)]
    with pytest.raises(fetch.ScopeError) as excinfo:
        asyncio.run(fetch.consume_video_pages(
            None, _gen(pages), label="collection", requested_page_counts=20,
            max_pages=10, max_items=100, warnings=[]))
    assert excinfo.value.kind == "login_expired"


def test_consume_pages_other_payload_status_is_platform_error():
    pages = [_FakePage(_raw_page(["1"]), has_more=False, status_code=2483)]
    with pytest.raises(fetch.ScopeError) as excinfo:
        asyncio.run(fetch.consume_video_pages(
            None, _gen(pages), label="like", requested_page_counts=20,
            max_pages=10, max_items=100, warnings=[]))
    assert excinfo.value.kind == "platform_error"


def test_scope_error_translation_with_stubs():
    """用桩类型验证翻译规则（主环境无 f2，函数延迟导入桩模块）。"""
    import types

    stub = types.ModuleType("f2.exceptions.api_exceptions")
    class APIRetryExhaustedError(Exception): ...
    class APIUnauthorizedError(Exception): ...
    class APIRateLimitError(Exception): ...
    stub.APIRetryExhaustedError = APIRetryExhaustedError
    stub.APIUnauthorizedError = APIUnauthorizedError
    stub.APIRateLimitError = APIRateLimitError
    sys.modules["f2.exceptions.api_exceptions"] = stub
    sys.modules["f2"] = types.ModuleType("f2")
    sys.modules["f2.exceptions"] = types.ModuleType("f2.exceptions")
    try:
        err = fetch.scope_error_from_exception(APIRetryExhaustedError("x"), "like")
        assert err.kind == "like_list_unavailable"
        err = fetch.scope_error_from_exception(APIUnauthorizedError("x"), "like")
        assert err.kind == "login_expired"
        err = fetch.scope_error_from_exception(APIRateLimitError("x"), "like")
        assert err.kind == "rate_limited"
        err = fetch.scope_error_from_exception(ValueError("x"), "like")
        assert err.kind == "platform_error"
    finally:
        for key in ("f2", "f2.exceptions", "f2.exceptions.api_exceptions"):
            sys.modules.pop(key, None)


def test_write_output_is_atomic_json(tmp_path):
    target = tmp_path / "sub" / "result.json"
    fetch.write_output(str(target), {"ok": True, "数据": "测试"})
    assert json.loads(target.read_text(encoding="utf-8"))["ok"] is True
    assert not target.with_name(target.name + ".tmp").exists()


def test_acquire_lock_blocks_second_holder(tmp_path):
    lock = str(tmp_path / "sync.lock")
    fetch.acquire_lock(lock)  # 第一次成功
    with pytest.raises(fetch.ScopeError) as excinfo:
        fetch.acquire_lock(lock)
    assert excinfo.value.kind == "already_running"
