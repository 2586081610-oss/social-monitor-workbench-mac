"""B站 × 抖音 作品交叉匹配：标题归一化 + 时长指纹 → 同步 / 独享清单。

输出 data/cross_platform_match.json 供生成对比文档。
"""
from __future__ import annotations

import difflib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
from tan_paths import DATA_ROOT

WS = DATA_ROOT  # 任务数据目录（TAN_DATA_DIR 可覆盖）

BILI = WS / "data" / "bilibili" / "521903482"
DOUYIN = WS / "data" / "douyin" / "tanchengyi_822069263606973"

_PUNCT = re.compile(r"[\s#＃@＠，,。.!！?？:：;；~～\-—_+=·…「」『』\[\]()（）【】\"'“”‘’、/\\|<>《》*]+")


def norm(text: str) -> str:
    return _PUNCT.sub("", (text or "").lower())


def load_bili() -> list[dict]:
    details: dict[str, dict] = {}
    for p in sorted(BILI.glob("details/BV*.json")):
        d = json.loads(p.read_text())
        if d.get("code") == 0:
            details[p.stem] = d["data"]
    rows = []
    for bv, det in details.items():
        rows.append({
            "platform": "bili",
            "id": bv,
            "url": f"https://www.bilibili.com/video/{bv}",
            "text": det.get("title") or "",
            "desc": det.get("desc") or "",
            "ts": det.get("pubdate") or 0,
            "dur": det.get("duration") or 0,  # 秒
        })
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows


def load_douyin() -> list[dict]:
    ids: dict[str, dict] = {}
    for p in sorted(DOUYIN.glob("pages/page_*.json")):
        for a in json.loads(p.read_text())["aweme_list"]:
            if a.get("aweme_id") and a["aweme_id"] not in ids:
                ids[a["aweme_id"]] = a
    rows = []
    for a in ids.values():
        rows.append({
            "platform": "douyin",
            "id": a["aweme_id"],
            "url": f"https://www.douyin.com/video/{a['aweme_id']}",
            "text": a.get("desc") or "",
            "ts": a.get("create_time") or 0,
            "dur": (a.get("duration") or 0) / 1000,  # 秒
            "is_image": bool(a.get("images")),
        })
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows


def match_score(b: dict, d: dict) -> float:
    """标题相似度 + 时长指纹联合打分。"""
    tb, td = norm(b["text"]), norm(d["text"])
    ratio = difflib.SequenceMatcher(None, tb, td).ratio() if tb and td else 0.0
    # 短文案且一方是另一方子串（抖音文案常比B站长）
    if tb and td and (tb in td or td in tb) and min(len(tb), len(td)) >= 10:
        ratio = max(ratio, 0.85)
    dur_ok = (not d["is_image"]) and b["dur"] > 0 and d["dur"] > 0
    if dur_ok:
        diff = abs(b["dur"] - d["dur"])
        dur_score = 1.0 if diff <= 2 else (0.8 if diff <= 6 else (0.0 if diff > 15 else 0.3))
    else:
        dur_score = 0.0
    return 0.6 * ratio + 0.4 * dur_score, ratio, dur_score


def main() -> None:
    bili, dy = load_bili(), load_douyin()
    print(f"bili={len(bili)} douyin={len(dy)}")

    pairs: list[tuple[float, float, float, int, int]] = []
    for i, b in enumerate(bili):
        for j, d in enumerate(dy):
            s, r, du = match_score(b, d)
            if s >= 0.55:
                pairs.append((s, r, du, i, j))
    pairs.sort(reverse=True)

    used_b, used_d = set(), set()
    matches = []
    for s, r, du, i, j in pairs:
        if i in used_b or j in used_d:
            continue
        used_b.add(i)
        used_d.add(j)
        matches.append({
            "score": round(s, 3), "title_ratio": round(r, 3), "dur_score": du,
            "bili": bili[i], "douyin": dy[j],
            "days_diff": abs(bili[i]["ts"] - dy[j]["ts"]) // 86400,
        })

    bili_only = [b for i, b in enumerate(bili) if i not in used_b]
    douyin_only = [d for j, d in enumerate(dy) if j not in used_d]

    out = {
        "matched": matches,
        "bili_only": bili_only,
        "douyin_only": douyin_only,
        "counts": {"bili": len(bili), "douyin": len(dy), "matched": len(matches)},
    }
    (WS / "data" / "cross_platform_match.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))

    print(f"matched={len(matches)}  bili_only={len(bili_only)}  douyin_only={len(douyin_only)}")
    print("\n--- 匹配样例（前12，按发布时间倒序） ---")
    for m in matches[:12]:
        print(f"  [{m['days_diff']:>3}d] B:{m['bili']['text'][:26]} ⇔ D:{m['douyin']['text'][:26]} (r={m['title_ratio']})")
    print("\n--- 低置信匹配（需人工复核, title_ratio<0.55） ---")
    for m in matches:
        if m["title_ratio"] < 0.55:
            print(f"  B:{m['bili']['text'][:30]} ⇔ D:{m['douyin']['text'][:30]} (r={m['title_ratio']}, dur={m['dur_score']})")
    print("\n--- 仅B站（前20） ---")
    for b in bili_only[:20]:
        print(f"  {datetime.fromtimestamp(b['ts'], TZ):%Y-%m-%d} {b['dur']:>5}s {b['text'][:36]}")
    print("\n--- 仅抖音（前20） ---")
    for d in douyin_only[:20]:
        kind = "图文" if d["is_image"] else f"{int(d['dur'])}s"
        print(f"  {datetime.fromtimestamp(d['ts'], TZ):%Y-%m-%d} {kind:>5} {d['text'][:36]}")


if __name__ == "__main__":
    main()
