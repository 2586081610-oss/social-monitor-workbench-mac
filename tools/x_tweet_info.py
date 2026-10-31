"""推文完整信息抓取（GraphQL TweetResultByRestId，经 yt-dlp 的会话与查询模板）。

用法：.venv/bin/python tools/x_tweet_info.py <推文URL或ID> [--json]
- 匿名可用；带 cookie（platform_cookies.json 的 twitter.cookie → cookies_file）更全
- 输出：作者/全文（含长文 note_tweet）/点赞/转发/引用/回复/浏览/媒体直链/引用链
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_TWEET_ID = re.compile(r"(?:status(?:es)?/)?(\d{15,20})")


def _tweet_id(arg: str) -> str:
    match = _TWEET_ID.search(arg)
    if not match:
        raise SystemExit(f"无法从参数中提取推文 ID: {arg}")
    return match.group(1)


def fetch_full_tweet(tweet_id: str) -> dict:
    from yt_dlp import YoutubeDL
    from yt_dlp.extractor.twitter import TwitterIE

    cookiefile = ""
    pc = ROOT / "config" / "platform_cookies.json"
    if pc.is_file():
        raw = (json.loads(pc.read_text(encoding="utf-8")).get("twitter") or {}).get("cookies_file") or ""
        if raw and Path(raw).is_file():
            cookiefile = raw
    ydl = YoutubeDL({"cookiefile": cookiefile, "quiet": True})
    ie = TwitterIE(ydl)
    ie.set_downloader(ydl)
    data = ie._call_graphql_api("2ICDjqPd81tulZcYrtpTuQ/TweetResultByRestId", tweet_id)
    return (data.get("tweetResult") or {}).get("result") or {}


def summarize(r: dict, depth: int = 0) -> dict:
    legacy = r.get("legacy", {})
    user = r.get("core", {}).get("user_results", {}).get("result", {})
    uleg = user.get("legacy", {})
    note = r.get("note_tweet", {}).get("note_tweet_results", {}).get("result", {})
    media = []
    for m in legacy.get("extended_entities", {}).get("media") or []:
        variants = [v for v in (m.get("video_info") or {}).get("variants", []) if v.get("bitrate")]
        best = max(variants, key=lambda x: x["bitrate"], default={})
        media.append(
            {
                "type": m.get("type"),
                "url": best.get("url") or m.get("media_url_https"),
                "thumbnail": m.get("media_url_https"),
            }
        )
    item = {
        "author": f"{uleg.get('name')} @{uleg.get('screen_name')}",
        "followers": uleg.get("followers_count"),
        "created_at": legacy.get("created_at"),
        "text": note.get("text") or legacy.get("full_text") or "",
        "favorite_count": legacy.get("favorite_count"),
        "retweet_count": legacy.get("retweet_count"),
        "quote_count": legacy.get("quote_count"),
        "reply_count": legacy.get("reply_count"),
        "view_count": (legacy.get("views") or {}).get("count"),
        "media": media,
    }
    inner = (r.get("quoted_status_result") or {}).get("result")
    if inner:
        item["quoted_tweet"] = summarize(inner, depth + 1)
    return item


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--json"]
    as_json = "--json" in sys.argv
    tweet_id = _tweet_id(args[0] if args else "")
    result = fetch_full_tweet(tweet_id)
    if not result:
        raise SystemExit("推文不存在或不可见")
    if as_json:
        print(json.dumps(summarize(result), ensure_ascii=False, indent=1))
        return
    item = summarize(result)
    print(f"作者: {item['author']} (粉丝 {item['followers']})")
    print(f"发布: {item['created_at']}")
    print(f"全文: {item['text']}")
    print(f"点赞 {item['favorite_count']} | 转发 {item['retweet_count']} | 引用 {item['quote_count']} | 回复 {item['reply_count']} | 浏览 {item['view_count']}")
    for m in item["media"]:
        print(f"· {m['type']}: {m['url']}")
    if "quoted_tweet" in item:
        q = item["quoted_tweet"]
        print(f"── 引用原帖: {q['author']} ──")
        print(q["text"])
        for m in q["media"]:
            print(f"· {m['type']}: {m['url']}")


if __name__ == "__main__":
    main()
