"""抖音UP主全量作品拉取（F2 隔离运行时 + 登录cookie + 断点续传）。

数据落 data/douyin/tanchengyi_822069263606973/：profile.json、pages/page_NNN.json
（每页原始 aweme_list 全量字段，含统计/话题/合集/置顶）。

用法：f2-runtime/.venv/bin/python -I scripts/dy_fetch_all.py [profile|posts|all]
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I 隔离模式下脚本目录被剔除，需手动补回
from tan_paths import DATA_ROOT, WORKBENCH_ROOT  # noqa: E402

F2_ROOT = WORKBENCH_ROOT / "tools" / "f2-runtime" / ".venv"  # 保留：f2 运行时定位参考
COOKIE = (WORKBENCH_ROOT / "config" / "f2" / "douyin_cookie.txt").read_text().strip()

SEC_UID = "MS4wLjABAAAArfpAmbNNdODGpzfznrZAXrt2JgCGFWxE25eFkQDGXOY"  # 谭成义
UID = "822069263606973"
DATA_DIR = DATA_ROOT / "data" / "douyin" / "tanchengyi_822069263606973"
PAGES_DIR = DATA_DIR / "pages"

PAGE_COUNTS = 20
MAX_PAGE_RETRIES = 5


def silence_f2_logs() -> None:
    try:
        from loguru import logger
        logger.remove()
    except Exception:
        pass


def build_kwargs() -> dict:
    from f2.apps.douyin.utils import ClientConfManager
    return {
        "headers": dict(ClientConfManager.headers()),
        "cookie": COOKIE,
        "timeout": 12,
        "max_retries": 3,
        "proxies": {"http://": None, "https://": None},
    }


async def phase_profile() -> None:
    from f2.apps.douyin.handler import DouyinHandler
    h = DouyinHandler(build_kwargs())
    prof = await h.fetch_user_profile(sec_user_id=SEC_UID)
    out = {"fetched_at": int(time.time()), "raw": prof._data}
    (DATA_DIR / "profile.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    u = prof._data.get("user") or {}
    print(f"profile: {u.get('nickname')} fans={u.get('follower_count')} aweme_count={u.get('aweme_count')}")


async def phase_posts() -> int:
    from f2.apps.douyin.handler import DouyinHandler
    from f2.exceptions.api_exceptions import APIError
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    h = DouyinHandler(build_kwargs())

    # 断点续传：从最后已存页的 max_cursor 继续
    existing = sorted(PAGES_DIR.glob("page_*.json"))
    cursor = 0
    pn = 1
    if existing:
        last = json.loads(existing[-1].read_text())
        if last.get("has_more") == 0:
            print("all pages already fetched")
            return sum(p["items"] for p in (json.loads(p.read_text()) for p in existing))
        cursor = last["max_cursor"]
        pn = len(existing) + 1
        print(f"resume from page {pn}, cursor={cursor}")

    total = 0
    while True:
        page_file = PAGES_DIR / f"page_{pn:03d}.json"
        data = None
        for attempt in range(1, MAX_PAGE_RETRIES + 1):
            try:
                agen = h.fetch_user_post_videos(sec_user_id=SEC_UID, max_cursor=cursor, page_counts=PAGE_COUNTS)
                flt = await asyncio.wait_for(agen.__anext__(), timeout=30)
                data = flt._data
                break
            except (APIError, asyncio.TimeoutError, StopAsyncIteration) as exc:
                wait = 8 * attempt
                print(f"  page {pn} attempt {attempt} error: {type(exc).__name__}: {str(exc)[:80]} -> wait {wait}s")
                await asyncio.sleep(wait)
        if data is None:
            print(f"page {pn} failed after {MAX_PAGE_RETRIES} attempts, stopping (partial)")
            break

        lst = data.get("aweme_list") or []
        has_more = data.get("has_more")
        payload = {
            "page": pn,
            "fetched_at": int(time.time()),
            "max_cursor": data.get("max_cursor"),
            "min_cursor": data.get("min_cursor"),
            "has_more": has_more,
            "items": len(lst),
            "aweme_list": lst,
        }
        page_file.write_text(json.dumps(payload, ensure_ascii=False))
        total += len(lst)
        print(f"page {pn}: {len(lst)} items (cum {total}), has_more={has_more}, cursor={data.get('max_cursor')}")
        if has_more != 1 or not lst:
            break
        cursor = data.get("max_cursor") or 0
        pn += 1
        await asyncio.sleep(random.uniform(1.5, 2.8))
    print(f"posts done: {total} items this run")
    return total


def main() -> None:
    silence_f2_logs()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    phases = sys.argv[1:] or ["profile", "posts"]

    async def run():
        if "profile" in phases:
            await phase_profile()
            await asyncio.sleep(random.uniform(1.0, 2.0))
        if "posts" in phases:
            await phase_posts()

    asyncio.run(run())
    print("ALL DONE")


if __name__ == "__main__":
    main()
