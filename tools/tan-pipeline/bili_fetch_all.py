"""B站UP主全量作品拉取（wbi 签名 + 登录cookie + 断点续传）。

签名逻辑复用 socialmonitor-mac backend/app/services/archive_metadata_resolver.py。
数据落 data/bilibili/<mid>/：profile.json、pages/page_NNN.json、details/BV*.json、tags/BV*.json。

用法（项目venv运行）：
  .venv/bin/python scripts/bili_fetch_all.py            # 跑完所有未完成阶段
  .venv/bin/python scripts/bili_fetch_all.py list       # 只跑列表页
  .venv/bin/python scripts/bili_fetch_all.py details    # 只补详情
"""
from __future__ import annotations

import hashlib
import json
import random
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from tan_paths import DATA_ROOT, WORKBENCH_ROOT

MID = 521903482  # 谭成义
WORKSPACE = DATA_ROOT  # 任务数据目录（TAN_DATA_DIR 可覆盖）
DATA_DIR = WORKSPACE / "data" / "bilibili" / str(MID)
PAGES_DIR = DATA_DIR / "pages"
DETAILS_DIR = DATA_DIR / "details"
TAGS_DIR = DATA_DIR / "tags"
PROJECT_COOKIE = WORKBENCH_ROOT / "config" / "platform_cookies.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

WBI_MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
)


def load_cookies() -> dict[str, str]:
    raw = json.loads(PROJECT_COOKIE.read_text())["bilibili"]["cookie"]
    cookies: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


COOKIES = load_cookies()
CLIENT = httpx.Client(
    cookies=COOKIES,
    timeout=15.0,
    trust_env=False,
    follow_redirects=False,
    headers={"User-Agent": UA, "Referer": f"https://space.bilibili.com/{MID}/video"},
)


def polite_sleep(a: float = 0.8, b: float = 1.5) -> None:
    time.sleep(random.uniform(a, b))


def sign_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    mixed = img_key + sub_key
    mixin_key = "".join(mixed[i] for i in WBI_MIXIN_KEY_ENC_TAB)[:32]
    ordered = {
        k: "".join(c for c in str(v) if c not in "!'()*")
        for k, v in params.items()
        if k != "w_rid"
    }
    ordered["wts"] = int(time.time())
    ordered = dict(sorted(ordered.items()))
    ordered["w_rid"] = hashlib.md5((urlencode(ordered) + mixin_key).encode()).hexdigest()
    return ordered


def _wbi_asset_key(url: str) -> str | None:
    name = url.rsplit("/", 1)[-1]
    if "." in name and len(name.split(".")[0]) == 32:
        return name.split(".")[0]
    return None


_WBI_CACHE: tuple[float, str, str] | None = None


def get_wbi_keys() -> tuple[str, str]:
    global _WBI_CACHE
    if _WBI_CACHE and time.time() - _WBI_CACHE[0] < 1800:
        return _WBI_CACHE[1], _WBI_CACHE[2]
    r = CLIENT.get("https://api.bilibili.com/x/web-interface/nav")
    wbi = r.json()["data"]["wbi_img"]
    img, sub = _wbi_asset_key(wbi["img_url"]), _wbi_asset_key(wbi["sub_url"])
    if not img or not sub:
        raise RuntimeError("wbi key parse failed")
    _WBI_CACHE = (time.time(), img, sub)
    return img, sub


RISK_CODES = {-352, -412, -799}


def api_get(url: str, params: dict, referer: str | None = None, retries: int = 6) -> dict:
    headers = {"Referer": referer} if referer else None
    for attempt in range(retries):
        r = CLIENT.get(url, params=params, headers=headers)
        if r.status_code == 429 or r.status_code >= 500:
            wait = 8 * (attempt + 1)
            print(f"  http {r.status_code}, wait {wait}s (attempt {attempt + 1})")
            time.sleep(wait)
            continue
        data = r.json()
        code = data.get("code")
        if code == 0:
            return data
        if code in RISK_CODES:
            wait = 10 * (attempt + 1)
            print(f"  risk control {code}, wait {wait}s (attempt {attempt + 1})")
            time.sleep(wait)
            continue
        # 业务性错误（如无稿件）直接返回
        return data
    raise RuntimeError(f"exhausted retries for {url}: last={r.status_code}")


def fetch_profile() -> None:
    """acc/info 被风控（-352），资料改走免签名 card/navnum/relation。"""
    card = api_get(
        "https://api.bilibili.com/x/web-interface/card",
        {"mid": MID, "photo": "true"},
        referer=f"https://space.bilibili.com/{MID}",
    )
    polite_sleep()
    rel = api_get("https://api.bilibili.com/x/relation/stat", {"vmid": MID})
    polite_sleep()
    navnum = api_get("https://api.bilibili.com/x/space/navnum", {"mid": MID})
    out = {
        "fetched_at": int(time.time()),
        "card": card.get("data"),
        "relation": rel.get("data"),
        "navnum": navnum.get("data"),
    }
    (DATA_DIR / "profile.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    c = card.get("data", {}).get("card", {})
    print(f"profile: {c.get('name')} fans={out['relation'].get('follower')} "
          f"videos={out['navnum'].get('video')} sign={(c.get('sign') or '')[:40]}")


def fetch_list() -> int:
    img, sub = get_wbi_keys()
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    pn = 1
    total = None
    while True:
        page_file = PAGES_DIR / f"page_{pn:03d}.json"
        if page_file.exists():
            data = json.loads(page_file.read_text())
        else:
            data = api_get(
                "https://api.bilibili.com/x/space/wbi/arc/search",
                sign_wbi({
                    "mid": MID, "pn": pn, "ps": 30, "order": "pubdate",
                    "platform": "web", "web_location": "1550101",
                    "order_avoided": "true", "dm_img_list": "[]",
                    "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMy4wIENvcmUp",
                    "dm_cover_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMy4wIENvcmUp",
                    "dm_img_inter": '{"ds":[],"wh":[3005,1528,106],"of":[300,1528,300]}',
                }, img, sub),
                referer=f"https://space.bilibili.com/{MID}/video",
            )
            if data.get("code") != 0:
                print(f"page {pn} error: {data.get('code')} {data.get('message')}")
                break
            page_file.write_text(json.dumps(data, ensure_ascii=False, indent=1))
            polite_sleep()
        info = (data.get("data") or {}).get("page") or {}
        vlist = ((data.get("data") or {}).get("list") or {}).get("vlist") or []
        total = info.get("count", total)
        print(f"page {pn}: {len(vlist)} items (count={total})")
        if not vlist:
            break
        if total is not None and pn * 30 >= total:
            break
        pn += 1
    n = sum(
        len(((json.loads(p.read_text()).get("data") or {}).get("list") or {}).get("vlist") or [])
        for p in sorted(PAGES_DIR.glob("page_*.json"))
    )
    print(f"list done: {n} videos")
    return n


def collect_bvids() -> list[dict]:
    seen: dict[str, dict] = {}
    for p in sorted(PAGES_DIR.glob("page_*.json")):
        data = json.loads(p.read_text())
        for v in ((data.get("data") or {}).get("list") or {}).get("vlist") or []:
            if v.get("bvid") and v["bvid"] not in seen:
                seen[v["bvid"]] = v
    items = list(seen.values())
    items.sort(key=lambda x: x.get("created", 0))
    return items


def fetch_details() -> None:
    DETAILS_DIR.mkdir(parents=True, exist_ok=True)
    TAGS_DIR.mkdir(parents=True, exist_ok=True)
    items = collect_bvids()
    todo = [v for v in items if not (DETAILS_DIR / f"{v['bvid']}.json").exists()]
    print(f"details: {len(items)} total, {len(todo)} to fetch")
    for i, v in enumerate(todo, 1):
        bv = v["bvid"]
        det = api_get(
            "https://api.bilibili.com/x/web-interface/view",
            {"bvid": bv},
            referer=f"https://www.bilibili.com/video/{bv}/",
        )
        if det.get("code") == 0:
            (DETAILS_DIR / f"{bv}.json").write_text(json.dumps(det, ensure_ascii=False, indent=1))
        else:
            print(f"  {bv} view err {det.get('code')}: {det.get('message')}")
        polite_sleep(0.9, 1.6)
        tag = api_get(
            "https://api.bilibili.com/x/tag/archive/tags", {"bvid": bv},
            referer=f"https://www.bilibili.com/video/{bv}/",
        )
        if tag.get("code") == 0:
            (TAGS_DIR / f"{bv}.json").write_text(json.dumps(tag, ensure_ascii=False, indent=1))
        if i % 10 == 0:
            print(f"  {i}/{len(todo)} done ({bv})")
    print("details done")


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    phases = sys.argv[1:] or ["profile", "list", "details"]
    for ph in phases:
        print(f"== phase: {ph} ==")
        if ph == "profile":
            fetch_profile()
        elif ph == "list":
            fetch_list()
        elif ph == "details":
            fetch_details()
    print("ALL DONE")
