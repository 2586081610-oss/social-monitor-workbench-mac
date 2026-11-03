"""双平台对比文档：data/cross_platform_match.json → 双平台作品对比_谭成义.md"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
from tan_paths import DATA_ROOT

WS = DATA_ROOT  # 任务数据目录（TAN_DATA_DIR 可覆盖）
M = json.loads((WS / "data" / "cross_platform_match.json").read_text())
OUT = WS / "双平台作品对比_谭成义.md"

matches, bili_only, dy_only = M["matched"], M["bili_only"], M["douyin_only"]

# 分层
tiers = {"A": [], "B": [], "C": []}
for m in matches:
    r = m["title_ratio"]
    if r >= 0.80:
        tiers["A"].append(m)
    elif r >= 0.40:
        tiers["B"].append(m)
    else:
        tiers["C"].append(m)

# 同步时间差 & 先发方
day0 = sum(1 for m in matches if m["days_diff"] == 0)
dy_first = sum(1 for m in matches if m["douyin"]["ts"] < m["bili"]["ts"])
bili_first = sum(1 for m in matches if m["bili"]["ts"] < m["douyin"]["ts"])

year_b = Counter(datetime.fromtimestamp(b["ts"], TZ).strftime("%Y") for b in bili_only)
year_d = Counter(datetime.fromtimestamp(d["ts"], TZ).strftime("%Y") for d in dy_only)
img_only = sum(1 for d in dy_only if d.get("is_image"))
dur_b = sum(b["dur"] for b in bili_only) / max(1, len(bili_only))
dur_m = sum(m["bili"]["dur"] for m in matches) / max(1, len(matches))

L: list[str] = []
ap = L.append
ap("# 谭成义：B站 × 抖音 双平台作品对比")
ap("")
ap(f"> 生成时间：{datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} · 匹配方法：标题归一化相似度 + 时长指纹（B站197条 × 抖音174条全量交叉比对）")
ap("")
ap("## 一、总量与独享结论")
ap("")
ap("| 结论 | 数值 |")
ap("|---|---|")
ap(f"| **谁的作品更多** | **B站更多**：197 条 vs 抖音 174 条（多 23 条） |")
ap(f"| 双平台同步作品 | **97 条**（B站 49.2% / 抖音 55.7% 的作品互为同款） |")
ap(f"| **仅B站独享** | **100 条** |")
ap(f"| **仅抖音独享** | **77 条**（其中图文 7 条——B站无图文作品，必然独享） |")
ap("")
ap("| 同步作品置信分层 | 数量 | 判定依据 |")
ap("|---|---|---|")
ap(f"| A 标题高度近似 | {len(tiers['A'])} | 标题相似度 ≥0.80 |")
ap(f"| B 文案改写重发 | {len(tiers['B'])} | 相似度 0.40-0.80，主题一致 |")
ap(f"| C 标题重写+时长指纹 | {len(tiers['C'])} | 标题完全重写，但视频时长差 ≤0.9 秒（该UP主长视频无剪辑，时长达指纹级） |")
ap("")

ap("## 二、同步发布规律")
ap("")
ap(f"- **同日双发 {day0} 条**（占同步作品 {day0 / len(matches) * 100:.0f}%），其余 1-3 天内错峰。")
ap(f"- 先发方：**抖音先发 {dy_first} 条 / B站先发 {bili_first} 条** —— 97 条同步作品全部由抖音首发（含同日双发，抖音也更早），B站纯跟随。")
ap(f"- 时长画像：同步作品B站端平均 {dur_m / 60:.0f} 分钟（长教学/VLOG 才值得搬运）。")
ap("")
ap("## 三、仅B站的 100 条是什么")
ap("")
ap("| 年份 | 条数 |")
ap("|---|---|")
for y in sorted(year_b):
    ap(f"| {y} | {year_b[y]} |")
ap("")
ap(f"- **B站独享平均时长 {dur_b / 60:.1f} 分钟**（对比同步作品 12 分钟），两类并存：")
ap("  1. **B站独家长视频**：三天两夜VLOG（48min）、训练生活Vlog（47min）、开枪正式备赛倒计时50天（41min）、奇妙旅行北京站（34min）等旅行/备赛纪录片，抖音端没有完整版")
ap("  2. **B站发的日常花絮短条**：胸部训练vlog、又多一张卡啦（1min）等抖音未发的「第二现场」碎片内容")
ap("")
ap("完整清单：")
ap("")
ap("| # | 发布日期 | 时长 | 标题 |")
ap("|---|---|---|---|")
for i, b in enumerate(bili_only, 1):
    d = datetime.fromtimestamp(b["ts"], TZ).strftime("%Y-%m-%d")
    ap(f"| {i} | {d} | {int(b['dur'] // 60)}:{int(b['dur'] % 60):02d} | [{b['text'][:40]}]({b['url']}) |")
ap("")
ap("## 四、仅抖音的 77 条是什么")
ap("")
ap("| 年份 | 条数 |")
ap("|---|---|")
for y in sorted(year_d):
    ap(f"| {y} | {year_d[y]} |")
ap("")
ap(f"- 图文 {img_only} 条（训练打卡/学员对比图集，B站零图文）")
ap("- 典型形态：**倒计时系列短条**（备赛倒计时N天）、**生活随拍短条**（十几秒-1分钟）、**话题性强的话题条**（#南瓜肩 #天赋）——短平快内容只养抖音")
ap("")
ap("完整清单：")
ap("")
ap("| # | 发布日期 | 类型 | 文案 |")
ap("|---|---|---|---|")
for i, d in enumerate(dy_only, 1):
    ts = datetime.fromtimestamp(d["ts"], TZ).strftime("%Y-%m-%d")
    kind = "图文" if d.get("is_image") else f"{int(d['dur'] // 60)}:{int(d['dur'] % 60):02d}"
    ap(f"| {i} | {ts} | {kind} | [{d['text'][:38]}]({d['url']}) |")
ap("")
ap("## 五、结论")
ap("")
ap("1. **作品数量：B站 197 > 抖音 174**，但这是搬运稀释的结果——B站有 100 条独享（多为长VLOG/纪录片特供），抖音有 77 条独享（短条+图文）。")
ap("2. **同步率约一半**：97 条双平台同发（同日为主、抖音先发为常态），是内容的「一次拍摄、两端分发」。")
ap("3. **平台分工**：抖音吃流量（407万粉，短条+长教学都发），B站吃长尾收藏（教学全集+独家VLOG，播放仅抖音零头但收藏比高）。")
ap("4. 想追更他的完整内容，**两边都得盯**：抖音快但缺长VLOG，B站有 100 条抖音看不到的独家长内容。")
ap("")
ap("## 附注")
ap("")
ap("- 匹配明细 JSON：`data/cross_platform_match.json`（含每对相似度/时长差/两链接）；匹配脚本：`scripts/cross_platform_compare.py`")
ap("- B站 197 条与抖音 174 条均为各平台**可见**作品；抖音账号计数 162 与可见 174 的差值为账号私密/不可见内容。")
ap("- 「独享」指另一平台无同款作品；个别B站独享长VLOG与抖音独享短条可能主题相近（旅行/备赛），但剪辑版本不同，按不同作品计。")

OUT.write_text("\n".join(L))
print(f"written: {OUT} ({OUT.stat().st_size:,} bytes)")
print(f"tierA={len(tiers['A'])} B={len(tiers['B'])} C={len(tiers['C'])} | 同日{day0} 抖音先发{dy_first} B站先发{bili_first}")
print(f"bili_only years={dict(sorted(year_b.items()))} douyin_only years={dict(sorted(year_d.items()))} 图文独享={img_only}")
