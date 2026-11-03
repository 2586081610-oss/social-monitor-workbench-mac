"""汇总 data/bilibili/<mid>/ 原始数据 → 特别详细的作品全量明细 md + csv。"""
from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tan_paths import DATA_ROOT

MID = 521903482
WS = DATA_ROOT  # 任务数据目录（TAN_DATA_DIR 可覆盖）
DATA = WS / "data" / "bilibili" / str(MID)
OUT_MD = WS / "B站博主_谭成义_作品全量明细.md"
OUT_CSV = WS / "B站博主_谭成义_作品全量明细.csv"
TZ = timezone(timedelta(hours=8))

# 双平台对照（cross_platform_match.json 生成）：bvid → (抖音url, 抖音文案, 判定)
_MATCH_FILE = WS / "data" / "cross_platform_match.json"
TIER_LABEL = {"A": "标题近似", "B": "文案改写", "C": "标题重写·时长指纹"}
BILI_COUNTERPART: dict[str, tuple[str, str, str]] = {}
DOUYIN_MATCHED: set[str] = set()
if _MATCH_FILE.exists():
    _m = json.loads(_MATCH_FILE.read_text())
    for pair in _m["matched"]:
        r = pair["title_ratio"]
        tier = "A" if r >= 0.80 else ("B" if r >= 0.40 else "C")
        BILI_COUNTERPART[pair["bili"]["id"]] = (
            pair["douyin"]["url"], pair["douyin"]["text"][:24], TIER_LABEL[tier],
        )
        DOUYIN_MATCHED.add(pair["douyin"]["id"])


def load_json(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def fmt_dur(sec: int) -> str:
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_ts(ts) -> str:
    return datetime.fromtimestamp(int(ts), TZ).strftime("%Y-%m-%d %H:%M:%S")


def date_of(ts) -> str:
    return datetime.fromtimestamp(int(ts), TZ).strftime("%Y-%m-%d")


def weekday_of(ts) -> str:
    return "周" + "一二三四五六日"[datetime.fromtimestamp(int(ts), TZ).weekday()]


# ---------- 读取 ----------
profile = load_json(DATA / "profile.json") or {}
card = ((profile.get("card") or {}).get("card")) or {}
relation = profile.get("relation") or {}
navnum = profile.get("navnum") or {}

videos: dict[str, dict] = {}
for p in sorted((DATA / "pages").glob("page_*.json")):
    d = load_json(p)
    for v in ((d.get("data") or {}).get("list") or {}).get("vlist") or []:
        videos.setdefault(v["bvid"], {"list": v})

details: dict[str, dict] = {}
for p in sorted((DATA / "details").glob("BV*.json")):
    d = load_json(p)
    if d and d.get("code") == 0:
        details[p.stem] = d["data"]

tags: dict[str, list] = {}
for p in sorted((DATA / "tags").glob("BV*.json")):
    d = load_json(p)
    if d and d.get("code") == 0:
        tags[p.stem] = [t.get("tag_name", "") for t in d.get("data") or []]

TID_MAP = {164: "运动·健身", 21: "生活·日常"}  # 匿名view接口tname为空，用tid映射

rows = []
for bv, v in videos.items():
    det = details.get(bv) or {}
    lst = v["list"]
    stat = det.get("stat") or {}
    tid = det.get("tid") or lst.get("typeid") or 0
    rows.append({
        "bvid": bv,
        "aid": det.get("aid") or lst.get("aid"),
        "title": det.get("title") or lst.get("title", ""),
        "desc": det.get("desc") or lst.get("description", ""),
        "pubdate": det.get("pubdate") or lst.get("created", 0),
        "duration": det.get("duration") or 0,
        "length_str": lst.get("length", ""),
        "tname": det.get("tname") or TID_MAP.get(tid, f"分区{tid}"),
        "tname_v2": det.get("tname_v2") or "",
        "pic": det.get("pic") or lst.get("pic", ""),
        "videos_cnt": det.get("videos") or 1,
        "pages": det.get("pages") or [],
        "staff": det.get("staff") or [],
        "tags": tags.get(bv, []),
        "view": stat.get("view", 0),
        "danmaku": stat.get("danmaku", 0),
        "reply": stat.get("reply", 0),
        "favorite": stat.get("favorite", 0),
        "coin": stat.get("coin", 0),
        "share": stat.get("share", 0),
        "like": stat.get("like", 0),
        "is_pay": bool(lst.get("is_pay")),
        "is_charging": bool(lst.get("is_charging_arc")),
        "copyright": {1: "自制", 2: "转载"}.get(lst.get("copyright"), ""),
    })
rows.sort(key=lambda r: r["pubdate"], reverse=True)  # 最新在前

missing = [bv for bv in videos if bv not in details]
print(f"videos={len(rows)} details={len(details)} tags={len(tags)} missing_details={missing}")

# ---------- 统计 ----------
tot = {k: sum(r[k] for r in rows) for k in ("view", "danmaku", "reply", "favorite", "coin", "share", "like", "duration")}
newest, oldest = rows[0], rows[-1]
years = Counter(date_of(r["pubdate"])[:4] for r in rows)
year_span = sorted(years)
months = Counter(date_of(r["pubdate"])[:7] for r in rows)
active_months = len(months)
tnames = Counter(r["tname"] or "未知" for r in rows)
tag_counter = Counter(t for r in rows for t in r["tags"] if t)
top_view = sorted(rows, key=lambda r: r["view"], reverse=True)[:10]
top_like = sorted(rows, key=lambda r: r["like"], reverse=True)[:10]
top_fav = sorted(rows, key=lambda r: r["favorite"], reverse=True)[:10]
multi_p = [r for r in rows if r["videos_cnt"] > 1]
coop = [r for r in rows if r["staff"]]
span_days = max(1, (int(newest["pubdate"]) - int(oldest["pubdate"])) // 86400)


def rank_table(items, metric, label):
    out = ["| 排名 | 发布日期 | 标题 | " + label + " |", "|---|---|---|---|"]
    for i, r in enumerate(items, 1):
        out.append(
            f"| {i} | {date_of(r['pubdate'])} | [{r['title']}](https://www.bilibili.com/video/{r['bvid']}) "
            f"| {r[metric]:,} |"
        )
    return "\n".join(out)


# ---------- CSV ----------
csv_cols = ["bvid", "aid", "pubdate", "weekday", "title", "duration_sec", "tname", "tname_v2",
            "view", "danmaku", "reply", "like", "coin", "favorite", "share", "tags", "pages_cnt",
            "copyright", "is_pay", "is_charging", "desc"]
with OUT_CSV.open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(csv_cols)
    for r in rows:  # csv 按时间倒序同 md
        w.writerow([
            r["bvid"], r["aid"], fmt_ts(r["pubdate"]), weekday_of(r["pubdate"]), r["title"],
            r["duration"], r["tname"], r["tname_v2"], r["view"], r["danmaku"], r["reply"],
            r["like"], r["coin"], r["favorite"], r["share"], " ".join(r["tags"]),
            r["videos_cnt"], r["copyright"], int(r["is_pay"]), int(r["is_charging"]),
            r["desc"].replace("\n", " "),
        ])

# ---------- Markdown ----------
L: list[str] = []
ap = L.append

ap(f"# B站博主「{card.get('name', '')}」作品全量明细")
ap("")
ap(f"> 生成时间：{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} · 数据来源：B站官方API（space列表 + 逐视频view详情 + tag接口） · 共 {len(rows)} 个视频作品")
ap("")
ap("---")
ap("")
ap("## 一、博主档案")
ap("")
ap("| 项目 | 内容 |")
ap("|---|---|")
ap(f"| 昵称 | {card.get('name', '')} |")
ap(f"| UID (mid) | {card.get('mid', '')} |")
ap(f"| 主页 | https://space.bilibili.com/{MID} |")
ap(f"| 粉丝数 | {relation.get('follower', 0):,} |")
ap(f"| 关注数 | {relation.get('following', 0)} |")
ap(f"| 个人签名 | {card.get('sign') or '（无）'} |")
ap(f"| 性别 | {card.get('sex') or '-'} |")
ap(f"| 等级 | Lv.{card.get('level_info', {}).get('current_level', '-')} |")
ap(f"| 投稿构成 | 视频 {navnum.get('video', 0)} · 专栏 {navnum.get('article', 0)} · 音频 {navnum.get('audio', 0)} · 相册 {navnum.get('album', 0)} · 图文动态 {navnum.get('opus', 0)} · 合集 {navnum.get('season_num', 0)} · 课程 {navnum.get('pugv', 0)} |")
face = card.get("face")
if face:
    ap(f"| 头像 | {face} |")
ap("")

ap("## 二、数据总览")
ap("")
ap("| 指标 | 数值 | 指标 | 数值 |")
ap("|---|---|---|---|")
ap(f"| 作品总数 | {len(rows)} | 总时长 | {fmt_dur(tot['duration'])}（{tot['duration']:,} 秒 / 约 {tot['duration'] // 3600} 小时） |")
ap(f"| 总播放 | {tot['view']:,} | 总点赞 | {tot['like']:,} |")
ap(f"| 总弹幕 | {tot['danmaku']:,} | 总评论 | {tot['reply']:,} |")
ap(f"| 总投币 | {tot['coin']:,} | 总收藏 | {tot['favorite']:,} |")
ap(f"| 总转发 | {tot['share']:,} | 平均单视频时长 | {fmt_dur(tot['duration'] / len(rows))} |")
ap(f"| 平均单视频播放 | {tot['view'] // len(rows):,} | 平均单视频点赞 | {tot['like'] // len(rows):,} |")
ap(f"| 最早作品 | {fmt_ts(oldest['pubdate'])}（{oldest['title'][:30]}） | 最新作品 | {fmt_ts(newest['pubdate'])}（{newest['title'][:30]}） |")
ap(f"| 持续更新 | {span_days:,} 天（约 {span_days / 365:.1f} 年） | 发布频率 | 平均约 {span_days / max(1, len(rows)):.1f} 天一更；有作品月份 {active_months} 个 |")
ap("")
ap(f"> **双平台对照**：{len(BILI_COUNTERPART)}/{len(rows)} 与抖音同款互链（见各作品「平台对照」），{len(rows) - len(BILI_COUNTERPART)} 条 B站独享；判定明细见《双平台作品对比_谭成义.md》。")
ap("")
ap("### 分年份发布量")
ap("")
ap("| 年份 | 作品数 | 占比 |")
ap("|---|---|---|")
for y in year_span:
    ap(f"| {y} | {years[y]} | {years[y] / len(rows) * 100:.1f}% |")
ap("")
ap("### 分区分布")
ap("")
ap("| 分区 | 作品数 | 占比 |")
ap("|---|---|---|")
for t, c in tnames.most_common():
    ap(f"| {t} | {c} | {c / len(rows) * 100:.1f}% |")
ap("")

ap("### 播放量 Top 10")
ap("")
ap(rank_table(top_view, "view", "播放"))
ap("")
ap("### 点赞 Top 10")
ap("")
ap(rank_table(top_like, "like", "点赞"))
ap("")
ap("### 收藏 Top 10")
ap("")
ap(rank_table(top_fav, "favorite", "收藏"))
ap("")

tag_lines = "、".join(f"`{t}`×{c}" for t, c in tag_counter.most_common(30))
ap("### 高频标签 Top 30")
ap("")
ap(tag_lines if tag_lines else "（无标签数据）")
ap("")
extra = []
if multi_p:
    extra.append(f"- 多P视频 {len(multi_p)} 个：" + "、".join(f"[{r['title'][:24]}](https://www.bilibili.com/video/{r['bvid']})({r['videos_cnt']}P)" for r in multi_p))
if coop:
    extra.append(f"- 联合投稿 {len(coop)} 个：" + "、".join(f"{r['title'][:24]}（合作者：{'、'.join(s.get('name', '') for s in r['staff'])}）" for r in coop))
if extra:
    ap("### 特别说明")
    ap("")
    L.extend(extra)
    ap("")
ap("---")
ap("")
ap("## 三、全量作品明细总表（按发布时间倒序）")
ap("")
ap("| # | 发布日期 | 标题 | 分区 | 时长 | 播放 | 弹幕 | 评论 | 点赞 | 投币 | 收藏 | 转发 |")
ap("|---|---|---|---|---|---|---|---|---|---|---|---|")
for i, r in enumerate(rows, 1):
    ap(f"| {i} | {date_of(r['pubdate'])} | [{r['title']}](https://www.bilibili.com/video/{r['bvid']}) "
       f"| {r['tname']} | {r['length_str'] or fmt_dur(r['duration'])} "
       f"| {r['view']:,} | {r['danmaku']:,} | {r['reply']:,} | {r['like']:,} | {r['coin']:,} | {r['favorite']:,} | {r['share']:,} |")
ap("")
ap("---")
ap("")
ap("## 四、逐作品档案（按发布时间倒序）")
ap("")
for i, r in enumerate(rows, 1):
    ap(f"### {i}. {r['title']}")
    ap("")
    ap(f"- **链接**：https://www.bilibili.com/video/{r['bvid']}（BV号：{r['bvid']}，av{r['aid']}）")
    pubdt = datetime.fromtimestamp(int(r["pubdate"]), TZ)
    ap(f"- **发布时间**：{fmt_ts(r['pubdate'])}（{weekday_of(r['pubdate'])}）")
    ap(f"- **时长**：{fmt_dur(r['duration'])}（{r['duration']} 秒）")
    ap(f"- **分区**：{r['tname']}" + (f"（{r['tname_v2']}）" if r["tname_v2"] and r["tname_v2"] != r["tname"] else ""))
    if r["videos_cnt"] > 1 and r["pages"]:
        parts = "；".join(
            f"P{pg.get('page', '')}「{pg.get('part', '')}」{fmt_dur(pg.get('duration', 0))}"
            for pg in r["pages"]
        )
        ap(f"- **分P（共{r['videos_cnt']}P）**：{parts}")
    if r["staff"]:
        names = "、".join(f"{s.get('name', '')}({s.get('title', '')})" for s in r["staff"])
        ap(f"- **联合投稿**：{names}")
    flags = []
    if r["copyright"]:
        flags.append(f"版权属性：{r['copyright']}")
    if r["is_pay"]:
        flags.append("付费视频")
    if r["is_charging"]:
        flags.append("充电专属视频")
    if flags:
        ap(f"- **属性**：{' · '.join(flags)}")
    if r["tags"]:
        ap(f"- **标签**：{'、'.join(r['tags'])}")
    if r["desc"]:
        desc = r["desc"].replace("\n", "  \n  > ")
        ap(f"- **简介**：")
        ap(f"  > {desc}")
    ap(f"- **数据**：播放 {r['view']:,} · 弹幕 {r['danmaku']:,} · 评论 {r['reply']:,} · 点赞 {r['like']:,} · 投币 {r['coin']:,} · 收藏 {r['favorite']:,} · 转发 {r['share']:,}")
    cp = BILI_COUNTERPART.get(r["bvid"])
    if cp:
        ap(f"- **平台对照**：抖音同款 → [{cp[1]}…]({cp[0]})（判定：{cp[2]}）")
    else:
        ap("- **平台对照**：**B站独享**（抖音无同款）")
    ap("")

ap("---")
ap("")
ap("## 附注")
ap("")
ap(f"- 数据抓取于 {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}，为抓取时点的快照，互动数据会随后续时间变化。")
ap("- 原始JSON存档：`data/bilibili/521903482/`（pages/ 列表、details/ 逐视频详情、tags/ 逐视频标签、profile.json 账号资料），CSV 版本：`B站博主_谭成义_作品全量明细.csv`。")
ap(f"- 「{navnum.get('season_num', 0)} 个合集」未逐一展开（合集接口需额外签名），合集内视频均已包含在上面的 197 个作品明细中。")

OUT_MD.write_text("\n".join(L))
print(f"written: {OUT_MD} ({OUT_MD.stat().st_size:,} bytes)")
print(f"written: {OUT_CSV}")
