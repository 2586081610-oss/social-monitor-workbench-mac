"""快速探测：card/navnum/relation（免wbi）与 arc/search（wbi+dm参数）哪个可用。"""
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 兼容 -I 隔离运行
from tan_paths import WORKBENCH_ROOT  # noqa: E402

MID = 521903482
CK = json.loads((WORKBENCH_ROOT / "config" / "platform_cookies.json").read_text())["bilibili"]["cookie"]
cookies = dict(p.split("=", 1) for p in CK.split(";") if "=" in p)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
c = httpx.Client(cookies=cookies, timeout=15, trust_env=False, headers={"User-Agent": UA})

TAB = (46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11)


def sign(params, img, sub):
    mixed = img + sub
    mk = "".join(mixed[i] for i in TAB)[:32]
    o = {k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in params.items() if k != "w_rid"}
    o["wts"] = int(time.time())
    o = dict(sorted(o.items()))
    o["w_rid"] = hashlib.md5((urlencode(o) + mk).encode()).hexdigest()
    return o


# 1. 免签名端点
for name, url, params in [
    ("card", "https://api.bilibili.com/x/web-interface/card", {"mid": MID, "photo": "true"}),
    ("relation", "https://api.bilibili.com/x/relation/stat", {"vmid": MID}),
    ("navnum", "https://api.bilibili.com/x/space/navnum", {"mid": MID}),
]:
    r = c.get(url, params=params, headers={"Referer": f"https://space.bilibili.com/{MID}"})
    d = r.json()
    print(f"[{name}] http={r.status_code} code={d.get('code')}", str(d.get("data"))[:300])

# 2. nav 拿 wbi key
nav = c.get("https://api.bilibili.com/x/web-interface/nav").json()
wbi = nav["data"]["wbi_img"]
img = wbi["img_url"].rsplit("/", 1)[-1].split(".")[0]
sub = wbi["sub_url"].rsplit("/", 1)[-1].split(".")[0]
print("[nav] isLogin =", nav["data"].get("isLogin"))

# 3. arc/search 增强参数
params = sign({
    "mid": MID, "pn": 1, "ps": 30, "order": "pubdate",
    "platform": "web", "web_location": "1550101", "order_avoided": "true",
    "dm_img_list": "[]",
    "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMy4wIENvcmUp",
    "dm_cover_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMy4wIENvcmUp",
    "dm_img_inter": '{"ds":[],"wh":[3005,1528,106],"of":[300,1528,300]}',
}, img, sub)
r = c.get("https://api.bilibili.com/x/space/wbi/arc/search", params=params,
          headers={"Referer": f"https://space.bilibili.com/{MID}/video", "Origin": "https://space.bilibili.com"})
d = r.json()
vl = ((d.get("data") or {}).get("list") or {}).get("vlist") or []
print(f"[arc/search+dm] http={r.status_code} code={d.get('code')} items={len(vl)} page={str(((d.get('data') or {}).get('page') or ''))[:120]}")
if vl:
    print("  first:", vl[0].get("bvid"), vl[0].get("title")[:40], "created:", vl[0].get("created"))
