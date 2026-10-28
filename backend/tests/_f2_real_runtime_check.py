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
from f2.apps.douyin.utils import TokenManager  # noqa: E402

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
        "headers": {"User-Agent": "UA"}, "cookie": "a=b", "timeout": 0, "max_retries": 1,
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

    #    5c. 夹内响应缺 aweme_list → platform_error
    async def fake_folder_no_list(self, params):
        return {"status_code": 0, "has_more": 0}

    DouyinCrawler.fetch_user_collects_video = fake_folder_no_list
    result = {}
    try:
        await fetch.fetch_collections_scope(handler, args, result, [])
        checks["folder_missing_aweme_list"] = "NO_ERROR_BAD"
    except fetch.ScopeError as exc:
        checks["folder_missing_aweme_list"] = exc.kind
    check("folder_missing_aweme_list", checks["folder_missing_aweme_list"] == "platform_error",
          str(checks["folder_missing_aweme_list"]))

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

    # 7. 点赞范围缺 sec_user_id → 提前明确报错（不联网；cookie 写入 tempfile）
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
            checks["like_missing_sec_user_id"] = "NO_ERROR_BAD"
        except fetch.ScopeError as exc:
            checks["like_missing_sec_user_id"] = exc.kind
    check("like_missing_sec_user_id", checks["like_missing_sec_user_id"] == "missing_sec_user_id",
          str(checks["like_missing_sec_user_id"]))

    return {"ok": not failures, "checks": checks, "failures": failures}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), ensure_ascii=False))
