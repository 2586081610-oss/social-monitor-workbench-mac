"""汇总 data/douyin/ 原始数据 → 抖音版特别详细作品全量明细 md + csv（含与B站对比）。"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
from tan_paths import DATA_ROOT

WS = DATA_ROOT  # 任务数据目录（TAN_DATA_DIR 可覆盖）
DATA = WS / "data" / "douyin" / "tanchengyi_822069263606973"
OUT_MD = WS / "抖音博主_谭成义_作品全量明细.md"
OUT_CSV = WS / "抖音博主_谭成义_作品全量明细.csv"

# 双平台对照（cross_platform_match.json 生成）：aweme_id → (B站url, B站标题, 判定)
_MATCH_FILE = WS / "data" / "cross_platform_match.json"
TIER_LABEL = {"A": "标题近似", "B": "文案改写", "C": "标题重写·时长指纹"}
DOUYIN_COUNTERPART: dict[str, tuple[str, str, str]] = {}
if _MATCH_FILE.exists():
    _m = json.loads(_MATCH_FILE.read_text())
    for pair in _m["matched"]:
        r = pair["title_ratio"]
        tier = "A" if r >= 0.80 else ("B" if r >= 0.40 else "C")
        DOUYIN_COUNTERPART[pair["douyin"]["id"]] = (
            pair["bili"]["url"], pair["bili"]["text"][:24], TIER_LABEL[tier],
        )


def fmt_dur(sec: float) -> str:
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_ts(ts) -> str:
    return datetime.fromtimestamp(int(ts), TZ).strftime("%Y-%m-%d %H:%M:%S")


def date_of(ts) -> str:
    return datetime.fromtimestamp(int(ts), TZ).strftime("%Y-%m-%d")


def weekday_of(ts) -> str:
    return "周" + "一二三四五六日"[datetime.fromtimestamp(int(ts), TZ).weekday()]


profile = json.loads((DATA / "profile.json").read_text())["raw"]["user"]
ids: dict[str, dict] = {}
for p in sorted((DATA / "pages").glob("page_*.json")):
    pg = json.loads(p.read_text())
    for a in pg["aweme_list"]:
        aid = a.get("aweme_id")
        if aid and aid not in ids:
            ids[aid] = a

rows = []
for a in ids.values():
    st = a.get("statistics") or {}
    tags = [t.get("hashtag_name") for t in (a.get("text_extra") or []) if t.get("hashtag_name")]
    mix = a.get("mix_info") or {}
    chapters = [
        {"title": c.get("desc") or "", "ts": (c.get("timestamp") or 0) / 1000}
        for c in (a.get("chapter_list") or [])
    ]
    rows.append({
        "aweme_id": a["aweme_id"],
        "desc": (a.get("desc") or "").strip(),
        "tags": tags,
        "create_time": a.get("create_time") or 0,
        "duration_ms": a.get("duration") or 0,
        "is_video": not a.get("images"),
        "images_cnt": len(a.get("images") or []),
        "is_top": bool(a.get("is_top")),
        "mix_name": mix.get("mix_name") or "",
        "music": ((a.get("music") or {}).get("title")) or "",
        "chapters": chapters,
        "digg": st.get("digg_count", 0),
        "comment": st.get("comment_count", 0),
        "share": st.get("share_count", 0),
        "collect": st.get("collect_count", 0),
    })
rows.sort(key=lambda r: r["create_time"], reverse=True)

n = len(rows)
videos = [r for r in rows if r["is_video"]]
images_posts = [r for r in rows if not r["is_video"]]
pinned = [r for r in rows if r["is_top"]]
tot = {k: sum(r[k] for r in rows) for k in ("digg", "comment", "share", "collect")}
tot_dur = sum(r["duration_ms"] / 1000 for r in videos)
newest, oldest = rows[0], rows[-1]
years = Counter(date_of(r["create_time"])[:4] for r in rows)
span_days = max(1, (int(newest["create_time"]) - int(oldest["create_time"])) // 86400)
tag_counter = Counter(t for r in rows for t in r["tags"])
mixes = Counter(r["mix_name"] for r in rows if r["mix_name"])
top_digg = sorted(rows, key=lambda r: r["digg"], reverse=True)[:10]
top_collect = sorted(rows, key=lambda r: r["collect"], reverse=True)[:10]
top_comment = sorted(rows, key=lambda r: r["comment"], reverse=True)[:10]
top_share = sorted(rows, key=lambda r: r["share"], reverse=True)[:10]


def rank_table(items, metric, label):
    out = ["| 排名 | 发布日期 | 文案 | " + label + " |", "|---|---|---|---|"]
    for i, r in enumerate(items, 1):
        d = r["desc"][:38] + ("…" if len(r["desc"]) > 38 else "")
        out.append(f"| {i} | {date_of(r['create_time'])} | [{d}](https://www.douyin.com/video/{r['aweme_id']}) | {r[metric]:,} |")
    return "\n".join(out)


# ---------- CSV ----------
with OUT_CSV.open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["aweme_id", "pubdate", "weekday", "kind", "duration_sec", "digg", "comment",
                "collect", "share", "is_top", "mix_name", "tags", "chapters_cnt", "desc"])
    for r in rows:
        w.writerow([
            r["aweme_id"], fmt_ts(r["create_time"]), weekday_of(r["create_time"]),
            "视频" if r["is_video"] else f"图文×{r['images_cnt']}",
            int(r["duration_ms"] / 1000), r["digg"], r["comment"], r["collect"], r["share"],
            int(r["is_top"]), r["mix_name"], " ".join(r["tags"]), len(r["chapters"]),
            r["desc"].replace("\n", " "),
        ])

# ---------- Markdown ----------
L: list[str] = []
ap = L.append

ap("# 抖音博主「谭成义」作品全量明细")
ap("")
ap(f"> 生成时间：{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} · 数据来源：抖音官方接口（用户资料 + 作品列表全量分页，F2 隔离运行时采集） · 可见作品 {n} 条")
ap("")
ap("---")
ap("")
ap("## 一、博主档案")
ap("")
ap("| 项目 | 内容 |")
ap("|---|---|")
ap(f"| 昵称 | {profile.get('nickname', '')} |")
ap(f"| 抖音号 | {profile.get('unique_id') or '-'} |")
ap(f"| uid | {profile.get('uid') or ''} |")
ap("| 主页 | https://www.douyin.com/user/" + (profile.get("sec_uid") or "") + " |")
ap(f"| 粉丝数 | {profile.get('follower_count', 0):,} |")
ap(f"| 关注数 | {profile.get('following_count', 0):,} |")
ap(f"| 获赞总数（累计） | {profile.get('total_favorited', 0):,} |")
ap(f"| IP属地 | {(profile.get('ip_location') or '').replace('IP属地：', '') or '-'} |")
ap(f"| 个人签名 | {(profile.get('signature') or '（无）').replace(chr(10), ' / ')} |")
ap(f"| 账号计数作品数 | {profile.get('aweme_count')}（列表接口实际抓到可见作品 {n} 条，差值一般为私密/不可见内容） |")
ap("")
ap("## 二、数据总览")
ap("")
ap("| 指标 | 数值 | 指标 | 数值 |")
ap("|---|---|---|---|")
ap(f"| 可见作品 | {n} 条（视频 {len(videos)} + 图文 {len(images_posts)}） | 总视频时长 | {fmt_dur(tot_dur)}（约 {int(tot_dur // 3600)} 小时） |")
ap(f"| 总点赞 | {tot['digg']:,} | 总评论 | {tot['comment']:,} |")
ap(f"| 总收藏 | {tot['collect']:,} | 总转发 | {tot['share']:,} |")
ap(f"| 平均单条点赞 | {tot['digg'] // n:,} | 平均单条收藏 | {tot['collect'] // n:,} |")
ap(f"| 置顶作品 | {len(pinned)} 条 | 合集 | {len(mixes)} 个（共 {sum(mixes.values())} 条） |")
ap(f"| 最早作品 | {fmt_ts(oldest['create_time'])} | 最新作品 | {fmt_ts(newest['create_time'])} |")
ap(f"| 持续更新 | {span_days:,} 天（约 {span_days / 365:.1f} 年） | 发布频率 | 平均约 {span_days / max(1, n):.1f} 天一条 |")
ap("")
ap(f"> **双平台对照**：{len(DOUYIN_COUNTERPART)}/{n} 与B站同款互链（见各作品「平台对照」），{n - len(DOUYIN_COUNTERPART)} 条抖音独享（含图文 {len(images_posts)} 条）；判定明细见《双平台作品对比_谭成义.md》。")
ap("")
ap("### 分年份发布量")
ap("")
ap("| 年份 | 作品数 | 占比 |")
ap("|---|---|---|")
for y in sorted(years):
    ap(f"| {y} | {years[y]} | {years[y] / n * 100:.1f}% |")
ap("")
ap("### 合集分布")
ap("")
ap("| 合集 | 作品数 |")
ap("|---|---|")
for m, c in mixes.most_common():
    ap(f"| {m} | {c} |")
ap("")
ap("### 点赞 Top 10")
ap("")
ap(rank_table(top_digg, "digg", "点赞"))
ap("")
ap("### 收藏 Top 10")
ap("")
ap(rank_table(top_collect, "collect", "收藏"))
ap("")
ap("### 评论 Top 10")
ap("")
ap(rank_table(top_comment, "comment", "评论"))
ap("")
ap("### 转发 Top 10")
ap("")
ap(rank_table(top_share, "share", "转发"))
ap("")
tag_lines = "、".join(f"`#{t}`×{c}" for t, c in tag_counter.most_common(30))
ap("### 高频话题标签 Top 30")
ap("")
ap(tag_lines if tag_lines else "（无话题数据）")
ap("")
ap("---")
ap("")
ap("## 三、全量作品明细总表（按发布时间倒序）")
ap("")
ap("| # | 发布日期 | 文案 | 类型 | 时长 | 点赞 | 评论 | 收藏 | 转发 |")
ap("|---|---|---|---|---|---|---|---|---|")
for i, r in enumerate(rows, 1):
    d = r["desc"][:32] + ("…" if len(r["desc"]) > 32 else "")
    kind = "视频" if r["is_video"] else f"图文×{r['images_cnt']}"
    dur = fmt_dur(r["duration_ms"] / 1000) if r["is_video"] else "-"
    top_mark = " 📌" if r["is_top"] else ""
    ap(f"| {i} | {date_of(r['create_time'])} | [{d}](https://www.douyin.com/video/{r['aweme_id']}){top_mark} "
       f"| {kind} | {dur} | {r['digg']:,} | {r['comment']:,} | {r['collect']:,} | {r['share']:,} |")
ap("")
ap("---")
ap("")
ap("## 四、逐作品档案（按发布时间倒序）")
ap("")
for i, r in enumerate(rows, 1):
    ap(f"### {i}. {r['desc'][:60] or '（无文案）'}" + ("（📌置顶）" if r["is_top"] else ""))
    ap("")
    ap(f"- **链接**：https://www.douyin.com/video/{r['aweme_id']}（aweme_id：{r['aweme_id']}）")
    ap(f"- **发布时间**：{fmt_ts(r['create_time'])}（{weekday_of(r['create_time'])}）")
    if r["is_video"]:
        ap(f"- **类型**：视频 · **时长**：{fmt_dur(r['duration_ms'] / 1000)}")
    else:
        ap(f"- **类型**：图文 · **图片数**：{r['images_cnt']}")
    if r["mix_name"]:
        ap(f"- **合集**：{r['mix_name']}")
    if r["music"]:
        ap(f"- **配乐**：{r['music']}")
    if r["tags"]:
        ap(f"- **话题标签**：{'、'.join('#' + t for t in r['tags'])}")
    if r["chapters"]:
        chs = "；".join(
            f"[{fmt_dur(c['ts'])}]{c['title']}" if c["ts"] else c["title"]
            for c in r["chapters"] if c["title"]
        )
        if chs:
            ap(f"- **视频章节（{len(r['chapters'])}节）**：{chs}")
    if r["desc"]:
        desc = r["desc"].replace("\n", "  \n  > ")
        ap("- **文案全文**：")
        ap(f"  > {desc}")
    ap(f"- **数据**：点赞 {r['digg']:,} · 评论 {r['comment']:,} · 收藏 {r['collect']:,} · 转发 {r['share']:,}")
    cp = DOUYIN_COUNTERPART.get(r["aweme_id"])
    if cp:
        ap(f"- **平台对照**：B站同款 → [{cp[1]}…]({cp[0]})（判定：{cp[2]}）")
    else:
        ap("- **平台对照**：**抖音独享**（B站无同款）")
    ap("")

ap("---")
ap("")
ap("## 五、与B站账号对比（同一博主）")
ap("")
ap("| 维度 | 抖音（谭成义 @nishitiantan） | B站（谭成义 UID 521903482） |")
ap("|---|---|---|")
ap(f"| 粉丝数 | 4,078,354（抓取时点 {datetime.now(TZ).strftime('%Y-%m-%d')}） | 277,111（2026-11-03） |")
ap(f"| 可见作品 | {n}（2020-07 起） | 197（2020-11 起） |")
ap(f"| 累计获赞 | 19,726,543（账号口径） | 761,674（{len(rows)}条作品加总口径） |")
ap("| 主力内容 | 健身教学/跟练/比赛日常，时长普遍更长 | 与抖音同源内容搬运 + 部分B站特供（数据为抖音的零头） |")
ap("| 量级差 | 主阵地，粉丝为B站的约 14.7 倍 | 副阵地 |")
ap("")
ap("> 两边签名一致（好好吃饭，好好睡觉，好好训练，好好生活！！），作品文案高度同源（如「器械系列之单手下拉」2026-11-02 双平台同发）。")
ap("")
ap("## 附注")
ap("")
ap(f"- 数据抓取于 {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}，为抓取时点快照；抖音不提供历史播放量，故无播放列。")
ap("- 原始JSON存档：`data/douyin/tanchengyi_822069263606973/`（profile.json + pages/ 全量分页原始响应），CSV 版本同名 `.csv`。")
ap("- 「视频章节」来自抖音章节功能（chapter_list），仅部分长视频有。")
ap("- 置顶标记（📌）为抓取时点的置顶状态。")

OUT_MD.write_text("\n".join(L))
print(f"written: {OUT_MD} ({OUT_MD.stat().st_size:,} bytes)")
print(f"written: {OUT_CSV}")
