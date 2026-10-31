"""真实 F2 运行时回归检查（在 tools/f2-runtime/.venv 内由 pytest 以子进程运行）。

用**真实 F2 Filter、handler 与底层 crawler**（网络打桩）验证消费层语义，
避免只用属性语义不同的假页面：
- 真实 UserPostFilter 把缺失 has_more 强转 False → 必须看原始响应键；
- 夹内作品逐页直读 crawler 原始响应：合法空夹 / 夹内 status_code=8 →
  login_expired / 下一页为空 / 多夹混合（一个空夹不中断其他夹）；
- 真实 F2 异常类的翻译与消息脱敏；
- 点赞范围缺 sec_user_id 提前报错。
合成 cookie 写入 tempfile，绝不写真实业务目录。

断网保证：本脚本在子进程内先安装 socket 守卫（任何 connect/getaddrinfo
都会抛错），并对 F2 请求模型的 msToken 生成打合成桩——真实 F2 在构造
UserCollectsVideo 等请求模型时会调用 TokenManager.cached_msToken()，
首次调用会联网请求 mssdk.bytedance.com；不打桩就不是真正的无网络测试。
全程无任何真实网络请求。

输出 JSON：{"ok": true/false, "checks": {...}, "failures": [...]}
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
import tempfile
from pathlib import Path

import httpx

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE_ROOT))


def install_network_guard() -> None:
    """子进程级断网：任何套接字连接/DNS 解析都立即失败（防线，不是提示）。"""
    class _Blocked(RuntimeError):
        pass

    def _blocked(*args, **kwargs):  # noqa: ANN002, ANN003
        raise _Blocked("network access attempted in offline check")

    socket.socket.connect = _blocked
    socket.socket.connect_ex = _blocked
    socket.create_connection = _blocked
    socket.getaddrinfo = _blocked


install_network_guard()

import monitor.platforms.f2_fetch as fetch  # noqa: E402
from f2.apps.douyin.crawler import DouyinCrawler  # noqa: E402
from f2.apps.douyin.filter import UserPostFilter  # noqa: E402
from f2.apps.douyin.handler import DouyinHandler, rich_console  # noqa: E402
from f2.apps.douyin.model import (
    UserCollects,
    UserCollectsVideo,
    UserCollection,
    UserLike,
)  # noqa: E402
from f2.apps.douyin.utils import ABogusManager, ClientConfManager, TokenManager  # noqa: E402

# 第 5 节会用网络桩替换这些类方法；第 9 节验证签名前需要恢复真实实现
_ORIGINAL_CRAWLER_METHODS = {
    name: getattr(DouyinCrawler, name)
    for name in ("fetch_user_like", "fetch_user_collection",
                 "fetch_user_collects", "fetch_user_collects_video",
                 "fetch_post_detail")
}

# msToken 合成桩：真实 F2 的请求模型（UserCollects/UserCollectsVideo 等）在类定义时
# 把 TokenManager.cached_msToken 绑定进 pydantic default_factory，事后替换方法无效；
# cached_msToken 只查 cls._msToken_cache 缓存，为 None 才联网请求 mssdk.bytedance.com。
# 因此：预置缓存值（原函数直接返回、不联网）+ 桩掉真实获取函数兜底。
TokenManager._msToken_cache = "synthetic-ms-token-for-offline-tests"
TokenManager.gen_real_msToken = classmethod(lambda cls: "synthetic-ms-token-for-offline-tests")

# 静默 F2 日志与 rich 输出（同 f2_fetch 主流程一致）
rich_console.quiet = True
logging.disable(logging.CRITICAL)

failures: list[str] = []

# 合成身份证（仅用于检查，不来自任何真实账号）
_SYNTH_SELF_ID = "MS4wLjABAAAsyntheticselfidentity0001"
_SYNTH_MANUAL_ID = "MS4wLjABAAAsyntheticmanualidentity01"


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        failures.append(f"{name}: {detail}")


def _aweme(i: int) -> dict:
    return {"aweme_id": str(i), "desc": f"w{i}", "create_time": 1746000000 + i,
            "author": {"nickname": "a", "sec_uid": "MS4wLjABAAAsynthetic0000000000"},
            "video": {"cover": {"url_list": ["https://c.example/1.jpg"]}}}


async def _consume(pages, *, label="like", page_counts=20, max_pages=100, max_items=100):
    warnings: list[str] = []
    works, meta = await fetch.consume_video_pages(
        (lambda h: _iter(pages)), label=label, requested_page_counts=page_counts,
        max_pages=max_pages, max_items=max_items, warnings=warnings)
    return works, meta, warnings


async def _iter(pages):
    for page in pages:
        yield page


async def main() -> dict:
    checks: dict = {}

    # 1. 真实 Filter：缺 aweme_list → 平台错误，不冒充 0 条
    page = UserPostFilter({"status_code": 0})
    check("real_filter_has_more_missing_coerced_false", page.has_more is False)
    try:
        await _consume([page])
        checks["missing_aweme_list"] = "NO_ERROR_BAD"
    except fetch.ScopeError as exc:
        checks["missing_aweme_list"] = exc.kind
    check("missing_aweme_list", checks["missing_aweme_list"] == "platform_error",
          str(checks["missing_aweme_list"]))

    # 2. 真实 Filter：aweme_list 在、has_more 缺 → 不完整 + 警告
    warnings: list[str] = []
    works, meta = await fetch.consume_video_pages(
        lambda h: _iter([UserPostFilter({"status_code": 0, "aweme_list": [_aweme(1)]})]),
        label="like", requested_page_counts=20, max_pages=10, max_items=10, warnings=warnings)
    check("missing_has_more_incomplete", meta["complete"] is False and any("has_more" in w for w in warnings),
          f"complete={meta['complete']} warnings={warnings}")

    # 3. 真实 Filter：正常结束 + 跨页去重 + 超量页计数
    works, meta, warnings = await _consume([
        UserPostFilter({"status_code": 0, "aweme_list": [_aweme(1), _aweme(2)], "has_more": 1}),
        UserPostFilter({"status_code": 0, "aweme_list": [_aweme(2), _aweme(3)], "has_more": 0}),
    ])
    check("complete_end_dedup", meta["complete"] is True and [w["external_id"] for w in works] == ["1", "2", "3"],
          f"meta={meta} works={works}")

    works, meta, _ = await _consume(
        [UserPostFilter({"status_code": 0, "aweme_list": [_aweme(i) for i in range(5)], "has_more": 0})],
        page_counts=2)
    check("oversize_counted", meta["server_oversize_pages"] == 1 and len(works) == 5, f"meta={meta}")

    # 4. 真实 handler 生成器（网络桩）：收藏夹列表 status_code=8 → login_expired
    handler = DouyinHandler({
        "headers": fetch.official_list_headers(), "cookie": "a=b", "timeout": 0, "max_retries": 1,
        "proxies": {"http://": None, "https://": None},
    })

    async def fake_collects_login(self, params):
        return {"status_code": 8, "collects_list": [], "has_more": 0}

    DouyinCrawler.fetch_user_collects = fake_collects_login
    args = type("Args", (), {"page_counts": 20, "max_items_per_list": 100,
                             "max_pages_per_list": 50, "folder_ids": "", "timeout": 0})()
    result: dict = {}
    try:
        await fetch.fetch_collections_scope(handler, args, result, [])
        checks["collects_status_8"] = "NO_ERROR_BAD"
    except fetch.ScopeError as exc:
        checks["collects_status_8"] = exc.kind
    check("collects_status_8", checks["collects_status_8"] == "login_expired",
          str(checks["collects_status_8"]))

    # 5. 夹内作品逐页直读 crawler 原始响应（复审项 3 的四个场景）
    #    5a. 多夹混合：空夹（合法）+ 有作品夹 + 下一页为空 —— 全部完成，空夹不中断
    async def fake_collects_two(self, params):
        return {"status_code": 0,
                "collects_list": [{"collects_id": "FAKEFOLDERID1", "collects_name": "空夹"},
                                  {"collects_id": "FAKEFOLDERID2", "collects_name": "有货"}],
                "has_more": 0}

    folder_page_state = {"n": 0}

    async def fake_folder_pages_mixed(self, params):
        # FAKEFOLDERID1：合法空夹（aweme_list=[]，has_more=0）
        if params.collects_id == "FAKEFOLDERID1":
            return {"status_code": 0, "aweme_list": [], "has_more": 0}
        # FAKEFOLDERID2：第一页 2 条 has_more=1，第二页空 has_more=0（下一页为空）
        folder_page_state["n"] += 1
        if folder_page_state["n"] == 1:
            return {"status_code": 0, "aweme_list": [_aweme(1), _aweme(2)], "has_more": 1, "cursor": 2}
        return {"status_code": 0, "aweme_list": [], "has_more": 0}

    DouyinCrawler.fetch_user_collects = fake_collects_two
    DouyinCrawler.fetch_user_collects_video = fake_folder_pages_mixed
    result = {}
    warn5: list[str] = []
    await fetch.fetch_collections_scope(handler, args, result, warn5)
    block = result["collects"]
    check("folder_empty_is_legit_and_mixed_complete",
          block["complete"] is True
          and block["folder_items"]["FAKEFOLDERID1"]["items"] == 0
          and block["folder_items"]["FAKEFOLDERID1"]["complete"] is True
          and block["folder_items"]["FAKEFOLDERID2"]["items"] == 2
          and block["folder_incomplete"] == [],
          f"complete={block['complete']} items={ {k: v['items'] for k, v in block['folder_items'].items()} }")
    check("folder_labels_no_real_ids",
          all("FAKEFOLDERID" not in w for w in warn5)
          and all("FAKEFOLDERID" not in i for i in block["folder_incomplete"]),
          f"warnings={warn5} incomplete={block['folder_incomplete']}")

    #    5b. 夹内 status_code=8 → login_expired（此前经生成器无法到达该分支）
    async def fake_folder_status8(self, params):
        return {"status_code": 8, "aweme_list": [], "has_more": 0}

    DouyinCrawler.fetch_user_collects_video = fake_folder_status8
    result = {}
    try:
        await fetch.fetch_collections_scope(handler, args, result, [])
        checks["folder_status_8"] = "NO_ERROR_BAD"
    except fetch.ScopeError as exc:
        checks["folder_status_8"] = exc.kind
    check("folder_status_8", checks["folder_status_8"] == "login_expired",
          str(checks["folder_status_8"]))

    #    5c. 夹内响应缺 aweme_list → 单夹失败被隔离，其他夹/整体如实不完整
    async def fake_folder_no_list(self, params):
        return {"status_code": 0, "has_more": 0}

    DouyinCrawler.fetch_user_collects_video = fake_folder_no_list
    result = {}
    try:
        await fetch.fetch_collections_scope(handler, args, result, [])
        checks["folder_missing_aweme_list"] = "isolated"
    except fetch.ScopeError as exc:
        checks["folder_missing_aweme_list"] = exc.kind
    block = result.get("collects") or {}
    check("folder_missing_aweme_list",
          checks["folder_missing_aweme_list"] == "isolated"
          and block.get("complete") is False
          and sorted(block.get("folder_incomplete") or []) == ["folder#1", "folder#2"]
          and all((block.get("folder_items") or {}).get(fid, {}).get("failed") is True
                  for fid in ("FAKEFOLDERID1", "FAKEFOLDERID2"))
          and all("works" not in (block.get("folder_items") or {}).get(fid, {})
                  for fid in ("FAKEFOLDERID1", "FAKEFOLDERID2")),
          str(block)[:200])

    #    5d. 多页夹成功 + 另一夹缺 aweme_list：成功夹结果保留，失败夹明确标记
    folder_page_state = {"n": 0}

    async def fake_folder_mixed_failure(self, params):
        if params.collects_id == "FAKEFOLDERID2":
            return {"status_code": 0, "has_more": 0}  # 缺 aweme_list → 单夹失败
        folder_page_state["n"] += 1
        if folder_page_state["n"] == 1:
            return {"status_code": 0, "aweme_list": [_aweme(1), _aweme(2)],
                    "has_more": 1, "cursor": 10}
        return {"status_code": 0, "aweme_list": [_aweme(3)], "has_more": 0}

    DouyinCrawler.fetch_user_collects = fake_collects_two
    DouyinCrawler.fetch_user_collects_video = fake_folder_mixed_failure
    result = {}
    warn5d: list[str] = []
    await fetch.fetch_collections_scope(handler, args, result, warn5d)
    block = result["collects"]
    check("folder_mixed_failure_preserves_good_folder",
          block["complete"] is False
          and block["folders_complete"] is True
          and block["folder_items"]["FAKEFOLDERID1"]["complete"] is True
          and block["folder_items"]["FAKEFOLDERID1"]["items"] == 3
          and [w["external_id"] for w in block["folder_items"]["FAKEFOLDERID1"]["works"]]
          == ["1", "2", "3"]
          and block["folder_items"]["FAKEFOLDERID2"]["failed"] is True
          and "works" not in block["folder_items"]["FAKEFOLDERID2"]
          and block["folder_incomplete"] == ["folder#2"]
          and block["folder_failures"] == [{"folder": "folder#2", "kind": "platform_error"}],
          str({k: v.get("items") for k, v in block["folder_items"].items()}))
    check("folder_mixed_failure_no_real_ids",
          all("FAKEFOLDERID" not in w for w in warn5d)
          and all("FAKEFOLDERID" not in str(row) for row in block["folder_failures"]),
          f"warnings={warn5d}")

    # 6. 真实 F2 异常类翻译 + 脱敏
    from f2.exceptions.api_exceptions import (
        APIRateLimitError,
        APIRetryExhaustedError,
        APIUnauthorizedError,
    )
    err = fetch.scope_error_from_exception(
        APIRetryExhaustedError("获取用户：MS4wLjABAAAsynthetic0000000000 点赞的作品失败 msToken=FAKE"), "like", "like")
    check("retry_exhausted_like", err.kind == "like_list_unavailable", err.kind)
    check("retry_message_no_identifiers",
          "MS4wLjABAAAA" not in err.args[0] and "FAKE" not in err.args[0], err.args[0])
    err = fetch.scope_error_from_exception(APIRetryExhaustedError("x"), "collection", "collection")
    check("retry_exhausted_other_scope", err.kind == "platform_error", err.kind)
    err = fetch.scope_error_from_exception(APIUnauthorizedError("x"), "like", "like")
    check("unauthorized", err.kind == "login_expired", err.kind)
    err = fetch.scope_error_from_exception(APIRateLimitError("x"), "like", "like")
    check("rate_limited", err.kind == "rate_limited", err.kind)

    # 7. 点赞缺 sec_user_id → 尝试用登录态自动确认；本检查全程断网，必须明确失败
    with tempfile.TemporaryDirectory(prefix="f2-runtime-check-") as tmp:
        cookie_file = Path(tmp) / "cookie.txt"
        cookie_file.write_text("sessionid=synthetic", encoding="utf-8")
        ns = type("Args", (), {})()
        ns.cookie_file = str(cookie_file)
        ns.output = str(Path(tmp) / "_out.json")
        ns.scopes = "like"
        ns.sec_user_id = ""
        ns.folder_ids = ""
        ns.page_counts = 20
        ns.max_items_per_list = 100
        ns.max_pages_per_list = 50
        ns.budget_seconds = 10
        ns.timeout = 1
        ns.max_retries = 1
        ns.lock_file = ""
        try:
            await fetch.run_scopes(ns)
            checks["like_auto_identity"] = "NO_ERROR_BAD"
        except fetch.ScopeError as exc:
            checks["like_auto_identity"] = exc.kind
    check("like_auto_identity", checks["like_auto_identity"] == fetch.SELF_IDENTITY_KIND,
          str(checks["like_auto_identity"]))

    # 8. detail 模式 × 真实 PostDetailFilter（网络桩）：P0 回归
    #    真实 Filter 暴露 api_status_code 而无 status_code 属性——run_detail 必须
    #    读原始响应，否则 AttributeError；图集缺行必须拒绝；身份不一致必须拒绝。
    from f2.apps.douyin.crawler import DouyinCrawler as _DC

    detail_payloads: dict = {"current": None}

    async def fake_post_detail(self, params):
        return detail_payloads["current"]

    _DC.fetch_post_detail = fake_post_detail

    def _detail_args(tmp: str) -> object:
        return _ns_with(cookie_file=str(Path(tmp) / "c.txt"), output=str(Path(tmp) / "o.json"))

    def _ns_with(**kwargs) -> object:
        base = {"mode": "detail", "cookie_file": "", "output": "", "aweme_id": "1234567890123456789",
                "timeout": 0, "max_retries": 1, "budget_seconds": 10, "lock_file": ""}
        base.update(kwargs)
        return type("Args", (), base)()

    def _payload(aweme_id: str, rows: list, status_code: int = 0, video: bool = False) -> dict:
        body = {"status_code": status_code, "aweme_detail": {
            "aweme_id": aweme_id, "author": {"nickname": "synthetic"}, "desc": "sample",
            "create_time": 1700000000, "images": rows}}
        if video:
            body["aweme_detail"]["video"] = {"play_addr": {"url_list": ["https://v.example.test/v.mp4"]}}
        return body

    with tempfile.TemporaryDirectory(prefix="f2-detail-check-") as tmp:
        cookie_file = Path(tmp) / "c.txt"
        cookie_file.write_text("sessionid=synthetic", encoding="utf-8")

        # 8a. 成功：完整图集 + 视频直链（identity 一致）
        detail_payloads["current"] = _payload("1234567890123456789",
                                              [{"url_list": ["https://i.example.test/1.jpg"]},
                                               {"url_list": ["https://i.example.test/2.jpg"]}], video=True)
        result = await asyncio.to_thread(fetch.run_detail, _ns_with(cookie_file=str(cookie_file), output=str(Path(tmp) / "a.json")))
        d = result.get("detail") or {}
        checks["detail_success"] = bool(result.get("ok")) and len(d.get("images", [])) == 2 \
            and d.get("video_url") == "https://v.example.test/v.mp4" \
            and d.get("external_id") == "1234567890123456789"
        check("detail_success", checks["detail_success"] is True, str(result)[:200])

        # 8b. 图集缺行（空 url_list）→ incomplete_album，不静默当完整
        detail_payloads["current"] = _payload("1234567890123456789",
                                              [{"url_list": ["https://i.example.test/1.jpg"]},
                                               {"url_list": []},
                                               {"url_list": ["https://i.example.test/3.jpg"]}])
        try:
            await asyncio.to_thread(fetch.run_detail, _ns_with(cookie_file=str(cookie_file), output=str(Path(tmp) / "b.json")))
            checks["detail_partial_album"] = "NO_ERROR_BAD"
        except fetch.ScopeError as exc:
            checks["detail_partial_album"] = exc.kind
        check("detail_partial_album", checks["detail_partial_album"] == "incomplete_album",
              str(checks["detail_partial_album"]))

        # 8c. 登录失效（status_code=8）→ login_expired
        detail_payloads["current"] = _payload("1234567890123456789",
                                              [{"url_list": ["https://i.example.test/1.jpg"]}], status_code=8)
        try:
            await asyncio.to_thread(fetch.run_detail, _ns_with(cookie_file=str(cookie_file), output=str(Path(tmp) / "c.json")))
            checks["detail_status_8"] = "NO_ERROR_BAD"
        except fetch.ScopeError as exc:
            checks["detail_status_8"] = exc.kind
        check("detail_status_8", checks["detail_status_8"] == "login_expired",
              str(checks["detail_status_8"]))

        # 8d. 身份不一致 → identity_mismatch
        detail_payloads["current"] = _payload("9999999999999999999",
                                              [{"url_list": ["https://i.example.test/1.jpg"]}])
        try:
            await asyncio.to_thread(fetch.run_detail, _ns_with(cookie_file=str(cookie_file), output=str(Path(tmp) / "d.json")))
            checks["detail_identity_mismatch"] = "NO_ERROR_BAD"
        except fetch.ScopeError as exc:
            checks["detail_identity_mismatch"] = exc.kind
        check("detail_identity_mismatch", checks["detail_identity_mismatch"] == "identity_mismatch",
              str(checks["detail_identity_mismatch"]))

    # 9. 列表接口的官方请求头与签名入参（真实 Crawler/Handler，零网络）
    official = fetch.official_list_headers()
    conf_ua = ClientConfManager.user_agent()
    conf_referer = ClientConfManager.referer()
    browser = ClientConfManager.brm_browser() if isinstance(ClientConfManager.brm_browser(), dict) else {}
    check("official_headers_from_conf",
          official.get("User-Agent") == conf_ua and official.get("Referer") == conf_referer,
          f"ua={official.get('User-Agent')!r}")
    check("official_referer_is_douyin",
          str(conf_referer).rstrip("/") == fetch._DOUYIN_REFERER_ORIGIN, str(conf_referer))
    token = fetch._UA_BROWSER_TOKENS.get(str(browser.get("name") or ""))
    check("official_ua_matches_model_browser",
          bool(token) and token in conf_ua and str(browser.get("version") or "") in conf_ua,
          f"name={browser.get('name')!r} version={browser.get('version')!r} ua={conf_ua!r}")
    official["x-synthetic-marker"] = "1"
    check("official_headers_copy_isolated",
          "x-synthetic-marker" not in ClientConfManager.headers())

    list_kwargs = {
        "headers": fetch.official_list_headers(),
        "cookie": "sessionid=synthetic",
        "timeout": 0,
        "max_retries": 1,
        "proxies": {"http://": None, "https://": None},
    }
    list_handler = DouyinHandler(list_kwargs)

    signed_calls: list[tuple] = []

    class SpyBogus:
        @classmethod
        def model_2_endpoint(cls, user_agent, base_endpoint, params, body=""):
            endpoint = ABogusManager.model_2_endpoint(user_agent, base_endpoint, params, body)
            signed_calls.append((user_agent, base_endpoint, dict(params), endpoint))
            return endpoint

    async def fake_get_json(endpoint):
        return {"status_code": 0, "aweme_list": [], "has_more": 0}

    async def fake_post_json(endpoint, json=None):
        return {"status_code": 0, "aweme_list": [], "has_more": 0}

    for name, method in _ORIGINAL_CRAWLER_METHODS.items():
        setattr(DouyinCrawler, name, method)

    async with DouyinCrawler(list_handler.kwargs) as crawler:
        ch = dict(crawler.crawler_headers)
        check("crawler_sends_official_ua", ch.get("User-Agent") == conf_ua, str(ch.get("User-Agent")))
        check("crawler_sends_referer", ch.get("Referer") == conf_referer, str(ch.get("Referer")))
        check("crawler_sends_argus", ch.get("x-tt-argus") == "1", str(ch.get("x-tt-argus")))
        check("crawler_sends_cookie", ch.get("Cookie") == "sessionid=synthetic", "cookie missing")
        check("crawler_keeps_proxies_and_timeout",
              crawler.proxies == {"http://": None, "https://": None} and crawler._timeout == 0,
              f"proxies={crawler.proxies} timeout={crawler._timeout}")
        crawler.bogus_manager = SpyBogus
        crawler._fetch_get_json = fake_get_json
        crawler._fetch_post_json = fake_post_json
        await crawler.fetch_user_like(UserLike(
            max_cursor=0, count=20, sec_user_id="MS4wLjABAAAsynthetic0000000000"))
        await crawler.fetch_user_collection(UserCollection(cursor=0, count=20))
        await crawler.fetch_user_collects(UserCollects(cursor=0, count=20))
        await crawler.fetch_user_collects_video(UserCollectsVideo(
            cursor=0, count=20, collects_id="FAKEFOLDERID1"))

    check("signed_calls_cover_four_lists", len(signed_calls) == 4, str(len(signed_calls)))
    check("signed_calls_use_official_ua",
          all(call[0] == conf_ua for call in signed_calls),
          str([call[0] for call in signed_calls]))
    check("signed_endpoints_have_abogus",
          all("a_bogus=" in call[3] for call in signed_calls),
          str([call[1] for call in signed_calls]))
    check("signed_endpoints_do_not_carry_cookie",
          all("sessionid=synthetic" not in call[3] for call in signed_calls))
    expected_param_subset = [
        {"max_cursor", "count", "sec_user_id"},
        {"cursor", "count"},
        {"cursor", "count"},
        {"cursor", "count", "collects_id"},
    ]
    check("signed_params_match_models",
          all(subset <= set(call[2])
              for subset, call in zip(expected_param_subset, signed_calls)),
          str([sorted(set(subset) - set(call[2]))
               for subset, call in zip(expected_param_subset, signed_calls)]))
    check("signed_browser_params_match_model",
          all(call[2].get("browser_name") == browser.get("name")
              and call[2].get("browser_platform") == browser.get("platform")
              for call in signed_calls),
          str([(call[2].get("browser_name"), call[2].get("browser_platform"))
               for call in signed_calls]))
    check("signed_folder_id_is_requested",
          signed_calls[3][2].get("collects_id") == "FAKEFOLDERID1"
          and signed_calls[0][2].get("sec_user_id") == "MS4wLjABAAAsynthetic0000000000")

    #    9a. 点赞：真实 handler 分页（网络桩）→ 每页 crawler 都用官方请求头
    captured_list_headers: list[dict] = []
    page_state = {"like": 0, "collection": 0, "collects": 0}

    async def fake_like_pages(self, params):
        captured_list_headers.append(dict(self.crawler_headers))
        page_state["like"] += 1
        if page_state["like"] == 1:
            return {"status_code": 0, "aweme_list": [_aweme(1), _aweme(2)], "has_more": 1,
                    "max_cursor": 10}
        return {"status_code": 0, "aweme_list": [_aweme(3)], "has_more": 0, "max_cursor": 20}

    async def fake_collection_pages(self, params):
        captured_list_headers.append(dict(self.crawler_headers))
        page_state["collection"] += 1
        if page_state["collection"] == 1:
            return {"status_code": 0, "aweme_list": [_aweme(4)], "has_more": 1, "cursor": 10}
        return {"status_code": 0, "aweme_list": [_aweme(5)], "has_more": 0, "cursor": 20}

    async def fake_collects_pages(self, params):
        captured_list_headers.append(dict(self.crawler_headers))
        page_state["collects"] += 1
        if page_state["collects"] == 1:
            return {"status_code": 0, "has_more": 1, "cursor": 10, "collects_list": [
                {"collects_id": "FAKEFOLDERID1", "collects_name": "one", "total_number": 0}]}
        return {"status_code": 0, "has_more": 0, "cursor": 20, "collects_list": [
            {"collects_id": "FAKEFOLDERID2", "collects_name": "two", "total_number": 0}]}

    with tempfile.TemporaryDirectory(prefix="f2-list-headers-") as tmp:
        cookie_file = Path(tmp) / "cookie.txt"
        cookie_file.write_text("sessionid=synthetic", encoding="utf-8")

        def _list_ns(scopes: str) -> object:
            return type("Args", (), {
                "cookie_file": str(cookie_file), "output": str(Path(tmp) / "_out.json"),
                "scopes": scopes, "sec_user_id": "MS4wLjABAAAsynthetic0000000000",
                "folder_ids": "NOT-IN-RESPONSE", "page_counts": 20, "max_items_per_list": 100,
                "max_pages_per_list": 50, "budget_seconds": 30, "timeout": 0,
                "max_retries": 1, "lock_file": "",
            })()

        DouyinCrawler.fetch_user_like = fake_like_pages
        result = await fetch.run_scopes(_list_ns("like"))
        check("like_pagination_complete",
              result.get("like", {}).get("complete") is True
              and [w["external_id"] for w in result["like"]["works"]] == ["1", "2", "3"]
              and result["like"]["pages"] == 2,
              str(result.get("like", {}).get("pages")))

        DouyinCrawler.fetch_user_collection = fake_collection_pages
        result = await fetch.run_scopes(_list_ns("collection"))
        check("collection_pagination_complete",
              result.get("collection", {}).get("complete") is True
              and [w["external_id"] for w in result["collection"]["works"]] == ["4", "5"]
              and result["collection"]["pages"] == 2,
              str(result.get("collection", {}).get("pages")))

        DouyinCrawler.fetch_user_collects = fake_collects_pages
        result = {}
        await fetch.fetch_collections_scope(list_handler, _list_ns("collections"), result, [])
        block = result["collects"]
        check("collects_pagination_complete",
              block["pages"] == 2 and block["folders_complete"] is True
              and {f["folder_id"] for f in block["folders"]} == {"FAKEFOLDERID1", "FAKEFOLDERID2"},
              str(block.get("pages")))

        #    9b. 列表请求失败仍按明确 kind 上报（status_code=8 / 缺 aweme_list）
        async def fake_like_status8(self, params):
            return {"status_code": 8, "aweme_list": [], "has_more": 0}

        DouyinCrawler.fetch_user_like = fake_like_status8
        try:
            await fetch.run_scopes(_list_ns("like"))
            checks["like_status_8"] = "NO_ERROR_BAD"
        except fetch.ScopeError as exc:
            checks["like_status_8"] = exc.kind
        check("like_status_8", checks["like_status_8"] == "login_expired", str(checks["like_status_8"]))

        async def fake_collection_missing_list(self, params):
            return {"status_code": 0, "has_more": 0}

        DouyinCrawler.fetch_user_collection = fake_collection_missing_list
        try:
            await fetch.run_scopes(_list_ns("collection"))
            checks["collection_missing_list"] = "NO_ERROR_BAD"
        except fetch.ScopeError as exc:
            checks["collection_missing_list"] = exc.kind
        check("collection_missing_list", checks["collection_missing_list"] == "platform_error",
              str(checks["collection_missing_list"]))

    check("list_crawlers_used_official_headers",
          captured_list_headers and all(
              h.get("User-Agent") == conf_ua and h.get("Referer") == conf_referer
              and h.get("x-tt-argus") == "1" for h in captured_list_headers),
          f"n={len(captured_list_headers)}")
    check("list_crawlers_never_use_mac_ua",
          all(h.get("User-Agent") != fetch._USER_AGENT for h in captured_list_headers))

    # 10. 点赞缺少显式本人标识：自动识别（真实请求模型/签名/crawler 语义，网络桩）
    _orig_get_json = DouyinCrawler._fetch_get_json
    _orig_model_2 = ABogusManager.__dict__["model_2_endpoint"]
    _orig_model_2_func = _orig_model_2.__func__
    self_signed: list[tuple] = []

    def spy_model_2(user_agent, base_endpoint, params, body=""):
        endpoint = _orig_model_2_func(ABogusManager, user_agent, base_endpoint, params, body)
        self_signed.append((user_agent, base_endpoint, dict(params), endpoint))
        return endpoint

    ABogusManager.model_2_endpoint = staticmethod(spy_model_2)

    profile_payload = {"current": None}

    async def fake_self_profile(self, endpoint):
        return profile_payload["current"]

    DouyinCrawler._fetch_get_json = fake_self_profile

    auto_like_ids: list[str] = []

    async def fake_auto_like(self, params):
        auto_like_ids.append(params.sec_user_id)
        return {"status_code": 0, "aweme_list": [_aweme(7)], "has_more": 0}

    async def fake_any_collection(self, params):
        return {"status_code": 0, "aweme_list": [_aweme(8)], "has_more": 0}

    try:
        with tempfile.TemporaryDirectory(prefix="f2-self-identity-") as tmp:
            cookie_file = Path(tmp) / "cookie.txt"
            cookie_file.write_text("sessionid=synthetic", encoding="utf-8")

            def _auto_args(scopes: str, sec_user_id: str = "") -> object:
                return type("Args", (), {
                    "cookie_file": str(cookie_file), "output": str(Path(tmp) / "_o.json"),
                    "scopes": scopes, "sec_user_id": sec_user_id,
                    "folder_ids": "NOT-IN-RESPONSE", "page_counts": 20,
                    "max_items_per_list": 100, "max_pages_per_list": 50,
                    "budget_seconds": 30, "timeout": 0, "max_retries": 1, "lock_file": "",
                })()

            # 10a 成功：资料返回本人标识 → 点赞使用该标识
            profile_payload["current"] = {"status_code": 0, "user": {"sec_uid": _SYNTH_SELF_ID}}
            DouyinCrawler.fetch_user_like = fake_auto_like
            result = await fetch.run_scopes(_auto_args("like"))
            auto_like_ok = (result.get("like", {}).get("complete") is True
                            and auto_like_ids == [_SYNTH_SELF_ID])
            checks["auto_identity_like_complete"] = auto_like_ok
            check("auto_identity_like_complete", auto_like_ok, str(auto_like_ids))
            check("auto_identity_signature",
                  len(self_signed) == 1
                  and self_signed[0][0] == conf_ua
                  and self_signed[0][1] == fetch._PROFILE_SELF_ENDPOINT
                  and "a_bogus=" in self_signed[0][3],
                  str(self_signed[:1])[:200])
            check("auto_identity_params_match_model",
                  self_signed[0][2].get("browser_name") == browser.get("name")
                  and self_signed[0][2].get("browser_platform") == browser.get("platform")
                  and isinstance(self_signed[0][2].get("msToken"), str),
                  str(sorted(self_signed[0][2])[:6]))
            check("auto_identity_not_in_output",
                  _SYNTH_SELF_ID not in json.dumps(result, ensure_ascii=False))

            # 10b 显式标识优先：不请求本人资料
            self_signed.clear()
            auto_like_ids.clear()
            result = await fetch.run_scopes(_auto_args("like", _SYNTH_MANUAL_ID))
            explicit_ok = self_signed == [] and auto_like_ids == [_SYNTH_MANUAL_ID]
            checks["explicit_identity_skips_profile"] = explicit_ok
            check("explicit_identity_skips_profile", explicit_ok, str(auto_like_ids))

            # 10c 未请求点赞：不请求本人资料
            self_signed.clear()
            DouyinCrawler.fetch_user_collection = fake_any_collection
            result = await fetch.run_scopes(_auto_args("collection"))
            check("no_like_no_profile_request",
                  self_signed == [] and result.get("collection", {}).get("complete") is True,
                  str(self_signed))

            # 10d 安全失败：无 user / 缺成功状态 / status 8 / 无效 ID；不得请求点赞
            for label, payload, expected in (
                ("auto_identity_missing_user", {"status_code": 0}, fetch.SELF_IDENTITY_KIND),
                ("auto_identity_missing_status", {"user": {"sec_uid": _SYNTH_SELF_ID}},
                 fetch.SELF_IDENTITY_KIND),
                ("auto_identity_login_expired", {"status_code": 8}, "login_expired"),
                ("auto_identity_invalid_id", {"status_code": 0, "user": {"sec_uid": "short"}},
                 fetch.SELF_IDENTITY_KIND),
            ):
                self_signed.clear()
                auto_like_ids.clear()
                profile_payload["current"] = payload
                try:
                    await fetch.run_scopes(_auto_args("like"))
                    checks[label] = "NO_ERROR_BAD"
                except fetch.ScopeError as exc:
                    checks[label] = exc.kind
                    check(f"{label}_no_leak",
                          _SYNTH_SELF_ID not in str(exc) and "https://" not in str(exc),
                          str(exc))
                check(label, checks[label] == expected, str(checks[label]))
                check(f"{label}_no_like_request", auto_like_ids == [], str(auto_like_ids))
    finally:
        DouyinCrawler.fetch_user_like = _ORIGINAL_CRAWLER_METHODS["fetch_user_like"]
        DouyinCrawler.fetch_user_collection = _ORIGINAL_CRAWLER_METHODS["fetch_user_collection"]
        DouyinCrawler._fetch_get_json = _orig_get_json
        ABogusManager.model_2_endpoint = _orig_model_2

    # 11. 3xx 拒绝跟随：本人资料 + 点赞(GET)/平铺收藏(POST)/收藏夹/夹内作品(GET)
    leaked: list[dict] = []
    transport_choice: dict = {"factory": None}

    def _redirect_all(request):
        if request.url.host == "www.douyin.com":
            return httpx.Response(302, headers={"Location": "https://outside.invalid/redirected"})
        leaked.append({"host": request.url.host, "uifid": request.headers.get("uifid")})
        return httpx.Response(200, json={})

    def _collects_ok_folder_redirect(request):
        if request.url.host != "www.douyin.com":
            leaked.append({"host": request.url.host, "uifid": request.headers.get("uifid")})
            return httpx.Response(200, json={})
        if request.url.path.endswith("/collects/video/list/"):
            return httpx.Response(302, headers={"Location": "https://outside.invalid/redirected"})
        return httpx.Response(200, json={
            "status_code": 0, "has_more": 0, "cursor": 0,
            "collects_list": [{"collects_id": "FAKEFOLDERID1", "collects_name": "one",
                               "total_number": 1}]})

    original_init = DouyinCrawler.__init__

    def redirect_init(self, kwargs=None):
        original_init(self, kwargs)
        self._aclient = httpx.AsyncClient(
            transport=httpx.MockTransport(transport_choice["factory"]))

    DouyinCrawler.__init__ = redirect_init
    try:
        synthetic_cookie = "sessionid=SECRET_TEST_SESSION; UIFID=SECRET_TEST_UIFID"
        redirect_handler = DouyinHandler({
            "headers": fetch.official_list_headers(), "cookie": synthetic_cookie,
            "timeout": 0, "max_retries": 1,
            "proxies": {"http://": None, "https://": None}})
        transport_choice["factory"] = _redirect_all
        try:
            await fetch.resolve_self_sec_user_id(redirect_handler)
            checks["redirect_self_profile"] = "NO_ERROR_BAD"
        except fetch.ScopeError as exc:
            checks["redirect_self_profile"] = exc.kind
        check("redirect_self_profile", checks["redirect_self_profile"] == "redirect_refused",
              str(checks["redirect_self_profile"]))

        with tempfile.TemporaryDirectory(prefix="f2-redirect-") as tmp:
            cookie_file = Path(tmp) / "cookie.txt"
            cookie_file.write_text(synthetic_cookie, encoding="utf-8")

            def _redirect_args(scope: str) -> object:
                return type("Args", (), {
                    "cookie_file": str(cookie_file), "output": str(Path(tmp) / "_o.json"),
                    "scopes": scope, "sec_user_id": _SYNTH_MANUAL_ID,
                    "folder_ids": "", "page_counts": 1, "max_items_per_list": 5,
                    "max_pages_per_list": 1, "budget_seconds": 10, "timeout": 0,
                    "max_retries": 1, "lock_file": ""})()

            for scope in ("like", "collection"):
                try:
                    await fetch.run_scopes(_redirect_args(scope))
                    checks[f"redirect_{scope}"] = "NO_ERROR_BAD"
                except fetch.ScopeError as exc:
                    checks[f"redirect_{scope}"] = exc.kind
                check(f"redirect_{scope}", checks[f"redirect_{scope}"] == "redirect_refused",
                      str(checks[f"redirect_{scope}"]))

            # 收藏夹列表成功、夹内作品返回 302：夹内 GET 也必须拒绝跟随
            transport_choice["factory"] = _collects_ok_folder_redirect
            try:
                await fetch.run_scopes(_redirect_args("collections"))
                checks["redirect_folder_items"] = "NO_ERROR_BAD"
            except fetch.ScopeError as exc:
                checks["redirect_folder_items"] = exc.kind
            check("redirect_folder_items", checks["redirect_folder_items"] == "redirect_refused",
                  str(checks["redirect_folder_items"]))
    finally:
        DouyinCrawler.__init__ = original_init

    check("redirect_no_external_requests", leaked == [], str(leaked[:2]))

    # 12. 详情 3xx 拒绝跟随（真实 PostDetail 请求链路），并确认全局替换已恢复
    detail_leaked: list[str] = []

    def detail_redirect_transport(request):
        if request.url.host == "www.douyin.com":
            return httpx.Response(302, headers={"Location": "https://outside.invalid/redirected"})
        detail_leaked.append(request.url.host)
        return httpx.Response(200, json={"status_code": 0})

    saved_init = DouyinCrawler.__init__

    def detail_init(self, kwargs=None):
        saved_init(self, kwargs)
        self._aclient = httpx.AsyncClient(
            transport=httpx.MockTransport(detail_redirect_transport))

    handler_module = sys.modules["f2.apps.douyin.handler"]
    original_handler_crawler = handler_module.DouyinCrawler
    DouyinCrawler.__init__ = detail_init
    try:
        with tempfile.TemporaryDirectory(prefix="f2-detail-redirect-") as tmp:
            cookie_file = Path(tmp) / "cookie.txt"
            cookie_file.write_text(
                "sessionid=SECRET_TEST_SESSION; UIFID=SECRET_TEST_UIFID", encoding="utf-8")
            detail_args = type("Args", (), {
                "mode": "detail", "cookie_file": str(cookie_file),
                "output": str(Path(tmp) / "o.json"), "aweme_id": "1234567890123456789",
                "timeout": 0, "max_retries": 1, "budget_seconds": 10, "lock_file": ""})()
            try:
                await asyncio.to_thread(fetch.run_detail, detail_args)
                checks["detail_redirect"] = "NO_ERROR_BAD"
            except fetch.ScopeError as exc:
                checks["detail_redirect"] = exc.kind
                check("detail_redirect_no_leak",
                      "outside.invalid" not in str(exc) and "SECRET_TEST_UIFID" not in str(exc),
                      str(exc))
    finally:
        DouyinCrawler.__init__ = saved_init

    check("detail_redirect", checks["detail_redirect"] == "redirect_refused",
          str(checks["detail_redirect"]))
    check("detail_redirect_no_external", detail_leaked == [], str(detail_leaked[:2]))
    restored = handler_module.DouyinCrawler is original_handler_crawler
    checks["detail_restores_handler_crawler"] = restored
    check("detail_restores_handler_crawler", restored, str(handler_module.DouyinCrawler))

    # 13. 连续 403：有限连续失败后停止后续夹请求，不误判为登录过期/限流
    DouyinCrawler.fetch_user_collects = _ORIGINAL_CRAWLER_METHODS["fetch_user_collects"]
    folder_request_paths: list[str] = []

    def forbidden_transport(request):
        if request.url.path.endswith("/collects/list/"):
            return httpx.Response(200, json={
                "status_code": 0, "has_more": 0, "cursor": 0,
                "collects_list": [
                    {"collects_id": "FAKEFOLDERID1", "collects_name": "一", "total_number": 1},
                    {"collects_id": "FAKEFOLDERID2", "collects_name": "二", "total_number": 1},
                    {"collects_id": "FAKEFOLDERID3", "collects_name": "三", "total_number": 1}]})
        folder_request_paths.append(request.url.path)
        return httpx.Response(403, text="synthetic forbidden")

    saved_init_403 = DouyinCrawler.__init__

    def forbidden_init(self, kwargs=None):
        saved_init_403(self, kwargs)
        self._aclient = httpx.AsyncClient(
            transport=httpx.MockTransport(forbidden_transport))

    DouyinCrawler.__init__ = forbidden_init
    try:
        forbidden_handler = DouyinHandler({
            "headers": fetch.official_list_headers(),
            "cookie": "sessionid=SECRET_TEST_SESSION; UIFID=SECRET_TEST_UIFID",
            "timeout": 0, "max_retries": 1,
            "proxies": {"http://": None, "https://": None}})
        result = {}
        warn13: list[str] = []
        await fetch.fetch_collections_scope(forbidden_handler, args, result, warn13)
    finally:
        DouyinCrawler.__init__ = saved_init_403

    block = result.get("collects") or {}
    kinds = [row.get("kind") for row in block.get("folder_failures") or []]
    check("consecutive_403_stops_after_limit",
          len(folder_request_paths) == fetch._MAX_CONSECUTIVE_FOLDER_FAILURES
          and block.get("stopped_after_consecutive_failures") is True
          and block.get("complete") is False
          and block.get("folder_incomplete") == ["folder#1", "folder#2", "folder#3"]
          and kinds == ["platform_error", "platform_error", "consecutive_failures"],
          f"requests={len(folder_request_paths)} kinds={kinds}")
    check("consecutive_403_not_misclassified",
          all(kind not in {"login_expired", "rate_limited"} for kind in kinds)
          and all("FAKEFOLDERID" not in w for w in warn13),
          f"warnings={warn13}")
    checks["consecutive_403_requests"] = len(folder_request_paths)
    checks["consecutive_403_stopped"] = block.get("stopped_after_consecutive_failures") is True

    # 14. 跨运行续传（真实 handler/crawler 链路，提交协议）：多页点赞与多夹在
    #     多次短预算内覆盖。取数进程不写进度文件，每次运行之间由"父进程确认步"
    #     （write_confirmed_progress，对应业务数据已持久提交）推进/清除状态。
    saved_resume_methods = {
        name: getattr(DouyinCrawler, name)
        for name in ("fetch_user_like", "fetch_user_collects", "fetch_user_collects_video")
    }
    resume_requests = {"like": [], "folders": []}

    async def fake_like_resume(self, params):
        resume_requests["like"].append(int(params.max_cursor))
        if int(params.max_cursor) == 0:
            return {"status_code": 0, "aweme_list": [_aweme(1)],
                    "has_more": 1, "max_cursor": 10}
        return {"status_code": 0, "aweme_list": [_aweme(2)],
                "has_more": 0, "max_cursor": 20}

    async def fake_collects_resume(self, params):
        return {"status_code": 0, "has_more": 0, "cursor": 0, "collects_list": [
            {"collects_id": f"FAKEFOLDERID{i}", "collects_name": str(i), "total_number": 1}
            for i in (1, 2, 3)]}

    async def fake_folder_resume(self, params):
        resume_requests["folders"].append(params.collects_id)
        return {"status_code": 0, "aweme_list": [_aweme(9)],
                "has_more": 0, "cursor": 0}

    DouyinCrawler.fetch_user_like = fake_like_resume
    DouyinCrawler.fetch_user_collects = fake_collects_resume
    DouyinCrawler.fetch_user_collects_video = fake_folder_resume
    try:
        with tempfile.TemporaryDirectory(prefix="f2-resume-") as tmp:
            cookie_file = Path(tmp) / "cookie.txt"
            cookie_file.write_text("sessionid=synthetic", encoding="utf-8")
            progress_file = Path(tmp) / "progress.json"

            def _resume_args(scope: str) -> object:
                return type("Args", (), {
                    "cookie_file": str(cookie_file), "output": str(Path(tmp) / "o.json"),
                    "scopes": scope, "sec_user_id": _SYNTH_MANUAL_ID, "folder_ids": "",
                    "page_counts": 1, "max_pages_per_list": 50, "max_items_per_list": 100,
                    "budget_seconds": 1, "timeout": 2, "max_retries": 1,
                    "lock_file": "", "progress_file": str(progress_file)})()

            def _confirm(result: dict) -> None:
                assert fetch.write_confirmed_progress(str(progress_file), result["resume"])

            # 点赞两页：首次只够第一页；取数后文件必须仍不存在（提交前不推进游标），
            # 父进程确认后状态落盘，第二次从已确认游标续传
            like_first = await asyncio.to_thread(fetch._run_list_mode, _resume_args("like"))
            check("resume_like_first_partial",
                  like_first["like"]["complete"] is False
                  and like_first["like"]["budget_exhausted"] is True
                  and resume_requests["like"] == [0]
                  and not progress_file.exists(),
                  f"cursors={resume_requests['like']} file={progress_file.exists()}")
            _confirm(like_first)
            check("resume_like_confirmed_cursor",
                  json.loads(progress_file.read_text(encoding="utf-8"))
                  ["scopes"]["like"]["cursor"] == 10,
                  progress_file.read_text(encoding="utf-8")[:120])
            like_second = await asyncio.to_thread(fetch._run_list_mode, _resume_args("like"))
            check("resume_like_second_complete",
                  like_second["like"]["complete"] is True
                  and [w["external_id"] for w in like_second["like"]["works"]] == ["2"]
                  and resume_requests["like"] == [0, 10],
                  f"cursors={resume_requests['like']}")
            _confirm(like_second)
            check("resume_like_cleared_after_confirm", not progress_file.exists(),
                  str(progress_file.exists()))

            # 收藏夹三夹：每次短预算推进一个夹（确认步在两次运行之间），直到完整
            resume_requests["folders"].clear()
            first = await asyncio.to_thread(fetch._run_list_mode, _resume_args("collections"))
            check("resume_folder_run1",
                  resume_requests["folders"] == ["FAKEFOLDERID1"]
                  and first["partial"] is True
                  and first["collects"]["folder_incomplete"] == ["folder#2", "folder#3"],
                  str(resume_requests["folders"]))
            _confirm(first)
            second = await asyncio.to_thread(fetch._run_list_mode, _resume_args("collections"))
            check("resume_folder_run2",
                  resume_requests["folders"] == ["FAKEFOLDERID1", "FAKEFOLDERID2"]
                  and second["collects"]["folder_items"]["FAKEFOLDERID1"].get("resumed") is True
                  and second["collects"]["folder_incomplete"] == ["folder#3"],
                  str(resume_requests["folders"]))
            _confirm(second)
            third = await asyncio.to_thread(fetch._run_list_mode, _resume_args("collections"))
            check("resume_folder_run3_complete",
                  resume_requests["folders"] == [
                      "FAKEFOLDERID1", "FAKEFOLDERID2", "FAKEFOLDERID3"]
                  and third["collects"]["complete"] is True,
                  str(resume_requests["folders"]))
            _confirm(third)
            check("resume_folder_cleared_after_confirm", not progress_file.exists(),
                  str(progress_file.exists()))
            checks["resume_completed_in_three_runs"] = (
                resume_requests["folders"] == [
                    "FAKEFOLDERID1", "FAKEFOLDERID2", "FAKEFOLDERID3"])
    finally:
        for name, method in saved_resume_methods.items():
            setattr(DouyinCrawler, name, method)

    return {"ok": not failures, "checks": checks, "failures": failures}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), ensure_ascii=False))
