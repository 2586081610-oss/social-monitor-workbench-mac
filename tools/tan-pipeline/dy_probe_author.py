"""探测：视频aweme_id → 作者sec_uid/昵称 → 用户资料 → UserPostFilter原始结构。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 兼容 -I 隔离运行
from tan_paths import WORKBENCH_ROOT  # noqa: E402

COOKIE = (WORKBENCH_ROOT / "config" / "f2" / "douyin_cookie.txt").read_text().strip()
AWEME_ID = "7703372555957598650"

from f2.apps.douyin.handler import DouyinHandler
from f2.apps.douyin.utils import ClientConfManager

kwargs = {
    "headers": dict(ClientConfManager.headers()),
    "cookie": COOKIE,
    "timeout": 12,
    "max_retries": 3,
    "proxies": {"http://": None, "https://": None},
}


async def main():
    handler = DouyinHandler(kwargs)

    # 1. 视频详情 → 作者
    detail = await handler.fetch_one_video(aweme_id=AWEME_ID)
    raw = detail._to_dict() if hasattr(detail, "_to_dict") else {}
    aweme = raw.get("aweme_detail") or {}
    author = aweme.get("author") or {}
    print("== 视频详情 ==")
    print("desc:", (aweme.get("desc") or "")[:60])
    print("author nickname:", author.get("nickname"))
    print("author sec_uid:", author.get("sec_uid"))
    print("author uid:", author.get("uid"))
    print("create_time:", aweme.get("create_time"))
    stats = aweme.get("statistics") or {}
    print("stats keys:", {k: stats.get(k) for k in ("digg_count", "comment_count", "share_count", "collect_count") if k in stats})

    sec_uid = author.get("sec_uid")
    if not sec_uid:
        sys.exit("no sec_uid")

    # 2. 用户资料
    prof = await handler.fetch_user_profile(sec_user_id=sec_uid)
    praw = prof._to_dict() if hasattr(prof, "_to_dict") else {}
    user = praw.get("user") or {}
    print("\n== 用户资料 ==")
    print("nickname:", user.get("nickname"))
    print("signature:", user.get("signature"))
    print("follower:", (user.get("follower_count") or 0), "| following:", (user.get("following_count") or 0))
    print("aweme_count:", user.get("aweme_count"), "| favoriting_count:", user.get("favoriting_count"))
    print("sec_uid:", user.get("sec_uid"), "| uid:", user.get("uid"))
    # 常用统计兜底字段
    print("keys:", [k for k in user.keys() if "count" in k][:20])

    # 3. 第一页作品列表，摸清 Filter 结构
    agen = handler.fetch_user_post_videos(sec_user_id=sec_uid, max_cursor=0, page_counts=20)
    page1 = await agen.__anext__()
    print("\n== UserPostFilter ==")
    d = page1._to_dict() if hasattr(page1, "_to_dict") else {}
    print("top keys:", sorted(d.keys())[:25])
    lst = d.get("aweme_list") or []
    print("aweme_list len:", len(lst))
    if lst:
        print("item keys:", sorted(lst[0].keys())[:40])
        st = lst[0].get("statistics") or {}
        print("stats:", {k: st.get(k) for k in ("digg_count", "comment_count", "share_count", "collect_count")})
    print("max_cursor:", d.get("max_cursor"), "has_more:", d.get("has_more"))

asyncio.run(main())
