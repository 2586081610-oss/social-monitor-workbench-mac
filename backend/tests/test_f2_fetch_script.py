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
import os
import sys
from pathlib import Path

import pytest

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

import json as _json  # noqa: E402

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


def test_redirect_refused_maps_to_explicit_failure_without_raw_text():
    exc = fetch.RedirectRefused("https://outside.invalid/?token=SECRET_LOCATION")
    err = fetch.scope_error_from_exception(exc, "like", "like")
    assert err.kind == "redirect_refused"
    assert "SECRET_LOCATION" not in str(err) and "https://" not in str(err)
    identity_err = fetch._self_identity_error(exc)
    assert identity_err.kind == "redirect_refused"
    assert "SECRET_LOCATION" not in str(identity_err)


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


# ---------- 列表接口使用固定版 F2 官方请求头 ----------

_OFFICIAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)
_OFFICIAL_REFERER = "https://www.douyin.com/"


def _install_f2_conf_stub(monkeypatch, *, headers, browser=None, with_manager=True,
                          handler=None):
    """最小 f2 模块桩：只提供 ClientConfManager（以及可选的 handler）。"""
    import types

    f2 = types.ModuleType("f2")
    f2.__path__ = []
    apps = types.ModuleType("f2.apps")
    apps.__path__ = []
    douyin = types.ModuleType("f2.apps.douyin")
    douyin.__path__ = []
    utils_mod = types.ModuleType("f2.apps.douyin.utils")
    if with_manager:
        class ClientConfManager:
            _headers: dict = {}
            _browser: dict = {}

            @classmethod
            def headers(cls):
                return cls._headers

            @classmethod
            def brm_browser(cls):
                return cls._browser
        ClientConfManager._headers = headers
        ClientConfManager._browser = browser if browser is not None else {}
        utils_mod.ClientConfManager = ClientConfManager
    douyin.utils = utils_mod
    handler_mod = None
    if handler is not None:
        handler_mod = types.ModuleType("f2.apps.douyin.handler")
        handler_mod.DouyinHandler = handler
        handler_mod.rich_console = type("RichConsole", (), {"quiet": False})()
        douyin.handler = handler_mod
    # 最小 crawler 桩：run_scopes 会用其子类禁止跟随重定向（本文件不触发真实请求）
    crawler_mod = types.ModuleType("f2.apps.douyin.crawler")

    class DouyinCrawler:
        headers: dict = {}

        def __init__(self, kwargs=None):
            self.kwargs = kwargs or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    crawler_mod.DouyinCrawler = DouyinCrawler
    douyin.crawler = crawler_mod
    if handler_mod is not None:
        handler_mod.DouyinCrawler = DouyinCrawler
    apps.douyin = douyin
    f2.apps = apps
    modules = [("f2", f2), ("f2.apps", apps), ("f2.apps.douyin", douyin),
               ("f2.apps.douyin.utils", utils_mod),
               ("f2.apps.douyin.crawler", crawler_mod)]
    if handler_mod is not None:
        modules.append(("f2.apps.douyin.handler", handler_mod))
    for name, module in modules:
        monkeypatch.setitem(sys.modules, name, module)


def _official_headers(**overrides):
    headers = {"User-Agent": _OFFICIAL_UA, "Referer": _OFFICIAL_REFERER}
    headers.update(overrides)
    return headers


def test_official_list_headers_copies_shared_config(monkeypatch):
    """返回安全副本：既有 UA/Referer 被采用，且不把共享配置字典暴露给下游。"""
    shared = _official_headers()
    _install_f2_conf_stub(monkeypatch, headers=shared,
                          browser={"name": "Edge", "version": "130.0.0.0"})
    result = fetch.official_list_headers()
    assert result == shared
    assert result is not shared
    result["User-Agent"] = "MUTATED"
    assert shared["User-Agent"] == _OFFICIAL_UA  # 未改写 F2 共享配置


@pytest.mark.parametrize("case", ["missing_ua", "missing_referer", "non_dict",
                                  "non_str_value", "wrong_referer", "ua_model_mismatch"])
def test_official_list_headers_refuses_missing_or_inconsistent_config(case, monkeypatch):
    """配置缺失/类型错误/站点不对/与模型浏览器不一致 → 固定 kind 明确失败。"""
    headers = _official_headers()
    browser = {"name": "Edge", "version": "130.0.0.0"}
    if case == "missing_ua":
        headers.pop("User-Agent")
    elif case == "missing_referer":
        headers.pop("Referer")
    elif case == "non_dict":
        headers = ["not-a-mapping"]
    elif case == "non_str_value":
        headers["x-extra"] = 123
    elif case == "wrong_referer":
        headers["Referer"] = "https://example.test/"
    elif case == "ua_model_mismatch":
        headers["User-Agent"] = "Mozilla/5.0 (Macintosh) Chrome/126.0.0.0 Safari/537.36"
    _install_f2_conf_stub(monkeypatch, headers=headers, browser=browser)
    with pytest.raises(fetch.ScopeError) as excinfo:
        fetch.official_list_headers()
    assert excinfo.value.kind == "runtime_config_missing"
    assert "F2" in str(excinfo.value)


def test_official_list_headers_refuses_when_runtime_missing(monkeypatch):
    _install_f2_conf_stub(monkeypatch, headers=_official_headers(), with_manager=False)
    with pytest.raises(fetch.ScopeError) as excinfo:
        fetch.official_list_headers()
    assert excinfo.value.kind == "runtime_missing"


def _list_scope_args(tmp_path, *, scopes="collection", sec_user_id=""):
    from types import SimpleNamespace
    cookie = tmp_path / "cookie.txt"
    cookie.write_text("sessionid=synthetic", encoding="utf-8")
    return SimpleNamespace(
        cookie_file=str(cookie), scopes=scopes, sec_user_id=sec_user_id,
        folder_ids="", page_counts=20, max_items_per_list=100,
        max_pages_per_list=50, budget_seconds=10, timeout=7, max_retries=3,
        lock_file="", output=str(tmp_path / "out.json"),
    )


def test_run_scopes_uses_official_headers_and_keeps_network_settings(monkeypatch, tmp_path):
    """列表取数实际传入 F2 的 kwargs：官方 UA+Referer，且保留超时/重试/代理/cookie。"""
    import importlib.metadata

    captured: list[dict] = []

    class Handler:
        def __init__(self, kwargs):
            captured.append(kwargs)

        def fetch_user_collection_videos(self, **kwargs):
            async def gen():
                yield _FakePage(_raw_page(["1"], has_more=0))
            return gen()

    _install_f2_conf_stub(monkeypatch, headers=_official_headers(),
                          browser={"name": "Edge", "version": "130.0.0.0"},
                          handler=Handler)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.0.1.7")

    result = asyncio.run(fetch.run_scopes(_list_scope_args(tmp_path)))
    assert len(captured) == 1
    kwargs = captured[0]
    assert kwargs["headers"] == _official_headers()
    assert kwargs["headers"]["User-Agent"] == _OFFICIAL_UA
    assert kwargs["headers"]["Referer"] == _OFFICIAL_REFERER
    assert kwargs["timeout"] == 7 and kwargs["max_retries"] == 3
    assert kwargs["proxies"] == {"http://": None, "https://": None}
    assert kwargs["cookie"] == "sessionid=synthetic"
    assert result["collection"]["complete"] is True
    assert [w["external_id"] for w in result["collection"]["works"]] == ["1"]
    assert result["runtime"]["f2_version"] == "0.0.1.7"


def test_run_scopes_refuses_before_creating_handler_when_config_missing(monkeypatch, tmp_path):
    """配置不可用时不创建 F2 handler，也不发任何请求。"""
    import importlib.metadata

    created: list[dict] = []

    class Handler:
        def __init__(self, kwargs):
            created.append(kwargs)

    _install_f2_conf_stub(monkeypatch, headers={}, handler=Handler)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.0.1.7")
    with pytest.raises(fetch.ScopeError) as excinfo:
        asyncio.run(fetch.run_scopes(_list_scope_args(tmp_path)))
    assert excinfo.value.kind == "runtime_config_missing"
    assert created == []


def test_detail_path_keeps_previous_request_headers(monkeypatch, tmp_path):
    """详情（单作品直取）已验收成功：本轮不改，仍只带既有 UA。"""
    from types import SimpleNamespace

    captured: list[dict] = []

    class Handler:
        def __init__(self, kwargs):
            captured.append(kwargs)

        async def fetch_one_video(self, aweme_id):
            return _FakePage({"status_code": 0, "aweme_detail": {
                "aweme_id": aweme_id, "author": {"nickname": "synthetic"},
                "desc": "sample", "create_time": 1700000000, "images": []}})

    _install_f2_conf_stub(monkeypatch, headers=_official_headers(), handler=Handler)
    cookie = tmp_path / "c.txt"
    cookie.write_text("sessionid=synthetic", encoding="utf-8")
    args = SimpleNamespace(cookie_file=str(cookie), aweme_id="1", timeout=5,
                           max_retries=2, budget_seconds=10)
    result = fetch.run_detail(args)
    assert captured[0]["headers"] == {"User-Agent": fetch._USER_AGENT}
    assert result["detail"]["external_id"] == "1"


def test_detail_refuses_redirect_and_restores_handler_crawler(monkeypatch, tmp_path):
    """详情 3xx → redirect_refused；期间使用禁止重定向子类，失败后恢复原类。"""
    from types import SimpleNamespace

    seen: dict = {}

    class Handler:
        def __init__(self, kwargs):
            self.kwargs = kwargs

        async def fetch_one_video(self, aweme_id):
            seen["crawler_cls"] = sys.modules["f2.apps.douyin.handler"].DouyinCrawler
            raise fetch.RedirectRefused("synthetic redirect SECRET_LOCATION")

    _install_f2_conf_stub(monkeypatch, headers=_official_headers(), handler=Handler)
    cookie = tmp_path / "c.txt"
    cookie.write_text("sessionid=synthetic; UIFID=SYNTHETIC-UIFID", encoding="utf-8")
    args = SimpleNamespace(cookie_file=str(cookie), aweme_id="1", timeout=5,
                           max_retries=2, budget_seconds=10)
    with pytest.raises(fetch.ScopeError) as excinfo:
        fetch.run_detail(args)
    assert excinfo.value.kind == "redirect_refused"
    assert "SECRET_LOCATION" not in str(excinfo.value)
    base = sys.modules["f2.apps.douyin.crawler"].DouyinCrawler
    handler_module = sys.modules["f2.apps.douyin.handler"]
    assert seen["crawler_cls"] is not base  # 详情请求期间替换为禁止重定向子类
    assert handler_module.DouyinCrawler is base  # 异常后恢复原类，不残留全局替换


# ---------- 收藏夹响应字段（固定版 F2 用 total_number） ----------

class _FoldersHandler:
    """只产出收藏夹列表页；folder_ids 不匹配时不访问夹内作品。"""

    kwargs: dict = {}

    def __init__(self, raws: list[dict]):
        self._raws = raws

    def fetch_user_collects(self, **kwargs):
        async def gen():
            for raw in self._raws:
                yield type("Page", (), {"_to_raw": lambda self, raw=raw: raw,
                                        "status_code": raw.get("status_code")})()
        return gen()


def _collects_args(folder_ids="NOT-IN-RESPONSE"):
    return type("Args", (), {
        "page_counts": 20, "max_items_per_list": 100,
        "max_pages_per_list": 50, "folder_ids": folder_ids,
    })()


def _run_collects(raws: list[dict]):
    result, warnings = {}, []
    asyncio.run(fetch.fetch_collections_scope(_FoldersHandler(raws), _collects_args(), result, warnings))
    return result["collects"], warnings


def test_collects_item_count_prefers_total_number_and_keeps_zero():
    """total_number 是固定版真实字段：0 合法；正数保留；旧字段按顺序回退。"""
    block, _warnings = _run_collects([{
        "status_code": 0, "has_more": 0,
        "collects_list": [
            {"collects_id": "F0", "collects_name": "空夹", "total_number": 0},
            {"collects_id": "F3", "collects_name": "三件", "total_number": 3},
            {"collects_id": "F5", "collects_name": "旧字段A", "collects_item_count": 5},
            {"collects_id": "F2", "collects_name": "旧字段B", "item_count": 2},
            {"collects_id": "F1", "collects_name": "未知"},
        ],
    }])
    counts = {row["folder_id"]: row["item_count"] for row in block["folders"]}
    assert counts == {"F0": 0, "F3": 3, "F5": 5, "F2": 2, "F1": None}
    assert block["complete"] is True and block["folders_complete"] is True


def test_collects_missing_or_null_list_still_fails_without_empty_success():
    """字段读取修复不改变完整性/失败语义：缺 collects_list、null 仍报 platform_error。"""
    for raw in ({"status_code": 0}, {"status_code": 0, "collects_list": None}):
        with pytest.raises(fetch.ScopeError) as excinfo:
            _run_collects([raw])
        assert excinfo.value.kind == "platform_error"
        assert "collects_list" in str(excinfo.value)


# ---------- 单夹失败隔离（保留其他范围/其他夹结果） ----------

_TWO_FOLDERS_RAW = [{
    "status_code": 0, "has_more": 0,
    "collects_list": [
        {"collects_id": "FAKEFOLDERID1", "collects_name": "正常", "total_number": 1},
        {"collects_id": "FAKEFOLDERID2", "collects_name": "异常", "total_number": 1},
    ],
}]


def _synthetic_work(external_id="1"):
    return fetch.extract_work({"aweme_id": external_id, "desc": "w"})[0]


def _run_two_folder_scope(monkeypatch, folder_impl):
    """用桩替换逐夹读取，走真实 fetch_collections_scope 的隔离逻辑。"""
    async def fake(handler, folder_id, *, label, args, warnings, **kwargs):
        return await folder_impl(folder_id, label)

    monkeypatch.setattr(fetch, "_fetch_folder_items_raw", fake)
    result, warnings = {}, []
    asyncio.run(fetch.fetch_collections_scope(
        _FoldersHandler(_TWO_FOLDERS_RAW), _collects_args(""), result, warnings))
    return result["collects"], warnings


def test_collects_single_folder_error_is_isolated_and_reported(monkeypatch):
    async def impl(folder_id, label):
        if folder_id == "FAKEFOLDERID2":
            raise fetch.ScopeError("platform_error", f"{label} 响应缺少 aweme_list 字段")
        return [_synthetic_work("1")], {"pages": 1, "items": 1, "complete": True,
                                        "server_oversize_pages": 0}

    block, warnings = _run_two_folder_scope(monkeypatch, impl)
    assert block["complete"] is False and block["folders_complete"] is True
    assert block["folder_incomplete"] == ["folder#2"]
    assert block["folder_failures"] == [{"folder": "folder#2", "kind": "platform_error"}]
    failed = block["folder_items"]["FAKEFOLDERID2"]
    assert failed["failed"] is True and "works" not in failed  # 不冒充合法空夹
    good = block["folder_items"]["FAKEFOLDERID1"]
    assert good["complete"] is True
    assert [w["external_id"] for w in good["works"]] == ["1"]  # 其他夹结果保留
    assert any("收藏夹#2 读取失败" in w for w in warnings)
    assert all("FAKEFOLDERID" not in w for w in warnings)


@pytest.mark.parametrize("kind", ["login_expired", "rate_limited", "redirect_refused"])
def test_collects_single_folder_safety_kind_stops_whole_sync(monkeypatch, kind):
    async def impl(folder_id, label):
        raise fetch.ScopeError(kind, "固定安全文案")

    with pytest.raises(fetch.ScopeError) as excinfo:
        _run_two_folder_scope(monkeypatch, impl)
    assert excinfo.value.kind == kind


def test_collects_single_folder_unexpected_error_is_isolated_and_redacted(monkeypatch):
    async def impl(folder_id, label):
        if folder_id == "FAKEFOLDERID2":
            raise RuntimeError("SECRET_TEST_COOKIE https://outside.invalid/?x=1")
        return [], {"pages": 1, "items": 0, "complete": True, "server_oversize_pages": 0}

    block, warnings = _run_two_folder_scope(monkeypatch, impl)
    assert block["folder_failures"] == [{"folder": "folder#2", "kind": "platform_error"}]
    assert all("SECRET_TEST_COOKIE" not in w and "https://" not in w for w in warnings)


_THREE_FOLDERS_RAW = [{
    "status_code": 0, "has_more": 0,
    "collects_list": [
        {"collects_id": "FAKEFOLDERID1", "collects_name": "一", "total_number": 1},
        {"collects_id": "FAKEFOLDERID2", "collects_name": "二", "total_number": 1},
        {"collects_id": "FAKEFOLDERID3", "collects_name": "三", "total_number": 1},
    ],
}]


def test_collects_consecutive_failures_stop_after_limit_and_mark_remaining(monkeypatch):
    calls: list[str] = []

    async def impl(folder_id, label):
        calls.append(folder_id)
        raise fetch.ScopeError("platform_error", f"{label} HTTP 403")

    async def fake(handler, folder_id, *, label, args, warnings, **kwargs):
        return await impl(folder_id, label)

    monkeypatch.setattr(fetch, "_fetch_folder_items_raw", fake)
    result, warnings = {}, []
    asyncio.run(fetch.fetch_collections_scope(
        _FoldersHandler(_THREE_FOLDERS_RAW), _collects_args(""), result, warnings))
    block = result["collects"]

    assert calls == ["FAKEFOLDERID1", "FAKEFOLDERID2"]  # 第三夹不再请求
    assert block["stopped_after_consecutive_failures"] is True
    assert block["folder_incomplete"] == ["folder#1", "folder#2", "folder#3"]
    assert [row["kind"] for row in block["folder_failures"]] == [
        "platform_error", "platform_error", "consecutive_failures",
    ]
    assert block["complete"] is False
    assert all("FAKEFOLDERID" not in w for w in warnings)


def test_collects_success_resets_consecutive_failure_counter(monkeypatch):
    calls: list[str] = []

    async def impl(folder_id, label):
        calls.append(folder_id)
        if folder_id != "FAKEFOLDERID2":
            raise fetch.ScopeError("platform_error", f"{label} HTTP 403")
        return [], {"pages": 1, "items": 0, "complete": True, "server_oversize_pages": 0}

    async def fake(handler, folder_id, *, label, args, warnings, **kwargs):
        return await impl(folder_id, label)

    monkeypatch.setattr(fetch, "_fetch_folder_items_raw", fake)
    result, warnings = {}, []
    asyncio.run(fetch.fetch_collections_scope(
        _FoldersHandler(_THREE_FOLDERS_RAW), _collects_args(""), result, warnings))
    block = result["collects"]

    assert calls == ["FAKEFOLDERID1", "FAKEFOLDERID2", "FAKEFOLDERID3"]
    assert block.get("stopped_after_consecutive_failures") is not True
    assert block["folder_incomplete"] == ["folder#1", "folder#3"]


# ---------- 整体预算到期：保留已完成结果 ----------

def _budget_args(tmp_path, *, scopes="collection,collections", budget=0.2):
    args = _list_scope_args(tmp_path, scopes=scopes)
    args.budget_seconds = budget
    args.timeout = 0
    args.max_retries = 1
    return args


def test_finalize_budget_partial_keeps_completed_and_marks_unfinished():
    work = _synthetic_work("1")
    result = {
        "ok": True, "warnings": [],
        "like": {"pages": 1, "items": 1, "complete": True,
                 "server_oversize_pages": 0, "works": [work]},
        "collects": {
            "pages": 1, "folders": [{"folder_id": "f1"}, {"folder_id": "f2"}],
            "folders_complete": True,
            "folder_items": {"f1": {"complete": True, "works": [work]}},
            "folder_incomplete": [], "folder_failures": [], "complete": False,
        },
    }
    args = type("Args", (), {"scopes": "like,collection,collections"})()
    finalized = fetch._finalize_budget_partial(args, result)

    assert finalized["like"]["complete"] is True  # 已完成范围不降级
    assert finalized["collection"]["complete"] is False
    assert finalized["collection"]["budget_exhausted"] is True
    assert "works" in finalized["collection"] and finalized["collection"]["works"] == []
    collects = finalized["collects"]
    assert collects["folder_items"]["f1"]["complete"] is True
    assert collects["folder_incomplete"] == ["folder#2"]
    assert collects["folder_failures"] == [{"folder": "folder#2", "kind": "budget_exhausted"}]
    assert collects["budget_exhausted"] is True
    assert finalized["partial"] is True and finalized["budget_exhausted"] is True


def test_budget_exhausted_before_next_scope_keeps_completed_scope(monkeypatch, tmp_path):
    _reset_auto_handler()
    _install_self_identity_stub(
        monkeypatch, handler_cls=_AutoIdentityHandler,
        response={"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}})
    monkeypatch.setattr(fetch, "_BUDGET_GRACE_SECONDS", 0.5)
    args = _budget_args(tmp_path, budget=1.0)
    result = fetch._run_list_mode(args)

    assert result["collection"]["complete"] is True
    assert result["collection"]["works"][0]["external_id"] == "9"
    assert result["collects"]["complete"] is False
    assert result["collects"]["budget_exhausted"] is True
    assert result["partial"] is True and result["budget_exhausted"] is True
    assert _AutoIdentityHandler.collection_calls == 1


def test_hard_timeout_uses_partial_result_instead_of_discarding(monkeypatch, tmp_path):
    """硬超时（卡住的请求）也要保留已完成范围，仅把未完成范围标记不完整。"""
    class SlowLikeHandler:
        kwargs: dict = {}

        def __init__(self, kwargs):
            self.kwargs = kwargs

        def fetch_user_collection_videos(self, **kwargs):
            async def gen():
                yield _FakePage(_raw_page(["9"], has_more=0))
            return gen()

        def fetch_user_like_videos(self, sec_user_id, **kwargs):
            async def gen():
                await asyncio.sleep(30)  # 模拟卡住，触发硬超时
                yield _FakePage(_raw_page(["1"], has_more=0))
            return gen()

    _install_self_identity_stub(monkeypatch, handler_cls=SlowLikeHandler,
                                response={"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}})
    monkeypatch.setattr(fetch, "_BUDGET_GRACE_SECONDS", 0.1)
    args = _budget_args(tmp_path, scopes="collection,like", budget=2)
    result = fetch._run_list_mode(args)

    assert result["collection"]["complete"] is True
    assert result["like"]["complete"] is False
    assert result["like"]["budget_exhausted"] is True
    assert result["partial"] is True
    assert _SYNTH_SELF_ID not in json.dumps(result, ensure_ascii=False)


def test_main_writes_partial_output_on_budget_exhaustion(monkeypatch, tmp_path):
    """主程序输出：预算到期仍落盘已完成范围，未完成范围标记 partial。"""
    _reset_auto_handler()
    _install_self_identity_stub(
        monkeypatch, handler_cls=_AutoIdentityHandler,
        response={"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}})
    monkeypatch.setattr(fetch, "_BUDGET_GRACE_SECONDS", 0.5)
    cookie = tmp_path / "cookie.txt"
    cookie.write_text("sessionid=synthetic", encoding="utf-8")
    output = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", [
        "f2_fetch.py", "--cookie-file", str(cookie), "--output", str(output),
        "--scopes", "collection,collections", "--budget-seconds", "1",
        "--timeout", "0", "--max-retries", "1",
    ])
    assert fetch.main() == 0
    doc = json.loads(output.read_text(encoding="utf-8"))
    assert doc["ok"] is True and doc["partial"] is True and doc["budget_exhausted"] is True
    assert doc["collection"]["complete"] is True
    assert doc["collects"]["complete"] is False and doc["collects"]["budget_exhausted"] is True
    assert "elapsed_seconds" in doc


# ---------- 跨运行续传：多次短预算逐步覆盖 ----------

def _progress_args(tmp_path, *, scopes, budget=1, sec_user_id="", folder_ids=""):
    args = _budget_args(tmp_path, scopes=scopes, budget=budget)
    args.progress_file = str(tmp_path / "sync_progress.json")
    args.sec_user_id = sec_user_id
    args.folder_ids = folder_ids
    return args


def test_progress_fingerprint_changes_with_identity_scopes_and_filters():
    base = dict(sec_user_id="MANUAL-A", scopes=["like"], folder_ids="f1,f2",
                page_counts=20, max_items=100, max_pages=50)
    assert fetch._progress_fingerprint(**base) == fetch._progress_fingerprint(**base)
    for change in ({"sec_user_id": "MANUAL-B"}, {"scopes": ["like", "collection"]},
                   {"folder_ids": "f1"}, {"page_counts": 10}, {"max_pages": 1},
                   {"cookie_hash": fetch._identity_hash("other-session")}):
        assert fetch._progress_fingerprint(**{**base, **change}) != fetch._progress_fingerprint(**base)


_THREE_SORTED_FOLDERS_RAW = [{
    "status_code": 0, "has_more": 0,
    "collects_list": [
        {"collects_id": "F1", "collects_name": "一", "total_number": 1},
        {"collects_id": "F2", "collects_name": "二", "total_number": 1},
        {"collects_id": "F3", "collects_name": "三", "total_number": 1},
    ],
}]


def _install_folder_resume_stub(monkeypatch, raw):
    class Handler:
        kwargs: dict = {}

        def __init__(self, kwargs):
            self.kwargs = kwargs

        def fetch_user_collects(self, **kwargs):
            async def gen():
                for raw in raw_pages:
                    yield _FakePage(raw)
            return gen()

    raw_pages = raw if isinstance(raw, list) else [raw]
    _install_self_identity_stub(monkeypatch, handler_cls=Handler,
                                response={"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}})


def test_short_budget_runs_progressively_cover_all_folders_and_clear_state(monkeypatch, tmp_path):
    calls: list[str] = []

    async def fake_folder(handler, folder_id, *, label, args, warnings, **kwargs):
        calls.append(folder_id)
        return [_synthetic_work(folder_id)], {
            "pages": 1, "items": 1, "complete": True, "server_oversize_pages": 0}

    _install_folder_resume_stub(monkeypatch, _THREE_SORTED_FOLDERS_RAW)
    monkeypatch.setattr(fetch, "_fetch_folder_items_raw", fake_folder)
    monkeypatch.setattr(fetch, "_BUDGET_GRACE_SECONDS", 0.5)
    args = _progress_args(tmp_path, scopes="collections", budget=1)

    first = fetch._run_list_mode(args)
    assert calls == ["F1"]                      # 第一次预算只够第一个夹
    assert first["collects"]["folder_items"]["F1"]["complete"] is True
    assert first["collects"]["folder_incomplete"] == ["folder#2", "folder#3"]
    assert first["partial"] is True
    # 提交协议：取数结束不写进度文件（提交前崩溃不推进游标），只回传状态
    assert not (tmp_path / "sync_progress.json").exists()
    assert first["resume"]["version"] == fetch._PROGRESS_VERSION
    assert first["resume"]["all_complete"] is False
    assert fetch.write_confirmed_progress(str(args.progress_file), first["resume"])
    progress_text = (tmp_path / "sync_progress.json").read_text(encoding="utf-8")
    assert "F1" not in progress_text and "F2" not in progress_text  # 只存哈希
    assert "sessionid" not in progress_text

    calls.clear()
    second = fetch._run_list_mode(args)
    assert calls == ["F2"]                      # 第二次跳过已确认完成的 F1，续读 F2
    assert second["collects"]["folder_items"]["F1"]["resumed"] is True
    assert second["collects"]["folder_items"]["F2"]["complete"] is True
    assert second["collects"]["folder_incomplete"] == ["folder#3"]
    assert fetch.write_confirmed_progress(str(args.progress_file), second["resume"])

    calls.clear()
    third = fetch._run_list_mode(args)
    assert calls == ["F3"]
    assert third["collects"]["complete"] is True
    assert third.get("partial") is not True
    assert third["resume"]["all_complete"] is True
    # 全部完整由父进程在数据提交后清除，下一轮从头开始
    assert fetch.write_confirmed_progress(str(args.progress_file), third["resume"])
    assert not (tmp_path / "sync_progress.json").exists()


class _LikePageResumeHandler:
    kwargs: dict = {}
    start_cursors: list[int] = []

    def __init__(self, kwargs):
        self.kwargs = kwargs

    def fetch_user_like_videos(self, sec_user_id, max_cursor=0, **kwargs):
        type(self).start_cursors.append(int(max_cursor))

        async def gen():
            if max_cursor == 0:
                yield _FakePage({"status_code": 0, "aweme_list": [_work_entry("1")],
                                 "has_more": 1, "max_cursor": 10})
                await asyncio.sleep(30)  # 下一页等待中预算到期
                yield _FakePage({"status_code": 0, "aweme_list": [_work_entry("2")],
                                 "has_more": 0, "max_cursor": 20})
            else:
                yield _FakePage({"status_code": 0, "aweme_list": [_work_entry("2")],
                                 "has_more": 0, "max_cursor": 20})
        return gen()


def _work_entry(aweme_id):
    return {"aweme_id": aweme_id, "desc": f"w{aweme_id}", "create_time": 1746000000,
            "author": {"nickname": "a", "sec_uid": _SYNTH_SELF_ID},
            "video": {"cover": {"url_list": ["https://c.example/1.jpg"]}}}


def test_like_pagination_resumes_from_saved_cursor(monkeypatch, tmp_path):
    _LikePageResumeHandler.start_cursors = []
    _install_self_identity_stub(
        monkeypatch, handler_cls=_LikePageResumeHandler,
        response={"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}})
    monkeypatch.setattr(fetch, "_BUDGET_GRACE_SECONDS", 0.5)
    args = _progress_args(tmp_path, scopes="like", budget=1, sec_user_id=_SYNTH_MANUAL_ID)

    first = fetch._run_list_mode(args)
    assert _LikePageResumeHandler.start_cursors == [0]
    assert first["like"]["complete"] is False
    assert first["like"]["works"][0]["external_id"] == "1"
    assert first["like"]["budget_exhausted"] is True
    # 提交协议（P1 修复回归）：取数完成不落进度文件——提交前崩溃时
    # 下一轮从已确认游标（此处为 0）重取，绝不跳过未入库数据
    assert not (tmp_path / "sync_progress.json").exists()
    assert fetch.write_confirmed_progress(str(args.progress_file), first["resume"])
    saved = json.loads((tmp_path / "sync_progress.json").read_text(encoding="utf-8"))
    assert saved["scopes"]["like"]["cursor"] == 10
    assert _SYNTH_MANUAL_ID not in json.dumps(saved)  # 不保存明文身份

    _LikePageResumeHandler.start_cursors = []
    second = fetch._run_list_mode(args)
    assert _LikePageResumeHandler.start_cursors == [10]  # 从已确认游标续传
    assert second["like"]["complete"] is True
    assert [w["external_id"] for w in second["like"]["works"]] == ["2"]
    assert second["resume"]["all_complete"] is True
    assert fetch.write_confirmed_progress(str(args.progress_file), second["resume"])
    assert not (tmp_path / "sync_progress.json").exists()


def test_like_resume_invalidated_when_identity_changes(monkeypatch, tmp_path):
    _LikePageResumeHandler.start_cursors = []
    _install_self_identity_stub(
        monkeypatch, handler_cls=_LikePageResumeHandler,
        response={"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}})
    monkeypatch.setattr(fetch, "_BUDGET_GRACE_SECONDS", 0.5)
    args = _progress_args(tmp_path, scopes="like", budget=1, sec_user_id="MANUAL-A")
    first = fetch._run_list_mode(args)
    assert fetch.write_confirmed_progress(str(args.progress_file), first["resume"])
    _LikePageResumeHandler.start_cursors = []
    args.sec_user_id = "MANUAL-B"  # 账号变化 → 旧游标不得沿用
    result = fetch._run_list_mode(args)
    assert _LikePageResumeHandler.start_cursors == [0]
    assert result["like"]["complete"] is False


def test_like_resume_invalidated_when_scope_changes(monkeypatch, tmp_path):
    _LikePageResumeHandler.start_cursors = []
    _install_self_identity_stub(
        monkeypatch, handler_cls=_LikePageResumeHandler,
        response={"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}})
    monkeypatch.setattr(fetch, "_BUDGET_GRACE_SECONDS", 0.5)
    args = _progress_args(tmp_path, scopes="like", budget=1, sec_user_id=_SYNTH_MANUAL_ID)
    first = fetch._run_list_mode(args)
    assert fetch.write_confirmed_progress(str(args.progress_file), first["resume"])
    _LikePageResumeHandler.start_cursors = []
    args.scopes = "like,collection"  # 同步范围变化 → 旧游标不得沿用
    result = fetch._run_list_mode(args)
    assert _LikePageResumeHandler.start_cursors == [0]
    assert result["like"]["complete"] is False


def test_mixed_folder_order_hard_timeout_marks_every_unfinished_folder_once(monkeypatch, tmp_path):
    """接口返回顺序 Z/A/M、结果排序 A/M/Z：失败、读取中、未开始的夹都要准确标记。"""
    raw = [{
        "status_code": 0, "has_more": 0,
        "collects_list": [
            {"collects_id": "Z", "collects_name": "Z", "total_number": 1},
            {"collects_id": "A", "collects_name": "A", "total_number": 1},
            {"collects_id": "M", "collects_name": "M", "total_number": 1},
        ],
    }]

    async def fake_folder(handler, folder_id, *, label, args, warnings, **kwargs):
        if folder_id == "A":
            raise fetch.ScopeError("platform_error", f"{label} 响应缺少 aweme_list 字段")
        if folder_id == "M":
            await asyncio.sleep(30)  # 读取中卡住，直到硬超时
        return [_synthetic_work(folder_id)], {
            "pages": 1, "items": 1, "complete": True, "server_oversize_pages": 0}

    _install_folder_resume_stub(monkeypatch, raw)
    monkeypatch.setattr(fetch, "_fetch_folder_items_raw", fake_folder)
    monkeypatch.setattr(fetch, "_BUDGET_GRACE_SECONDS", 0.1)
    args = _budget_args(tmp_path, scopes="collections", budget=3)
    result = fetch._run_list_mode(args)

    block = result["collects"]
    # 稳定排序 A/M/Z 后编号：A 已失败，M 读取中，Z 未开始，全部只出现一次
    assert block["folder_incomplete"] == ["folder#1", "folder#2", "folder#3"]
    assert [row["kind"] for row in block["folder_failures"]] == [
        "platform_error", "budget_exhausted", "budget_exhausted",
    ]
    assert block["folder_items"]["A"]["failed"] is True
    for folder_id in ("M", "Z"):
        assert block["folder_items"][folder_id]["budget_exhausted"] is True
        assert "works" not in block["folder_items"][folder_id]
    assert block["complete"] is False and result["partial"] is True
    assert all(fid not in json.dumps(result["warnings"]) for fid in ("A", "M", "Z"))

    from monitor.platforms.douyin_f2_source import parse_f2_result
    snapshot = parse_f2_result(result, scopes=("collections",))
    assert sorted(snapshot.folder_incomplete) == ["folder#1", "folder#2", "folder#3"]
    assert snapshot.direct_sync["collections"] is False


# ---------- 点赞自动识别本人标识（只读本人资料接口） ----------

_SYNTH_SELF_ID = "MS4wLjABAAAsynthetic0000000000"
_SYNTH_MANUAL_ID = "MS4wLjABAAAsyntheticmanual000000"


def _install_self_identity_stub(monkeypatch, *, handler_cls, response=None,
                                request_error=None, crawler_headers=None):
    """安装 f2 本人资料最小桩：请求模型默认参数、ABogus 签名、crawler GET、异常类。

    返回 (state, signed)：state["requests"] 记录发起的资料请求次数，signed 记录签名入参。
    """
    import types
    import importlib.metadata

    _install_f2_conf_stub(monkeypatch, headers=_official_headers(),
                          browser={"name": "Edge", "version": "130.0.0.0"},
                          handler=handler_cls)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.0.1.7")
    utils_mod = sys.modules["f2.apps.douyin.utils"]
    signed: list[tuple] = []

    class ABogusManager:
        @classmethod
        def model_2_endpoint(cls, user_agent, base_endpoint, params, body=""):
            signed.append((user_agent, base_endpoint, dict(params)))
            return base_endpoint + "?signed=synthetic"

    utils_mod.ABogusManager = ABogusManager

    model_mod = types.ModuleType("f2.apps.douyin.model")

    class BaseRequestModel:
        def model_dump(self):
            return {"device_platform": "webapp", "browser_name": "Edge",
                    "browser_platform": "Win32", "msToken": "synthetic-ms-token"}

    model_mod.BaseRequestModel = BaseRequestModel
    monkeypatch.setitem(sys.modules, "f2.apps.douyin.model", model_mod)
    sys.modules["f2.apps.douyin"].model = model_mod

    state = {"requests": 0}
    header_values = dict({"User-Agent": _OFFICIAL_UA} if crawler_headers is None else crawler_headers)

    class DouyinCrawler:
        headers = header_values

        def __init__(self, kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            state["requests"] += 1
            return self

        async def __aexit__(self, *exc):
            return False

        async def _fetch_get_json(self, endpoint):
            if request_error is not None:
                raise request_error
            return response

    crawler_mod = types.ModuleType("f2.apps.douyin.crawler")
    crawler_mod.DouyinCrawler = DouyinCrawler
    monkeypatch.setitem(sys.modules, "f2.apps.douyin.crawler", crawler_mod)
    sys.modules["f2.apps.douyin"].crawler = crawler_mod

    exceptions_mod = types.ModuleType("f2.exceptions.api_exceptions")

    class APIUnauthorizedError(Exception): ...
    class APIRateLimitError(Exception): ...
    class APIRetryExhaustedError(Exception): ...

    exceptions_mod.APIUnauthorizedError = APIUnauthorizedError
    exceptions_mod.APIRateLimitError = APIRateLimitError
    exceptions_mod.APIRetryExhaustedError = APIRetryExhaustedError
    exceptions_pkg = types.ModuleType("f2.exceptions")
    exceptions_pkg.__path__ = []
    exceptions_pkg.api_exceptions = exceptions_mod
    monkeypatch.setitem(sys.modules, "f2.exceptions", exceptions_pkg)
    monkeypatch.setitem(sys.modules, "f2.exceptions.api_exceptions", exceptions_mod)
    return state, signed, exceptions_mod


class _AutoIdentityHandler:
    """记录点赞请求实际使用的本人标识；收藏仅用于“未请求点赞”对照。"""

    kwargs: dict = {}

    like_ids: list[str] = []
    collection_calls: int = 0

    def __init__(self, kwargs):
        self.kwargs = kwargs

    def fetch_user_like_videos(self, sec_user_id, **kwargs):
        _AutoIdentityHandler.like_ids.append(sec_user_id)

        async def gen():
            yield _FakePage(_raw_page(["1"], has_more=0))
        return gen()

    def fetch_user_collection_videos(self, **kwargs):
        _AutoIdentityHandler.collection_calls += 1

        async def gen():
            yield _FakePage(_raw_page(["9"], has_more=0))
        return gen()


def _reset_auto_handler():
    _AutoIdentityHandler.like_ids = []
    _AutoIdentityHandler.collection_calls = 0


def test_extract_self_sec_uid_accepts_only_profile_self_identity():
    assert fetch.extract_self_sec_uid({"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}}) == _SYNTH_SELF_ID
    # 只认 user.sec_uid：作品作者字段、其他字段、非字符串都不采信
    assert fetch.extract_self_sec_uid({"status_code": 0, "author": {"sec_uid": _SYNTH_SELF_ID}}) is None
    assert fetch.extract_self_sec_uid({"status_code": 0, "user": {"uid": "123"}}) is None
    assert fetch.extract_self_sec_uid({"status_code": 0, "user": {"sec_uid": 123}}) is None
    assert fetch.extract_self_sec_uid({"status_code": 0, "user": {"sec_uid": "MS4wLjABAAAA with space"}}) is None
    assert fetch.extract_self_sec_uid({"status_code": 0, "user": {"sec_uid": "short"}}) is None
    assert fetch.extract_self_sec_uid({"status_code": 8, "user": {"sec_uid": _SYNTH_SELF_ID}}) is None
    assert fetch.extract_self_sec_uid({"user": {"sec_uid": _SYNTH_SELF_ID}}) is None  # 缺成功状态
    assert fetch.extract_self_sec_uid({"status_code": None, "user": {"sec_uid": _SYNTH_SELF_ID}}) is None
    assert fetch.extract_self_sec_uid("not-a-dict") is None


def test_like_without_manual_id_auto_identifies_and_uses_it(monkeypatch, tmp_path):
    _reset_auto_handler()
    state, signed, _exc = _install_self_identity_stub(
        monkeypatch, handler_cls=_AutoIdentityHandler,
        response={"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID, "nickname": "本人"}})
    args = _list_scope_args(tmp_path, scopes="like")
    result = asyncio.run(fetch.run_scopes(args))

    assert state["requests"] == 1
    assert _AutoIdentityHandler.like_ids == [_SYNTH_SELF_ID]  # 自动识别结果传入点赞列表
    assert result["like"]["complete"] is True
    assert [w["external_id"] for w in result["like"]["works"]] == ["1"]
    # 签名使用同一官方 UA、固定本人资料路径，参数含模型浏览器参数
    assert signed[0][0] == _OFFICIAL_UA
    assert signed[0][1] == fetch._PROFILE_SELF_ENDPOINT
    assert signed[0][2]["browser_name"] == "Edge"
    # 标识只在进程内使用：结果与警告都不出现
    assert _SYNTH_SELF_ID not in json.dumps(result, ensure_ascii=False)


def test_manual_identity_skips_self_profile_request(monkeypatch, tmp_path):
    _reset_auto_handler()
    state, signed, _exc = _install_self_identity_stub(
        monkeypatch, handler_cls=_AutoIdentityHandler, response={"status_code": 0,
        "user": {"sec_uid": _SYNTH_SELF_ID}})
    result = asyncio.run(fetch.run_scopes(
        _list_scope_args(tmp_path, scopes="like", sec_user_id=_SYNTH_MANUAL_ID)))

    assert state["requests"] == 0 and signed == []  # 手动配置优先，不额外查询
    assert _AutoIdentityHandler.like_ids == [_SYNTH_MANUAL_ID]
    assert result["like"]["complete"] is True


def test_scopes_without_like_never_request_self_profile(monkeypatch, tmp_path):
    _reset_auto_handler()
    state, signed, _exc = _install_self_identity_stub(
        monkeypatch, handler_cls=_AutoIdentityHandler, response={"status_code": 0,
        "user": {"sec_uid": _SYNTH_SELF_ID}})
    result = asyncio.run(fetch.run_scopes(_list_scope_args(tmp_path, scopes="collection")))

    assert state["requests"] == 0 and signed == []
    assert _AutoIdentityHandler.like_ids == []
    assert _AutoIdentityHandler.collection_calls == 1
    assert result["collection"]["complete"] is True


@pytest.mark.parametrize("response,expected_kind", [
    ({"status_code": 0}, "self_identity_unavailable"),                    # 无 user
    ({"status_code": 0, "user": None}, "self_identity_unavailable"),      # user 非对象
    ({"status_code": 0, "user": {"sec_uid": ""}}, "self_identity_unavailable"),
    ({"status_code": 0, "user": {"sec_uid": "   "}}, "self_identity_unavailable"),
    ({"status_code": 0, "user": {"sec_uid": "MS4wLjABAAAA bad id"}}, "self_identity_unavailable"),
    ({"status_code": 0, "author": {"sec_uid": _SYNTH_SELF_ID}}, "self_identity_unavailable"),
    # 缺失/null/类型异常的成功状态都不能当作已确认（合成复现回归）
    ({"user": {"sec_uid": _SYNTH_SELF_ID}}, "self_identity_unavailable"),
    ({"status_code": None, "user": {"sec_uid": _SYNTH_SELF_ID}}, "self_identity_unavailable"),
    ({"status_code": "0", "user": {"sec_uid": _SYNTH_SELF_ID}}, "self_identity_unavailable"),
    ({"status_code": 8}, "login_expired"),
    ({"status_code": 2483}, "platform_error"),
    ({"status_code": True}, "self_identity_unavailable"),
    ("not-a-dict", "self_identity_unavailable"),
])
def test_auto_identity_failure_is_explicit_not_empty_like(monkeypatch, tmp_path, response, expected_kind):
    """资料无有效本人标识 / 响应无效 / 登录失效 → 明确失败，且不请求点赞。"""
    _reset_auto_handler()
    _install_self_identity_stub(monkeypatch, handler_cls=_AutoIdentityHandler, response=response)
    with pytest.raises(fetch.ScopeError) as excinfo:
        asyncio.run(fetch.run_scopes(_list_scope_args(tmp_path, scopes="like")))
    assert excinfo.value.kind == expected_kind
    assert _AutoIdentityHandler.like_ids == []
    # 固定文案，不含标识/URL/原始响应
    assert _SYNTH_SELF_ID not in str(excinfo.value)
    assert "https://" not in str(excinfo.value)


@pytest.mark.parametrize("error_name,expected_kind", [
    ("APIUnauthorizedError", "login_expired"),
    ("APIRateLimitError", "rate_limited"),
    ("APIRetryExhaustedError", "self_identity_unavailable"),
    ("RuntimeError", "self_identity_unavailable"),
])
def test_auto_identity_request_error_is_fixed_and_redacted(monkeypatch, tmp_path, error_name, expected_kind):
    _reset_auto_handler()
    state, signed, exc_mod = _install_self_identity_stub(
        monkeypatch, handler_cls=_AutoIdentityHandler)
    payload = (getattr(exc_mod, error_name, None) or RuntimeError)(
        f"leak {_SYNTH_SELF_ID} https://www.douyin.com/aweme/v1/web/user/profile/self/?token=SECRET")

    async def _raise(self, endpoint):
        raise payload

    sys.modules["f2.apps.douyin.crawler"].DouyinCrawler._fetch_get_json = _raise
    with pytest.raises(fetch.ScopeError) as excinfo:
        asyncio.run(fetch.run_scopes(_list_scope_args(tmp_path, scopes="like")))
    assert excinfo.value.kind == expected_kind
    assert _AutoIdentityHandler.like_ids == []
    message = str(excinfo.value)
    assert _SYNTH_SELF_ID not in message
    assert "SECRET" not in message
    assert "https://" not in message
    assert "leak" not in message
    assert excinfo.value.__cause__ is None  # 不挂原始异常，避免 traceback 外泄


def test_auto_identity_missing_user_agent_fails_without_request(monkeypatch, tmp_path):
    _reset_auto_handler()
    state, signed, _exc = _install_self_identity_stub(
        monkeypatch, handler_cls=_AutoIdentityHandler,
        response={"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}},
        crawler_headers={})
    with pytest.raises(fetch.ScopeError) as excinfo:
        asyncio.run(fetch.run_scopes(_list_scope_args(tmp_path, scopes="like")))
    assert excinfo.value.kind == "runtime_config_missing"
    assert state["requests"] == 1 and signed == []  # 进入 crawler 上下文但未签名/未请求
    assert _AutoIdentityHandler.like_ids == []


# ---------- 提交协议（取数半程）：只回传、不落盘；失败回滚夹游标 ----------

def test_fetch_does_not_persist_progress_and_echoes_resume_state(monkeypatch, tmp_path):
    """取数进程对进度文件只读：写入/清除都由父进程在数据提交后完成（P1-1 修复）。"""
    _LikePageResumeHandler.start_cursors = []
    _install_self_identity_stub(
        monkeypatch, handler_cls=_LikePageResumeHandler,
        response={"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}})
    monkeypatch.setattr(fetch, "_BUDGET_GRACE_SECONDS", 0.5)
    args = _progress_args(tmp_path, scopes="like", budget=1, sec_user_id=_SYNTH_MANUAL_ID)

    result = fetch._run_list_mode(args)
    path = tmp_path / "sync_progress.json"
    assert not path.exists()  # 取数完成 ≠ 游标推进
    resume = result["resume"]
    assert resume["version"] == fetch._PROGRESS_VERSION
    assert isinstance(resume["fingerprint"], str) and resume["fingerprint"]
    assert resume["scopes"]["like"] == {"complete": False, "cursor": 10}
    assert resume["all_complete"] is False
    # 回传状态不含明文身份/凭据
    dumped = json.dumps(resume)
    assert _SYNTH_MANUAL_ID not in dumped and "sessionid" not in dumped


def test_fetch_ignores_v1_progress_from_old_commit_protocol(monkeypatch, tmp_path):
    """旧 v1“取数即写”状态的游标不可信（可能指向未提交数据）：作废从零重取。"""
    args = _progress_args(tmp_path, scopes="like", budget=1, sec_user_id=_SYNTH_MANUAL_ID)
    fp = fetch._progress_fingerprint(
        sec_user_id=_SYNTH_MANUAL_ID, scopes=["like"], folder_ids="",
        page_counts=20, max_items=100, max_pages=50,
        cookie_hash=fetch._identity_hash("sessionid=synthetic"))
    (tmp_path / "sync_progress.json").write_text(json.dumps({
        "version": 1, "fingerprint": fp,
        "scopes": {"like": {"complete": False, "cursor": 999}},
    }, ensure_ascii=False), encoding="utf-8")
    _LikePageResumeHandler.start_cursors = []
    _install_self_identity_stub(
        monkeypatch, handler_cls=_LikePageResumeHandler,
        response={"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}})
    monkeypatch.setattr(fetch, "_BUDGET_GRACE_SECONDS", 0.5)

    fetch._run_list_mode(args)
    assert _LikePageResumeHandler.start_cursors == [0]  # 不沿用 v1 游标


def test_write_confirmed_progress_write_clear_and_reject(tmp_path):
    """确认步：形状完整才落盘；全部完整清除；任何拒绝都不改动已有状态。"""
    path = str(tmp_path / "sync_progress.json")
    partial = {"version": 2, "fingerprint": "fp", "all_complete": False,
               "scopes": {"like": {"complete": False, "cursor": 5}}}
    assert fetch.write_confirmed_progress(path, partial) is True
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    assert saved == {"version": 2, "fingerprint": "fp", "scopes": partial["scopes"]}
    assert (Path(path).stat().st_mode & 0o777) == 0o600  # 受权限保护

    original = Path(path).read_text(encoding="utf-8")
    for bad in (None, {}, [], {"version": "2", "fingerprint": "fp", "scopes": {}},
                {"version": True, "fingerprint": "fp", "scopes": {}},
                {"version": 2, "scopes": {}},
                {"version": 2, "fingerprint": "", "scopes": {}},
                {"version": 2, "fingerprint": "fp"},
                {"version": 2, "fingerprint": "fp", "all_complete": False}):
        assert fetch.write_confirmed_progress(path, bad) is False
        assert Path(path).read_text(encoding="utf-8") == original

    done = {"version": 2, "fingerprint": "fp", "all_complete": True,
            "scopes": {"like": {"complete": True, "cursor": 0}}}
    assert fetch.write_confirmed_progress(path, done) is True
    assert not Path(path).exists()  # 全部完整 → 清除，下一轮从头完整同步


def test_single_folder_failure_rolls_back_uncommitted_folder_cursor(monkeypatch, tmp_path):
    """单夹读取失败会丢弃本次已读页：夹游标必须回到进入前的已确认状态。

    否则下一轮会从未入库的游标续传，漏掉失败前已读但未返回的作品。
    """
    fp = fetch._progress_fingerprint(
        sec_user_id="", scopes=["collections"], folder_ids="",
        page_counts=20, max_items=100, max_pages=50,
        cookie_hash=fetch._identity_hash("sessionid=synthetic"))
    progress_path = tmp_path / "sync_progress.json"
    folder_key = fetch._identity_hash("F1")
    confirmed = {"complete": False, "cursor": 55}
    progress_path.write_text(json.dumps({
        "version": fetch._PROGRESS_VERSION, "fingerprint": fp,
        "scopes": {"collections": {"complete": False, "folders_complete": True,
                                   "folders": {folder_key: dict(confirmed)}}},
    }, ensure_ascii=False), encoding="utf-8")

    seen_start_cursors: list[int] = []

    async def fake_folder(handler, folder_id, *, label, args, warnings, start_cursor=0, **kwargs):
        seen_start_cursors.append(int(start_cursor))
        raise fetch.ScopeError("platform_error", f"{label} 响应缺少 aweme_list 字段")

    _install_folder_resume_stub(monkeypatch, _THREE_SORTED_FOLDERS_RAW)
    monkeypatch.setattr(fetch, "_fetch_folder_items_raw", fake_folder)
    args = _progress_args(tmp_path, scopes="collections", budget=10)

    result = fetch._run_list_mode(args)
    # F1 从已确认游标 55 进入；F2 无续传条目从 0 开始，连续两夹失败后停止
    assert seen_start_cursors == [55, 0]
    assert result["collects"]["folder_failures"][0]["kind"] == "platform_error"
    # 回传状态：失败夹的游标未推进（仍为已确认值），完成标记未推进
    assert result["resume"]["scopes"]["collections"]["folders"][folder_key] == confirmed
    assert fetch.write_confirmed_progress(str(args.progress_file), result["resume"])
    saved = json.loads(progress_path.read_text(encoding="utf-8"))
    assert saved["scopes"]["collections"]["folders"][folder_key] == confirmed


def test_hard_timeout_partial_keeps_resume_state_for_committed_scopes(monkeypatch, tmp_path):
    """硬超时（卡在未包 wait_for 的夹列表首页）收尾的 partial 也带回传状态，
    游标与已返回作品一致：提交后可安全续传。"""
    class HangCollectsListHandler:
        kwargs: dict = {}

        def __init__(self, kwargs):
            self.kwargs = kwargs

        def fetch_user_like_videos(self, sec_user_id, max_cursor=0, **kwargs):
            async def gen():
                yield _FakePage({"status_code": 0, "aweme_list": [_work_entry("1")],
                                 "has_more": 0, "max_cursor": 20})
            return gen()

        def fetch_user_collects(self, **kwargs):
            async def gen():
                await asyncio.sleep(60)  # 夹列表首页卡住：内部截止无法打断
                yield _FakePage({"status_code": 0, "collects_list": [], "has_more": 0})
            return gen()

    _install_self_identity_stub(monkeypatch, handler_cls=HangCollectsListHandler,
                                response={"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}})
    monkeypatch.setattr(fetch, "_BUDGET_GRACE_SECONDS", 0.2)
    args = _progress_args(tmp_path, scopes="like,collections", budget=2,
                          sec_user_id=_SYNTH_MANUAL_ID)

    result = fetch._run_list_mode(args)
    assert result["partial"] is True
    assert result["like"]["complete"] is True
    assert result["collects"]["complete"] is False
    assert result["resume"]["scopes"]["like"] == {"complete": True, "cursor": 0}
    assert result["resume"]["all_complete"] is False
    assert _SYNTH_MANUAL_ID not in json.dumps(result["resume"])


# ---------- 中断一致性：硬超时/取消不得让游标越过未返回的作品 ----------

class _SwallowAfterFirstPageMixin:
    """第一页正常产出后挂起，并吞掉一次取消再挂起——复刻真实 F2 生成器
    “下一页请求取消不及时”（内层预算超时被吞，外层硬超时击穿）的机制。"""

    @staticmethod
    def _swallow_tail():
        async def _tail():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await asyncio.sleep(60)
        return _tail()


def test_hard_timeout_cancelled_like_pages_stay_in_result_with_cursor(monkeypatch, tmp_path):
    """P1 回归（第 1 轮复审）：硬超时取消落在已读页之后时，结果必须保留已读页
    作品且回传游标与之一致；确认后下一轮从游标续传补齐，不漏项、不误报完成。"""
    state = {"runs": []}

    class Handler(_SwallowAfterFirstPageMixin):
        kwargs: dict = {}

        def __init__(self, kwargs):
            self.kwargs = kwargs

        def fetch_user_collection_videos(self, **kwargs):
            async def gen():
                yield _FakePage({"status_code": 0, "aweme_list": [_work_entry("9")],
                                 "has_more": 0, "cursor": 0})
            return gen()

        def fetch_user_like_videos(self, sec_user_id, max_cursor=0, **kwargs):
            state["runs"].append(int(max_cursor))

            async def gen():
                if int(max_cursor) == 10:
                    yield _FakePage({"status_code": 0, "aweme_list": [_work_entry("2")],
                                     "has_more": 0, "max_cursor": 20})
                    return
                yield _FakePage({"status_code": 0, "aweme_list": [_work_entry("1")],
                                 "has_more": 1, "max_cursor": 10})
                await self._swallow_tail()
            return gen()

    _install_self_identity_stub(monkeypatch, handler_cls=Handler,
                                response={"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}})
    monkeypatch.setattr(fetch, "_BUDGET_GRACE_SECONDS", 0.1)
    args = _progress_args(tmp_path, scopes="collection,like", budget=2,
                          sec_user_id=_SYNTH_MANUAL_ID)
    progress = tmp_path / "sync_progress.json"

    first = fetch._run_list_mode(args)
    assert first["partial"] is True
    assert [w["external_id"] for w in first["like"]["works"]] == ["1"]  # 已读页保留
    assert first["like"]["complete"] is False and first["like"]["budget_exhausted"] is True
    assert first["resume"]["scopes"]["like"]["cursor"] == 10  # 与已返回作品一致
    assert first["collection"]["complete"] is True
    assert fetch.write_confirmed_progress(str(args.progress_file), first["resume"])
    saved = json.loads(progress.read_text(encoding="utf-8"))
    assert saved["scopes"]["like"] == {"complete": False, "cursor": 10}
    assert saved["scopes"]["collection"]["complete"] is True

    second = fetch._run_list_mode(args)  # 确认后从游标 10 续传第二页
    assert state["runs"] == [0, 10]
    assert [w["external_id"] for w in second["like"]["works"]] == ["2"]
    assert second["like"]["complete"] is True and second.get("partial") is not True
    assert fetch.write_confirmed_progress(str(args.progress_file), second["resume"])
    assert not progress.exists()


def test_hard_timeout_cancelled_collection_pages_stay_in_result(monkeypatch, tmp_path):
    """平铺收藏范围同样受中断一致性保护。"""

    class Handler(_SwallowAfterFirstPageMixin):
        kwargs: dict = {}

        def __init__(self, kwargs):
            self.kwargs = kwargs

        def fetch_user_collection_videos(self, sec_user_id=None, max_cursor=0, **kwargs):
            async def gen():
                if int(max_cursor) == 7:
                    yield _FakePage({"status_code": 0, "aweme_list": [_work_entry("8")],
                                     "has_more": 0, "cursor": 9})
                    return
                yield _FakePage({"status_code": 0, "aweme_list": [_work_entry("7")],
                                 "has_more": 1, "cursor": 7})
                await self._swallow_tail()
            return gen()

    _install_self_identity_stub(monkeypatch, handler_cls=Handler,
                                response={"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}})
    monkeypatch.setattr(fetch, "_BUDGET_GRACE_SECONDS", 0.1)
    args = _progress_args(tmp_path, scopes="collection", budget=2,
                          sec_user_id=_SYNTH_MANUAL_ID)
    progress = tmp_path / "sync_progress.json"

    first = fetch._run_list_mode(args)
    assert first["partial"] is True
    assert [w["external_id"] for w in first["collection"]["works"]] == ["7"]
    assert first["resume"]["scopes"]["collection"]["cursor"] == 7
    assert fetch.write_confirmed_progress(str(args.progress_file), first["resume"])

    second = fetch._run_list_mode(args)
    assert [w["external_id"] for w in second["collection"]["works"]] == ["8"]
    assert second["collection"]["complete"] is True
    assert fetch.write_confirmed_progress(str(args.progress_file), second["resume"])
    assert not progress.exists()


def test_hard_timeout_before_first_page_does_not_advance_cursor(monkeypatch, tmp_path):
    """取消落在首页前：无已读作品，游标不得推进（回传状态保持已确认值）。"""

    class Handler(_SwallowAfterFirstPageMixin):
        kwargs: dict = {}

        def __init__(self, kwargs):
            self.kwargs = kwargs

        def fetch_user_collection_videos(self, **kwargs):
            async def gen():
                yield _FakePage({"status_code": 0, "aweme_list": [_work_entry("9")],
                                 "has_more": 0, "cursor": 0})
            return gen()

        def fetch_user_like_videos(self, sec_user_id, max_cursor=0, **kwargs):
            async def gen():
                await self._swallow_tail()
            return gen()

    _install_self_identity_stub(monkeypatch, handler_cls=Handler,
                                response={"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}})
    monkeypatch.setattr(fetch, "_BUDGET_GRACE_SECONDS", 0.1)
    args = _progress_args(tmp_path, scopes="collection,like", budget=1,
                          sec_user_id=_SYNTH_MANUAL_ID)

    result = fetch._run_list_mode(args)
    assert result["partial"] is True
    assert "like" not in result["resume"]["scopes"]  # 未读任何页：游标未推进
    assert result["collection"]["complete"] is True
    assert fetch.write_confirmed_progress(str(args.progress_file), result["resume"])
    saved = json.loads((tmp_path / "sync_progress.json").read_text(encoding="utf-8"))
    assert "like" not in saved["scopes"] and saved["scopes"]["collection"]["complete"] is True


def test_hard_timeout_cancelled_folder_pages_stay_in_result_with_cursor(monkeypatch, tmp_path):
    """夹内作品逐页直读路径同样受中断一致性保护：取消落在第二页等待中时，
    第一页作品保留在结果里、夹游标与之严格一致；确认后下一轮从游标续传。"""
    import types

    calls: list[int] = []
    state = {"hang": True}

    class Handler:
        kwargs: dict = {}

        def __init__(self, kwargs):
            self.kwargs = kwargs

        def fetch_user_collects(self, **kwargs):
            async def gen():
                yield _FakePage({"status_code": 0, "has_more": 0, "cursor": 0,
                                 "collects_list": [{"collects_id": "F1", "collects_name": "一",
                                                    "total_number": 2}]})
            return gen()

    async def fake_fetch_video(self, params):
        calls.append(int(params.cursor))
        if int(params.cursor) == 0:
            return {"status_code": 0, "has_more": 1, "cursor": 5,
                    "aweme_list": [{"aweme_id": "F1W1", "desc": "w1", "create_time": 1746000001,
                                    "author": {"nickname": "a", "sec_uid": "sec-1"},
                                    "video": {"cover": {"url_list": ["https://c.example/1.jpg"]}}}]}
        if state["hang"]:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await asyncio.sleep(60)
        return {"status_code": 0, "has_more": 0, "cursor": 0,
                "aweme_list": [{"aweme_id": "F1W2", "desc": "w2", "create_time": 1746000002,
                                "author": {"nickname": "a", "sec_uid": "sec-1"},
                                "video": {"cover": {"url_list": ["https://c.example/2.jpg"]}}}]}

    _install_f2_conf_stub(monkeypatch, headers=_official_headers(), handler=Handler,
                          browser={"name": "Edge", "version": "130.0.0.0"})
    model_mod = types.ModuleType("f2.apps.douyin.model")

    class UserCollectsVideo:
        def __init__(self, cursor=0, count=0, collects_id=""):
            self.cursor = cursor
            self.count = count
            self.collects_id = collects_id

    model_mod.UserCollectsVideo = UserCollectsVideo
    monkeypatch.setitem(sys.modules, "f2.apps.douyin.model", model_mod)
    crawler_mod = sys.modules["f2.apps.douyin.crawler"]
    monkeypatch.setattr(crawler_mod.DouyinCrawler, "fetch_user_collects_video",
                        fake_fetch_video, raising=False)
    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.0.1.7")
    monkeypatch.setattr(fetch, "_BUDGET_GRACE_SECONDS", 0.1)
    args = _progress_args(tmp_path, scopes="collections", budget=2,
                          sec_user_id=_SYNTH_MANUAL_ID)
    progress = tmp_path / "sync_progress.json"
    folder_key = fetch._identity_hash("F1")

    first = fetch._run_list_mode(args)
    assert first["partial"] is True
    assert [w["external_id"] for w in first["collects"]["folder_items"]["F1"]["works"]] == ["F1W1"]
    assert first["collects"]["folder_incomplete"] == ["folder#1"]
    assert first["collects"]["complete"] is False
    assert first["resume"]["scopes"]["collections"]["folders"][folder_key] == {
        "complete": False, "cursor": 5}
    assert fetch.write_confirmed_progress(str(args.progress_file), first["resume"])

    state["hang"] = False
    second = fetch._run_list_mode(args)
    assert calls == [0, 5, 5]  # 第二轮从已确认游标 5 续传
    assert second["collects"]["complete"] is True
    assert [w["external_id"] for w in second["collects"]["folder_items"]["F1"]["works"]] == ["F1W2"]
    assert second["collects"]["folder_incomplete"] == []  # 完成后撤下增量阶段标记
    assert second.get("partial") is not True
    assert fetch.write_confirmed_progress(str(args.progress_file), second["resume"])
    assert not progress.exists()


# ---------- 目录分页跨轮续传（第 3 轮复审回归） ----------

def _install_folder_list_stub(monkeypatch, *, list_fn, item_fn):
    """目录+夹内桩：list_fn(max_cursor) 产出目录页；item_fn 处理单夹作品。"""
    import importlib.metadata

    class Handler:
        kwargs: dict = {}

        def __init__(self, kwargs):
            self.kwargs = kwargs

        def fetch_user_collects(self, max_cursor=0, **kwargs):
            async def gen():
                for page in list_fn(int(max_cursor or 0)):
                    yield _FakePage(page)
            return gen()

    _install_self_identity_stub(monkeypatch, handler_cls=Handler,
                                response={"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}})
    monkeypatch.setattr(fetch, "_fetch_folder_items_raw", item_fn)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.0.1.7")


def _folder_page(folder_id, *, has_more, cursor):
    return {"status_code": 0, "has_more": has_more, "cursor": cursor,
            "collects_list": [{"collects_id": folder_id, "collects_name": f"夹{folder_id}",
                               "total_number": 0}]}


def test_folder_list_resume_advances_across_rounds(monkeypatch, tmp_path):
    """目录分页必须跨轮推进：每轮页数受限时，第二轮从上一轮水位线继续读
    下一页，而不是每轮都从第 1 页重启（旧版后续收藏夹永远无法被发现）。"""
    list_calls: list[int] = []
    item_calls: list[str] = []

    def list_fn(start_cursor):
        # 惰性同步生成器：目录页被消费到才记录（页数受限时后续页不产生）；
        # 响应 cursor 指向下一页（第 1 页 cursor=1，第 2 页 cursor=2）
        for index in (1, 2):
            if index <= start_cursor:
                continue
            list_calls.append(index)
            yield _folder_page(f"F{index}", has_more=int(index == 1), cursor=index)

    async def item_fn(handler, folder_id, *, label, args, warnings, **kwargs):
        item_calls.append(folder_id)
        return [], {"pages": 1, "items": 0, "complete": True, "server_oversize_pages": 0}

    _install_folder_list_stub(monkeypatch, list_fn=list_fn, item_fn=item_fn)
    args = _progress_args(tmp_path, scopes="collections", budget=10)
    args.max_pages_per_list = 1  # 每轮只允许读一页目录
    progress = tmp_path / "sync_progress.json"

    first = fetch._run_list_mode(args)
    assert list_calls == [1] and item_calls == ["F1"]
    assert first["collects"]["complete"] is False
    assert first["resume"]["scopes"]["collections"]["list_cursor"] == 1  # 水位线推进
    assert fetch.write_confirmed_progress(str(args.progress_file), first["resume"])

    second = fetch._run_list_mode(args)
    assert list_calls == [1, 2]      # 旧版此处为 [1, 1]：目录永远停在第一页
    assert item_calls == ["F1", "F2"]  # F1 已完成，不重复处理
    assert [f["folder_id"] for f in second["collects"]["folders"]] == ["F2"]
    assert second["collects"]["complete"] is True and second.get("partial") is not True
    assert second["resume"]["all_complete"] is True
    assert fetch.write_confirmed_progress(str(args.progress_file), second["resume"])
    assert not progress.exists()  # 全部完整，确认步清除，下一轮全新完整同步

    third = fetch._run_list_mode(args)   # 清除后全新同步：每轮 1 页，重读第 1 页
    assert list_calls == [1, 2, 1]
    assert [f["folder_id"] for f in third["collects"]["folders"]] == ["F1"]
    assert fetch.write_confirmed_progress(str(args.progress_file), third["resume"])
    fetch._run_list_mode(args)           # 第二轮读到第 2 页
    assert list_calls == [1, 2, 1, 2]


def test_folder_list_watermark_holds_until_page_folders_complete(monkeypatch, tmp_path):
    """页内还有未完成夹时水位线必须停留：下一轮重读该页，已完成夹按哈希跳过
    （不重复处理），未完成夹继续，重放幂等；页内全部终结后才推进水位线。"""
    list_calls: list[int] = []
    item_calls: list[str] = []
    state = {"f2_complete": False}

    def list_fn(start_cursor):
        for index, folders in ((1, ("F1", "F2")), (2, ("F3",))):
            if index <= start_cursor:
                continue
            list_calls.append(index)
            yield {"status_code": 0, "has_more": int(index == 1), "cursor": index,
                   "collects_list": [{"collects_id": fid, "collects_name": fid,
                                      "total_number": 1} for fid in folders]}

    async def item_fn(handler, folder_id, *, label, args, warnings, **kwargs):
        item_calls.append(folder_id)
        complete = folder_id != "F2" or state["f2_complete"]
        return [], {"pages": 1, "items": 0, "complete": complete,
                    "server_oversize_pages": 0}

    _install_folder_list_stub(monkeypatch, list_fn=list_fn, item_fn=item_fn)
    args = _progress_args(tmp_path, scopes="collections", budget=10)
    args.max_pages_per_list = 1

    first = fetch._run_list_mode(args)
    assert item_calls == ["F1", "F2"]
    assert first["collects"]["complete"] is False
    assert first["resume"]["scopes"]["collections"]["list_cursor"] == 0  # F2 未完成：停留
    assert fetch.write_confirmed_progress(str(args.progress_file), first["resume"])

    state["f2_complete"] = True
    second = fetch._run_list_mode(args)
    assert list_calls == [1, 1]          # 重读水位线页
    assert item_calls == ["F1", "F2", "F2"]  # F1 按哈希跳过不重复处理；F2 幂等续作
    assert [f["folder_id"] for f in second["collects"]["folders"]] == ["F1", "F2"]
    assert second["resume"]["scopes"]["collections"]["list_cursor"] == 1  # 页内全部终结：推进
    assert fetch.write_confirmed_progress(str(args.progress_file), second["resume"])

    third = fetch._run_list_mode(args)   # 水位线之后：第 2 页 F3
    assert list_calls == [1, 1, 2] and item_calls == ["F1", "F2", "F2", "F3"]
    assert third["collects"]["complete"] is True and third.get("partial") is not True
    assert fetch.write_confirmed_progress(str(args.progress_file), third["resume"])
    assert not (tmp_path / "sync_progress.json").exists()


def test_folder_list_watermark_holds_on_folder_failure_and_retries(monkeypatch, tmp_path):
    """单夹失败：水位线停留，下一轮重读该页并重试失败夹（既有夹跳过）。"""
    list_calls: list[int] = []
    item_calls: list[str] = []
    state = {"f2_fail": True}

    def list_fn(start_cursor):
        for index, folders in ((1, ("F1", "F2")), (2, ("F3",))):
            if index <= start_cursor:
                continue
            list_calls.append(index)
            yield {"status_code": 0, "has_more": int(index == 1), "cursor": index,
                   "collects_list": [{"collects_id": fid, "collects_name": fid,
                                      "total_number": 1} for fid in folders]}

    async def item_fn(handler, folder_id, *, label, args, warnings, **kwargs):
        item_calls.append(folder_id)
        if folder_id == "F2" and state["f2_fail"]:
            raise fetch.ScopeError("platform_error", f"{label} 响应缺少 aweme_list 字段")
        return [], {"pages": 1, "items": 0, "complete": True, "server_oversize_pages": 0}

    _install_folder_list_stub(monkeypatch, list_fn=list_fn, item_fn=item_fn)
    args = _progress_args(tmp_path, scopes="collections", budget=10)
    args.max_pages_per_list = 1

    first = fetch._run_list_mode(args)
    assert first["collects"]["folder_failures"] == [{"folder": "folder#2", "kind": "platform_error"}]
    assert first["resume"]["scopes"]["collections"]["list_cursor"] == 0  # 失败：停留重试
    assert fetch.write_confirmed_progress(str(args.progress_file), first["resume"])

    state["f2_fail"] = False
    second = fetch._run_list_mode(args)
    assert list_calls == [1, 1] and item_calls == ["F1", "F2", "F2"]  # 失败夹被重试
    assert second["resume"]["scopes"]["collections"]["list_cursor"] == 1
    assert fetch.write_confirmed_progress(str(args.progress_file), second["resume"])

    third = fetch._run_list_mode(args)
    assert third["collects"]["complete"] is True
    assert fetch.write_confirmed_progress(str(args.progress_file), third["resume"])
    assert not (tmp_path / "sync_progress.json").exists()


@pytest.mark.parametrize("cursor_value", [0, None])
def test_folder_list_invalid_cursor_reports_incomplete_and_never_advances(monkeypatch, tmp_path,
                                                                         cursor_value):
    """has_more=1 但响应游标缺失/无效：如实不完整，水位线停留，不虚报完成；
    下一轮重读同一页（不空转推进、不误跳后续收藏夹）。"""
    list_calls: list[int] = []
    item_calls: list[str] = []

    def list_fn(start_cursor):
        for index in (1, 2):
            if index <= start_cursor:
                continue
            list_calls.append(index)
            page = {"status_code": 0, "has_more": int(index == 1),
                    "collects_list": [{"collects_id": f"F{index}", "collects_name": f"夹{index}",
                                       "total_number": 0}]}
            if cursor_value is not None:
                page["cursor"] = cursor_value
            yield page

    async def item_fn(handler, folder_id, *, label, args, warnings, **kwargs):
        item_calls.append(folder_id)
        return [], {"pages": 1, "items": 0, "complete": True, "server_oversize_pages": 0}

    _install_folder_list_stub(monkeypatch, list_fn=list_fn, item_fn=item_fn)
    args = _progress_args(tmp_path, scopes="collections", budget=10)
    args.max_pages_per_list = 5

    first = fetch._run_list_mode(args)
    assert item_calls == ["F1"]
    assert first["collects"]["complete"] is False and first["collects"]["folders_complete"] is False
    assert "folders" in first["collects"].get("budget_exhausted", "") or first["collects"].get(
        "folders_complete") is False
    assert first["resume"]["scopes"]["collections"]["list_cursor"] == 0
    assert fetch.write_confirmed_progress(str(args.progress_file), first["resume"])

    second = fetch._run_list_mode(args)
    assert list_calls == [1, 1]  # 重读同一页，不凭空推进
    assert item_calls == ["F1"]  # F1 已完成：哈希跳过，不重复处理（重放幂等）
    assert second["collects"]["complete"] is False  # 目录无法推进：不虚报完成
    assert second["resume"]["scopes"]["collections"]["list_cursor"] == 0
    assert fetch.write_confirmed_progress(str(args.progress_file), second["resume"])


def test_folder_list_resume_after_lost_confirmation_replays_idempotently(monkeypatch, tmp_path):
    """提交/确认失败恢复：取数完成但确认未落盘时，下一轮从原水位线重读并
    幂等重放已读夹（不跳过、不漏夹）；确认成功后才推进。"""
    list_calls: list[int] = []
    item_calls: list[str] = []

    def list_fn(start_cursor):
        for index in (1, 2):
            if index <= start_cursor:
                continue
            list_calls.append(index)
            yield _folder_page(f"F{index}", has_more=int(index == 1), cursor=index)

    async def item_fn(handler, folder_id, *, label, args, warnings, **kwargs):
        item_calls.append(folder_id)
        return [], {"pages": 1, "items": 0, "complete": True, "server_oversize_pages": 0}

    _install_folder_list_stub(monkeypatch, list_fn=list_fn, item_fn=item_fn)
    args = _progress_args(tmp_path, scopes="collections", budget=10)
    args.max_pages_per_list = 1

    first = fetch._run_list_mode(args)
    assert first["resume"]["scopes"]["collections"]["list_cursor"] == 1
    # 模拟 DB 提交后、确认落盘前进程死亡：不写确认状态
    assert not (tmp_path / "sync_progress.json").exists()

    second = fetch._run_list_mode(args)  # 从原水位线（0）重读，F1 幂等重放
    assert list_calls == [1, 1] and item_calls == ["F1", "F1"]
    assert second["resume"]["scopes"]["collections"]["list_cursor"] == 1
    assert fetch.write_confirmed_progress(str(args.progress_file), second["resume"])

    third = fetch._run_list_mode(args)   # 确认成功后才推进到第 2 页
    assert list_calls == [1, 1, 2] and item_calls == ["F1", "F1", "F2"]
    assert third["collects"]["complete"] is True
    assert fetch.write_confirmed_progress(str(args.progress_file), third["resume"])
    assert not (tmp_path / "sync_progress.json").exists()


def test_folder_list_order_change_misses_only_until_next_full_pass(monkeypatch, tmp_path):
    """目录顺序变化：两轮之间新插入到水位线之前的收藏夹，在当前轮次中不被
    发现（既有水位线语义的边界）；本轮走完、状态清除后的下一轮完整同步会发现它。"""
    list_calls: list[int] = []
    item_calls: list[str] = []
    state = {"insert_f0": False}

    def list_fn(start_cursor):
        for index in (1, 2):
            if index <= start_cursor:
                continue
            list_calls.append(index)
            ids = (["F0"] if (index == 1 and state["insert_f0"]) else []) + [f"F{index}"]
            yield {"status_code": 0, "has_more": int(index == 1), "cursor": index,
                   "collects_list": [{"collects_id": fid, "collects_name": fid,
                                      "total_number": 0} for fid in ids]}

    async def item_fn(handler, folder_id, *, label, args, warnings, **kwargs):
        item_calls.append(folder_id)
        return [], {"pages": 1, "items": 0, "complete": True, "server_oversize_pages": 0}

    _install_folder_list_stub(monkeypatch, list_fn=list_fn, item_fn=item_fn)
    args = _progress_args(tmp_path, scopes="collections", budget=10)
    args.max_pages_per_list = 1

    first = fetch._run_list_mode(args)   # 第 1 页：F1 完成，水位线 → 1
    assert fetch.write_confirmed_progress(str(args.progress_file), first["resume"])

    state["insert_f0"] = True            # 平台在水位线之前插入 F0
    second = fetch._run_list_mode(args)  # 从水位线续传：只见第 2 页 F2
    assert list_calls == [1, 2] and item_calls == ["F1", "F2"]
    assert "F0" not in [f["folder_id"] for f in second["collects"]["folders"]]
    assert second["collects"]["complete"] is True
    assert fetch.write_confirmed_progress(str(args.progress_file), second["resume"])
    assert not (tmp_path / "sync_progress.json").exists()  # 本轮完整，状态清除

    third = fetch._run_list_mode(args)   # 下一轮完整同步：第 1 页发现 F0
    assert item_calls == ["F1", "F2", "F0", "F1"]
    assert "F0" in [f["folder_id"] for f in third["collects"]["folders"]]


# ---------- 目录页含无标识记录：如实不完整、不跳页（第 5 轮复审回归） ----------

def _unidentified_record(kind: str):
    """构造无标识目录项：非对象 / 缺 collects_id / 空串 / 纯空白。"""
    if kind == "non_dict":
        return "not-a-folder"
    if kind == "missing_id":
        return {"collects_name": "missing identity", "total_number": 1}
    if kind == "empty_id":
        return {"collects_id": "", "collects_name": "empty", "total_number": 1}
    return {"collects_id": "   ", "collects_name": "blank", "total_number": 1}


def test_folder_list_unidentified_record_holds_watermark_and_reports_incomplete(
        monkeypatch, tmp_path):
    """首/中/末页出现无标识目录项：该页水位线必须停留、目录如实不完整、
    绝不虚报完成或清除状态；页内有效夹照常处理并保全。"""
    for record_kind in ("non_dict", "missing_id", "empty_id", "blank_id"):
        for invalid_page in (1, 2):  # 首页 / 中间页（末页单独覆盖）
            list_calls: list[int] = []
            item_calls: list[str] = []

            def list_fn(start_cursor, _kind=record_kind, _page=invalid_page):
                for index in (1, 2, 3):
                    if index <= start_cursor:
                        continue
                    list_calls.append(index)
                    page = {"status_code": 0, "has_more": int(index < 3), "cursor": index,
                            "collects_list": [{"collects_id": f"F{index}",
                                               "collects_name": f"夹{index}",
                                               "total_number": 0}]}
                    if index == _page:
                        page["collects_list"].append(_unidentified_record(_kind))
                    yield page

            async def item_fn(handler, folder_id, *, label, args, warnings, **kwargs):
                item_calls.append(folder_id)
                return [], {"pages": 1, "items": 0, "complete": True,
                            "server_oversize_pages": 0}

            _install_folder_list_stub(monkeypatch, list_fn=list_fn, item_fn=item_fn)
            args = _progress_args(tmp_path, scopes="collections", budget=10)
            args.max_pages_per_list = 5
            progress = tmp_path / "sync_progress.json"
            if progress.exists():
                progress.unlink()
            list_calls.clear()
            item_calls.clear()

            first = fetch._run_list_mode(args)
            # 有效夹全部处理并保全（含异常页上的有效夹）
            assert item_calls == ["F1", "F2", "F3"], (record_kind, invalid_page, item_calls)
            block = first["collects"]
            assert block["complete"] is False, (record_kind, invalid_page)
            assert block["folders_complete"] is False, (record_kind, invalid_page)
            # 水位线停在异常页起始，不越过未知项
            expected_cursor = invalid_page - 1
            assert first["resume"]["scopes"]["collections"]["list_cursor"] == expected_cursor, \
                (record_kind, invalid_page)
            assert any("缺少收藏夹标识" in w for w in first["warnings"]), record_kind
            assert fetch.write_confirmed_progress(str(args.progress_file), first["resume"])
            saved = json.loads(progress.read_text(encoding="utf-8"))
            assert saved["scopes"]["collections"]["complete"] is False

            # 异常未恢复：重读异常页，已完成夹哈希跳过，绝不虚报完成或清除状态
            second = fetch._run_list_mode(args)
            assert second["collects"]["complete"] is False, (record_kind, invalid_page)
            assert second["resume"]["scopes"]["collections"]["list_cursor"] == expected_cursor
            assert fetch.write_confirmed_progress(str(args.progress_file), second["resume"])
            assert progress.exists()


def test_folder_list_unidentified_record_on_last_page_never_reports_complete(
        monkeypatch, tmp_path):
    """末页（has_more=0）含无标识目录项：目录读完也不得虚报完整、不得清除状态。"""
    list_calls: list[int] = []
    item_calls: list[str] = []

    def list_fn(start_cursor):
        for index in (1, 2):
            if index <= start_cursor:
                continue
            list_calls.append(index)
            page = {"status_code": 0, "has_more": int(index == 1), "cursor": index,
                    "collects_list": [{"collects_id": f"F{index}", "collects_name": f"夹{index}",
                                       "total_number": 0}]}
            if index == 2:
                page["collects_list"].append({"collects_name": "missing identity",
                                              "total_number": 1})
            yield page

    async def item_fn(handler, folder_id, *, label, args, warnings, **kwargs):
        item_calls.append(folder_id)
        return [], {"pages": 1, "items": 0, "complete": True, "server_oversize_pages": 0}

    _install_folder_list_stub(monkeypatch, list_fn=list_fn, item_fn=item_fn)
    args = _progress_args(tmp_path, scopes="collections", budget=10)
    args.max_pages_per_list = 5
    progress = tmp_path / "sync_progress.json"

    first = fetch._run_list_mode(args)
    assert item_calls == ["F1", "F2"]  # 有效夹保全
    assert first["collects"]["complete"] is False and first["collects"]["folders_complete"] is False
    assert first["resume"]["scopes"]["collections"]["list_cursor"] == 1  # 停在末页起始
    assert any("缺少收藏夹标识" in w for w in first["warnings"])
    assert fetch.write_confirmed_progress(str(args.progress_file), first["resume"])

    second = fetch._run_list_mode(args)  # 重读末页：异常仍在 → 仍不完整，状态保留
    assert second["collects"]["complete"] is False
    assert progress.exists()


def test_folder_list_unidentified_record_recovery_when_platform_corrects(monkeypatch, tmp_path):
    """异常消失后（平台补全标识）：重读异常页可安全推进并最终完整清除；
    已完成夹不重复处理，已有事实不破坏。"""
    list_calls: list[int] = []
    item_calls: list[str] = []
    state = {"page2_broken": True}

    def list_fn(start_cursor):
        for index in (1, 2):
            if index <= start_cursor:
                continue
            list_calls.append(index)
            page = {"status_code": 0, "has_more": int(index == 1), "cursor": index,
                    "collects_list": [{"collects_id": f"F{index}", "collects_name": f"夹{index}",
                                       "total_number": 0}]}
            if index == 2 and state["page2_broken"]:
                page["collects_list"].append({"collects_name": "missing identity",
                                              "total_number": 1})
            yield page

    async def item_fn(handler, folder_id, *, label, args, warnings, **kwargs):
        item_calls.append(folder_id)
        return [], {"pages": 1, "items": 0, "complete": True, "server_oversize_pages": 0}

    _install_folder_list_stub(monkeypatch, list_fn=list_fn, item_fn=item_fn)
    args = _progress_args(tmp_path, scopes="collections", budget=10)
    args.max_pages_per_list = 5
    progress = tmp_path / "sync_progress.json"

    first = fetch._run_list_mode(args)   # 末页异常：停在末页起始（水位线 1）
    assert first["resume"]["scopes"]["collections"]["list_cursor"] == 1
    assert fetch.write_confirmed_progress(str(args.progress_file), first["resume"])

    state["page2_broken"] = False        # 平台补全标识
    second = fetch._run_list_mode(args)  # 重读末页：F2 哈希跳过，页内全部终结 → 完整
    assert list_calls == [1, 2, 2]
    assert item_calls == ["F1", "F2"]    # F2 不重复处理
    assert second["collects"]["complete"] is True and second.get("partial") is not True
    assert second["resume"]["all_complete"] is True
    assert fetch.write_confirmed_progress(str(args.progress_file), second["resume"])
    assert not progress.exists()


# ---------- 确认步写入/替换/清理失败必须如实上报（第 6 轮复审回归） ----------

def test_write_confirmed_progress_reports_write_replace_and_clear_failures(
        monkeypatch, tmp_path):
    """进度确认的临时写入、原子替换、清理失败都不得当作确认成功：
    返回 False、旧确认状态原样保留、临时文件不残留；故障恢复后可成功。"""
    path = tmp_path / "sync_progress.json"
    partial = {"version": 2, "fingerprint": "fp", "all_complete": False,
               "scopes": {"like": {"complete": False, "cursor": 5}}}
    done = dict(partial, all_complete=True)
    assert fetch.write_confirmed_progress(str(path), partial) is True
    original = path.read_text(encoding="utf-8")

    # 临时写入失败
    with monkeypatch.context() as m:
        def broken_write(self, *args, **kwargs):
            raise OSError("synthetic tmp write failure")
        m.setattr(Path, "write_text", broken_write)
        assert fetch.write_confirmed_progress(str(path), partial) is False
        assert path.read_text(encoding="utf-8") == original  # 旧确认状态保全
    assert fetch.write_confirmed_progress(str(path), partial) is True  # 恢复后成功

    # 原子替换失败
    with monkeypatch.context() as m:
        def broken_replace(src, dst, *args, **kwargs):
            raise OSError("synthetic replace failure")
        m.setattr(os, "replace", broken_replace)
        assert fetch.write_confirmed_progress(str(path), partial) is False
        assert path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*.tmp"))  # 失败不残留半份状态

    # 清理失败（unlink 被拒绝）：不得当作确认成功
    with monkeypatch.context() as m:
        def broken_unlink(self, missing_ok=False):
            raise OSError("synthetic unlink failure")
        m.setattr(Path, "unlink", broken_unlink)
        assert fetch.write_confirmed_progress(str(path), done) is False
        assert path.exists()  # 旧状态仍在，但调用方已知失败
    assert fetch.write_confirmed_progress(str(path), done) is True  # 恢复后清除成功
    assert not path.exists()


def test_write_confirmed_progress_reports_real_permission_failure(tmp_path, monkeypatch):
    """真实目录权限故障（chmod 0o500）：写入与清理都返回 False；权限恢复后
    确认成功并清除状态（POSIX）。"""
    import fcntl  # noqa: F401 - POSIX 断言前提（与本套件其余锁测试一致）

    progress_dir = tmp_path / "progress"
    progress_dir.mkdir(mode=0o700)
    state = progress_dir / "sync_progress.json"
    partial = {"version": 2, "fingerprint": "fp", "all_complete": False,
               "scopes": {"like": {"complete": False, "cursor": 5}}}
    done = dict(partial, all_complete=True)
    assert fetch.write_confirmed_progress(str(state), partial) is True

    os.chmod(progress_dir, 0o500)
    try:
        assert fetch.write_confirmed_progress(str(state), done) is False  # 清理失败
        assert state.exists()
        advanced = dict(partial, scopes={"like": {"complete": False, "cursor": 9}})
        assert fetch.write_confirmed_progress(str(state), advanced) is False  # 写入失败
        assert _json.loads(state.read_text(encoding="utf-8"))["scopes"]["like"]["cursor"] == 5
    finally:
        os.chmod(progress_dir, 0o700)
    assert fetch.write_confirmed_progress(str(state), done) is True  # 恢复后成功
    assert not state.exists()


def test_progress_confirm_failure_rounds_hold_state_and_recovery_rediscovers(
        monkeypatch, tmp_path):
    """第 6 轮复审场景（真实权限故障 + 恢复）：清理失败期间旧确认状态保全、
    重放幂等、确认如实返回 False；权限恢复后确认成功清除状态，下一轮重新
    从头发现新点赞，不再因残留 complete 状态长期漏查。"""
    calls = {"like": 0, "collection": 0}

    class Handler:
        kwargs: dict = {}

        def __init__(self, kwargs):
            self.kwargs = kwargs

        def fetch_user_like_videos(self, sec_user_id, max_cursor=0, **kwargs):
            calls["like"] += 1

            async def gen():
                yield _FakePage({"status_code": 0, "aweme_list": [_work_entry("9000001")],
                                 "has_more": 0, "max_cursor": 0})
            return gen()

        def fetch_user_collection_videos(self, max_cursor=0, **kwargs):
            calls["collection"] += 1

            async def gen():
                # 链式翻页（与真实 F2 handler 生成器一致）：从起始游标读到 has_more=0
                if int(max_cursor or 0) < 111:
                    yield _FakePage({"status_code": 0, "aweme_list": [_work_entry("9000003")],
                                     "has_more": 1, "cursor": 111})
                yield _FakePage({"status_code": 0, "aweme_list": [_work_entry("9000002")],
                                 "has_more": 0, "cursor": 0})
            return gen()

    _install_self_identity_stub(monkeypatch, handler_cls=Handler,
                                response={"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}})
    args = _progress_args(tmp_path, scopes="like,collection", budget=10,
                          sec_user_id=_SYNTH_MANUAL_ID)
    progress = tmp_path / "sync_progress.json"
    fingerprint = fetch._progress_fingerprint(
        sec_user_id=_SYNTH_MANUAL_ID, scopes=["like", "collection"], folder_ids="",
        page_counts=20, max_items=100, max_pages=50,
        cookie_hash=fetch._identity_hash("sessionid=synthetic"))
    stale = {"version": fetch._PROGRESS_VERSION, "fingerprint": fingerprint,
             "all_complete": False,
             "scopes": {"like": {"complete": True, "cursor": 0},
                        "collection": {"complete": False, "cursor": 111}}}
    assert fetch.write_confirmed_progress(str(progress), stale) is True

    os.chmod(tmp_path, 0o500)  # 真实权限故障：进度文件无法清理/写入
    try:
        first = fetch._run_list_mode(args)
        assert first["like"].get("resumed") is True       # 旧状态保全：like 按哈希跳过
        assert first["resume"]["all_complete"] is True
        assert [w["external_id"] for w in first["collection"]["works"]] == ["9000002"]
        assert fetch.write_confirmed_progress(str(progress), first["resume"]) is False
        assert progress.exists()                          # 旧状态残留，但调用方已知失败

        second = fetch._run_list_mode(args)               # 故障持续：重放幂等、仍如实失败
        assert calls == {"like": 0, "collection": 2}      # like 跳过；collection 幂等重放
        assert fetch.write_confirmed_progress(str(progress), second["resume"]) is False
        assert progress.exists()
    finally:
        os.chmod(tmp_path, 0o700)                         # 文件系统恢复

    third = fetch._run_list_mode(args)
    assert fetch.write_confirmed_progress(str(progress), third["resume"]) is True
    assert not progress.exists()                          # 恢复后确认成功、状态清除

    fourth = fetch._run_list_mode(args)                   # 全新一轮：重新发现点赞
    assert calls["like"] == 1                             # 旧残留不再长期漏查新点赞
    assert [w["external_id"] for w in fourth["like"]["works"]] == ["9000001"]
    assert fourth["like"]["complete"] is True
    assert fetch.write_confirmed_progress(str(progress), fourth["resume"]) is True
    assert not progress.exists()
