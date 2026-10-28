"""真实 F2 运行时回归检查（在 tools/f2-runtime/.venv 内由 pytest 以子进程运行）。

用**真实 F2 Filter 与 handler 生成器**（网络打桩）验证消费层语义，避免只用
属性语义不同的假页面：
- 真实 UserPostFilter 把缺失 has_more 强转 False → 必须看原始响应键；
- 真实生成器上游提前结束（yield 前异常被吞）→ 0 页必须显式失败；
- 平台 status_code=8 → login_expired；
- 夹级/整体不完整状态如实传播；
- 真实 F2 异常类的翻译与消息脱敏。

输出 JSON：{"ok": true/false, "checks": {...}, "failures": [...]}
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE_ROOT))

import monitor.platforms.f2_fetch as fetch  # noqa: E402
from f2.apps.douyin.crawler import DouyinCrawler  # noqa: E402
from f2.apps.douyin.filter import UserPostFilter  # noqa: E402
from f2.apps.douyin.handler import DouyinHandler, rich_console  # noqa: E402

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

    # 4. 真实 handler 生成器（网络桩）：收藏夹 status_code=8 → login_expired
    handler = DouyinHandler({
        "headers": {"User-Agent": "UA"}, "cookie": "a=b", "timeout": 0, "max_retries": 1,
        "proxies": {"http://": None, "https://": None},
    })

    async def fake_collects_login(self, params):
        return {"status_code": 8, "collects_list": [], "has_more": 0}

    DouyinCrawler.fetch_user_collects = fake_collects_login
    args = type("Args", (), {"page_counts": 20, "max_items_per_list": 100,
                             "max_pages_per_list": 50, "folder_ids": ""})()
    result: dict = {}
    try:
        await fetch.fetch_collections_scope(handler, args, result, [])
        checks["collects_status_8"] = "NO_ERROR_BAD"
    except fetch.ScopeError as exc:
        checks["collects_status_8"] = exc.kind
    check("collects_status_8", checks["collects_status_8"] == "login_expired",
          str(checks["collects_status_8"]))

    # 5. 真实 handler 生成器：正常路径（1 夹 5 作品，完整结束）
    async def fake_collects_ok(self, params):
        return {"status_code": 0, "collects_list": [{"collects_id": "f1", "collects_name": "夹A"}], "has_more": 0}

    async def fake_folder_ok(self, params):
        return {"status_code": 0, "aweme_list": [_aweme(i) for i in range(5)], "has_more": 0}

    DouyinCrawler.fetch_user_collects = fake_collects_ok
    DouyinCrawler.fetch_user_collects_video = fake_folder_ok
    result = {}
    await fetch.fetch_collections_scope(handler, args, result, [])
    block = result["collects"]
    check("collects_happy_path", block["complete"] is True and block["folders_complete"] is True
          and block["folder_incomplete"] == [] and block["folder_items"]["f1"]["items"] == 5,
          f"block={ {k: v for k, v in block.items() if k != 'folder_items'} }")

    # 6. 真实 handler 生成器：夹内作品分页一直 has_more=1 → 页数上限 → 不完整传播
    async def fake_folder_endless(self, params):
        return {"status_code": 0, "aweme_list": [_aweme(1)], "has_more": 1}

    DouyinCrawler.fetch_user_collects_video = fake_folder_endless
    capped_args = type("Args", (), {"page_counts": 20, "max_items_per_list": 100,
                                    "max_pages_per_list": 2, "folder_ids": ""})()
    result = {}
    warn2: list[str] = []
    await fetch.fetch_collections_scope(handler, capped_args, result, warn2)
    block = result["collects"]
    check("collects_incomplete_propagated", block["complete"] is False
          and "folder:f1" in block["folder_incomplete"] and block["folders_complete"] is True,
          f"complete={block['complete']} incomplete={block['folder_incomplete']}")

    # 7. 真实 F2 异常类翻译 + 脱敏
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

    # 8. 点赞范围缺 sec_user_id → 提前明确报错（不联网）
    cookie_file = Path(WORKSPACE_ROOT) / "workbuddy" / "f2" / "_synthetic_cookie_check.txt"
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    cookie_file.write_text("sessionid=synthetic", encoding="utf-8")
    ns = type("Args", (), {})()
    ns.cookie_file = str(cookie_file)
    ns.output = str(cookie_file.parent / "_out.json")
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
    cookie_file.unlink(missing_ok=True)

    return {"ok": not failures, "checks": checks, "failures": failures}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), ensure_ascii=False))
