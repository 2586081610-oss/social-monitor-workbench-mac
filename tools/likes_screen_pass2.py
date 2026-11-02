#!/usr/bin/env python3
"""第二遍本地仲裁（只读 pass1 CSV，不碰库/权重/接口）。

仅对 pass1 未分类条目做三件事：
1. 高精度扩词捞回 note 档（money/ai/digital/travel/fitness，不做好作品——关键词抓不住）；
2. 情感/颜值/剧情贴更准的标（动作仍是 archive）；
3. 剩余未分类按话题标签归粗主题，出构成报告。

输出：全量 pass2 CSV + 捞回明细 CSV + 汇总 MD。全程无任何回写。
用法: ./.venv/bin/python -I tools/likes_screen_pass2.py
"""
import csv
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "triage_out"
csvs = sorted(OUT.glob("likes_screen_*.csv"))
CSV_IN = [p for p in csvs if "pass2" not in p.name][-1]
TS = datetime.now().strftime("%Y%m%d-%H%M")
CSV_FULL = OUT / f"likes_screen_pass2_{TS}.csv"
CSV_RESCUED = OUT / f"pass2_rescued_{TS}.csv"
MD_PATH = OUT / f"pass2_summary_{TS}.md"

# --- 捞回规则（按顺序取第一个命中的类目；故意保守，宁缺毋滥）---
RESCUE = [
    (
        "money",
        re.compile(
            r"财经|股票|炒股|基金|理财|投资|副业|创业|生意|变现|营收|复利|上市|估值"
            r"|经济学|商业|穷人|富人|赚钱|搞钱|存款|巴菲特|芒格"
        ),
    ),
    (
        "ai_tools",
        re.compile(
            r"\bAI\b|人工智能|大模型|ChatGPT|\bGPT\b|Claude|DeepSeek|开源|GitHub"
            r"|逆向|破解|智能体|\bAgent\b|提示词|\bPrompt\b|算法|机器学习|深度学习"
            r"|\bLLM\b|Ollama|\bAGI\b",
            re.IGNORECASE,
        ),
    ),
    (
        "digital_tools",
        re.compile(
            r"大疆|DJI|iPhone|iPad|Mac\b|MacBook|华为|小米|OPPO|vivo|安卓|Android"
            r"|Windows|电脑|笔记本|软件|APP|App|效率|网盘|下载|浏览器|键盘|鼠标"
            r"|耳机|音箱|无人机|显示器|显卡|硬盘|SSD|U盘|充电|电池|数码|平板|路由器",
            re.IGNORECASE,
        ),
    ),
    (
        "travel",
        re.compile(
            r"攻略|自驾|徒步|露营|机票|签证|行程|路书|自由行|旅行|旅游|穷游|青旅"
            r"|民宿|景点|景区|行李"
        ),
    ),
    (
        "fitness",
        re.compile(
            r"卧推|硬拉|引体|俯卧撑|撸铁|体态|圆肩|骨盆前倾|帕梅拉|刘畊宏|力量训练"
            r"|增肌|减脂|有氧|无氧|跑姿|配速|深蹲|马拉松|健身|健身环|跳绳"
        ),
    ),
]
CATEGORY_LABEL = {
    "money": "赚钱认知·商业",
    "ai_tools": "AI·开源工具",
    "digital_tools": "数码工具·实用",
    "travel": "旅行攻略",
    "fitness": "健身运动",
}

# 游戏词命中时"攻略"不算旅行
GAME_RE = re.compile(r"游戏|云顶|金铲铲|三角洲|steam|英雄联盟|原神|王者|吃鸡|Steam", re.IGNORECASE)

# --- 贴标规则（动作保持 archive，只为构成更准）---
RELABEL = [
    ("emotion", re.compile(r"恋爱|情侣|爱情|分手|相亲|婚姻|前任|暗恋|表白|脱单|暧昧|失恋")),
    ("beauty", re.compile(r"穿搭|[oO][oO][tT][dD]|发型|化妆|护肤|颜值|帅哥|美女|妆容|染发|烫头")),
    ("drama", re.compile(r"短剧|追剧|剧情|影视|电影|解说|电视剧|番剧|动漫")),
]
RELABEL_LABEL = {"emotion": "情感·自我成长", "beauty": "颜值欣赏", "drama": "剧情娱乐·追剧"}

# --- 剩余未分类的粗主题（仅报告用）---
THEME_TAGS = {
    "美食": re.compile(r"美食|家常菜|做菜|菜谱|教程.*饭|好吃|探店|烘焙|蛋糕|火锅|烧烤|早餐|晚餐"),
    "游戏": GAME_RE,
    "音乐": re.compile(r"陶喆|周杰伦|吉他|翻唱|说唱|音乐|钢琴|唱歌|演唱会|前奏|编曲"),
    "摄影拍照": re.compile(r"摄影|拍照|相机|构图|修图|镜头|像素"),
    "穿搭发型": re.compile(r"穿搭|ootd|发型|化妆|护肤", re.IGNORECASE),
    "知识科普": re.compile(r"科普|涨知识|冷知识|知识|历史|哲学|心理学|英语|学习|大学|读书"),
    "情感生活": re.compile(r"情侣|爱情|恋爱|生活|日常|vlog|大学生|校园|妈妈|爸|家", re.IGNORECASE),
}
TAG_RE = re.compile(r"#([\w\u4e00-\u9fff·A-Za-z0-9]+)")


def theme_of(title: str) -> str:
    for theme, pat in THEME_TAGS.items():
        if pat.search(title):
            return theme
    return "其他"


def main() -> int:
    with open(CSV_IN, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    rescued, relabeled = [], []
    for r in rows:
        r["pass2_flag"] = r["pass2_matched"] = r["pass2_label"] = r["pass2_action"] = r["pass2_conf"] = ""
        if r["label"] != "未分类":
            continue
        title = r["title"]
        for cat, pat in RESCUE:
            hits = pat.findall(title)
            if not hits:
                continue
            if cat == "travel" and GAME_RE.search(title) and "攻略" in hits:
                continue  # 游戏攻略不是旅行攻略
            n_hits = len(set(h.lower() for h in hits))
            r["pass2_flag"] = "rescue"
            r["pass2_label"] = CATEGORY_LABEL[cat]
            r["pass2_action"] = "note"
            r["pass2_conf"] = "medium" if n_hits >= 2 else "low"
            r["pass2_matched"] = "|".join(sorted(set(hits)))
            rescued.append(r)
            break
        if r["pass2_flag"] == "rescue":
            continue
        for cat, pat in RELABEL:
            hits = pat.findall(title)
            if hits:
                r["pass2_flag"] = "relabel"
                r["pass2_label"] = RELABEL_LABEL[cat]
                r["pass2_action"] = "archive"
                r["pass2_conf"] = "low"
                r["pass2_matched"] = "|".join(sorted(set(hits))[:3])
                relabeled.append(r)
                break

    # 全量写出（pass1 列 + pass2 列）
    fields = list(rows[0].keys())
    with open(CSV_FULL, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    rescued_fields = fields + []
    with open(CSV_RESCUED, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=rescued_fields)
        w.writeheader()
        w.writerows(rescued)

    # 统计
    n_total = len(rows)
    by_cat = Counter(r["pass2_label"] for r in rescued)
    by_conf = Counter(r["pass2_conf"] for r in rescued)
    relabel_by = Counter(r["pass2_label"] for r in relabeled)
    remain = [r for r in rows if r["label"] == "未分类" and not r["pass2_flag"]]
    themes = Counter(theme_of(r["title"]) for r in remain)
    note_after = sum(1 for r in rows if r["action"] == "note" or r["pass2_action"] == "note")

    lines = [
        "# 第二遍本地仲裁汇总（只读，不回写）",
        "",
        f"- 输入：`{CSV_IN.name}`（{n_total} 条，未分类 {sum(1 for r in rows if r['label'] == '未分类')} 条）",
        f"- 捞回 note 档：**{len(rescued)}** 条（置信 medium {by_conf.get('medium', 0)} / low {by_conf.get('low', 0)}）",
        f"- note 档总数：{sum(1 for r in rows if r['action'] == 'note')} → {note_after}",
        f"- 更准贴标（动作仍 archive）：{len(relabeled)} 条 {dict(relabel_by.most_common())}",
        f"- 仍为未分类：{len(remain)} 条",
        "",
        "## 捞回类目分布",
        "",
        "| 类目 | 条数 |",
        "|---|---:|",
    ]
    for cat, cnt in by_cat.most_common():
        lines.append(f"| {cat} | {cnt} |")
    lines += ["", "## 剩余未分类的粗主题构成", "", "| 主题 | 条数 | 占剩余 |", "|---|---:|---:|"]
    for t, cnt in themes.most_common():
        lines.append(f"| {t} | {cnt} | {cnt / len(remain):.0%} |")
    MD_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"输入: {CSV_IN.name}")
    print(f"捞回 note 档 {len(rescued)} 条: {dict(by_cat.most_common())}")
    print(f"贴标 {len(relabeled)} 条: {dict(relabel_by.most_common())}")
    print(f"剩余未分类 {len(remain)} 条，主题构成: {dict(themes.most_common())}")
    print(f"note 档 {sum(1 for r in rows if r['action'] == 'note')} → {note_after}")
    print(f"\n捞回样例（前 15）:")
    for r in rescued[:15]:
        print(f"  #{r['work_id']} [{r['pass2_label']}] {r['pass2_matched']} ← {r['title'][:40]}")
    print(f"\n全量 → {CSV_FULL.name} / 捞回 → {CSV_RESCUED.name} / 汇总 → {MD_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
