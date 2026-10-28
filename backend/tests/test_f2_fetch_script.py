"""f2_fetch.py 取数脚本核心逻辑单测（纯函数，不 import f2、不联网）。

假页面镜像真实 F2 Filter 的关键语义：has_more 缺失时 Filter 属性返回 False
（布尔强转），因此消费层必须检查原始响应的键是否存在。
覆盖：作品提取、严格原始响应校验（aweme_list/has_more/空页/提前结束）、
分页消费（complete 端点/去重/超量页/页数上限）、平台 status_code 识别、
异常翻译与脱敏、原子输出、文件锁。
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
from monitor.platforms.douyin_personal_source import redact_sensitive_text  # noqa: E402


class _FakePage:
    """镜像真实 Filter 语义：_to_raw() + status_code 属性（读原始响应）。"""

    def __init__(self, raw: dict):
        self._raw = raw

    def _to_raw(self) -> dict:
        return self._raw

    @property
    def status_code(self):
        return self._raw.get("status_code")

    @property
    def has_more(self) -> bool:  # 与真实 UserPostFilter 一致：缺失 → False
        return bool(self._raw.get("has_more"))


def _raw_page(ids: list[str], *, has_more=None, omit_has_more=False, omit_aweme_list=False,
              status_code=0) -> dict:
    raw = {"status_code": status_code}
    if not omit_aweme_list:
        raw["aweme_list"] = [
            {"aweme_id": i, "desc": f"作品{i}", "create_time": 1746000000 + int(i),
             "author": {"nickname": "作者", "sec_uid": "sec-1"},
             "video": {"cover": {"url_list": ["https://c.example/1.jpg"]}}}
            for i in ids
        ]
    if not omit_has_more:
        raw["has_more"] = has_more
    return raw


def _gen(pages: list[_FakePage]):
    async def _make_gen(handler):
        for page in pages:
            yield page
    return _make_gen


def _consume(pages, *, label="like", page_counts=20, max_pages=100, max_items=100):
    warnings: list[str] = []
    works, meta = asyncio.run(fetch.consume_video_pages(
        _gen(pages), label=label, requested_page_counts=page_counts,
        max_pages=max_pages, max_items=max_items, warnings=warnings))
    return works, meta, warnings


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


# ---------- consume_video_pages：严格原始响应 ----------

def test_consume_pages_complete_end_and_dedup():
    pages = [_FakePage(_raw_page(["1", "2"], has_more=1)), _FakePage(_raw_page(["2", "3"], has_more=0))]
    works, meta, warnings = _consume(pages)
    assert [w["external_id"] for w in works] == ["1", "2", "3"]  # 跨页去重
    assert meta == {"pages": 2, "items": 3, "complete": True, "server_oversize_pages": 0}
    assert warnings == []


def test_consume_pages_counts_oversize_pages():
    pages = [_FakePage(_raw_page(["1", "2", "3", "4", "5"], has_more=0))]
    works, meta, _ = _consume(pages, page_counts=2)
    assert meta["server_oversize_pages"] == 1
    assert len(works) == 5  # 超量返回全部接受，不丢数据


def test_consume_pages_caps_at_max_pages_with_warning():
    pages = [_FakePage(_raw_page([str(i)], has_more=1)) for i in range(5)]
    works, meta, warnings = _consume(pages, max_pages=3)
    assert meta["pages"] == 3 and meta["complete"] is False
    assert any("页数上限" in w for w in warnings)


def test_consume_pages_missing_aweme_list_is_error_not_empty():
    """响应缺 aweme_list 字段 → 平台错误，绝不当作 0 条成功。"""
    pages = [_FakePage(_raw_page([], has_more=0, omit_aweme_list=True))]
    with pytest.raises(fetch.ScopeError) as excinfo:
        _consume(pages)
    assert excinfo.value.kind == "platform_error"
    assert "aweme_list" in str(excinfo.value)


def test_consume_pages_zero_pages_is_error_not_success():
    """上游在 yield 前结束（真实 F2 生成器吞错场景）→ 显式失败。"""
    with pytest.raises(fetch.ScopeError) as excinfo:
        _consume([])
    assert excinfo.value.kind == "platform_error"


def test_consume_pages_premature_end_is_incomplete():
    """最后一页 has_more=1 但生成器耗尽 → 不完整，不冒充成功。"""
    pages = [_FakePage(_raw_page(["1"], has_more=1)), _FakePage(_raw_page(["2"], has_more=1))]
    works, meta, warnings = _consume(pages, max_pages=100)
    assert meta["complete"] is False
    assert any("提前结束" in w for w in warnings)
    assert len(works) == 2


def test_consume_pages_missing_has_more_is_incomplete():
    """真实 Filter 会把缺失 has_more 强转 False：必须看原始响应键。"""
    pages = [_FakePage(_raw_page(["1"], omit_has_more=True))]
    works, meta, warnings = _consume(pages)
    assert meta["complete"] is False
    assert any("has_more" in w for w in warnings)


def test_consume_pages_payload_status_8_maps_to_login_expired():
    pages = [_FakePage(_raw_page(["1"], has_more=0, status_code=8))]
    with pytest.raises(fetch.ScopeError) as excinfo:
        _consume(pages)
    assert excinfo.value.kind == "login_expired"


def test_consume_pages_other_payload_status_is_platform_error():
    pages = [_FakePage(_raw_page(["1"], has_more=0, status_code=2483))]
    with pytest.raises(fetch.ScopeError) as excinfo:
        _consume(pages)
    assert excinfo.value.kind == "platform_error"


def test_scope_error_translation_with_real_signature_and_redaction():
    """桩类型验证翻译规则 + 消息脱敏（合成令牌不得出现在消息里）。"""
    import types

    stub = types.ModuleType("f2.exceptions.api_exceptions")

    class APIRetryExhaustedError(Exception): ...
    class APIUnauthorizedError(Exception): ...
    class APIRateLimitError(Exception): ...
    stub.APIRetryExhaustedError = APIRetryExhaustedError
    stub.APIUnauthorizedError = APIUnauthorizedError
    stub.APIRateLimitError = APIRateLimitError
    sys.modules["f2.exceptions.api_exceptions"] = stub
    try:
        err = fetch.scope_error_from_exception(APIRetryExhaustedError("x"), "like", "like")
        assert err.kind == "like_list_unavailable"
        err = fetch.scope_error_from_exception(APIRetryExhaustedError("x"), "collection", "collection")
        assert err.kind == "platform_error"
        err = fetch.scope_error_from_exception(APIUnauthorizedError("x"), "like", "like")
        assert err.kind == "login_expired"
        err = fetch.scope_error_from_exception(APIRateLimitError("x"), "like", "like")
        assert err.kind == "rate_limited"
        leak = RuntimeError("synthetic msToken=FAKE_SECRET_VALUE&sec_user_id=MS4wLjABAAAAdeadbeef1234")
        err = fetch.scope_error_from_exception(leak, "like", "like")
        assert err.kind == "platform_error"
        assert "FAKE_SECRET_VALUE" not in err.args[0]
        assert "MS4wLjABAAAA" not in err.args[0]
    finally:
        sys.modules.pop("f2.exceptions.api_exceptions", None)


def test_redact_covers_tokens_and_sec_uid():
    text = "url https://x.douyin.com/?msToken=SECRET123&a_bogus=XYZ789 cookie: sessionid=ABC; ok MS4wLjABAAAAzhangsan09"
    out = redact_sensitive_text(text)
    assert "SECRET123" not in out and "XYZ789" not in out and "ABC" not in out
    assert "MS4wLjABAAAA" not in out
    assert "https://x.douyin.com/" in out  # 路径信息保留，可诊断


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
