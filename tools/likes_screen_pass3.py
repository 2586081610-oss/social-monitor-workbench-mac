#!/usr/bin/env python3
"""第三遍定点补捞（只读 pass2 CSV，不碰库/权重/接口）。

依据「其他」桶高频话题标签的证据，补五类高精度规则：
游泳→健身、股市/交易→赚钱、目的地+场景→旅行、AI紧邻中文→AI、剪映→数码。
剩余未分类打粗主题标签（仅报告用）。
输出：最终全量 CSV + pass3 捞回明细 CSV。
用法: ./.venv/bin/python -I tools/likes_screen_pass3.py
"""
import csv
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "triage_out"
CSV_IN = sorted(OUT.glob("likes_screen_pass2_*.csv"))[-1]
TS = datetime.now().strftime("%Y%m%d-%H%M")
CSV_FULL = OUT / f"likes_screen_final_{TS}.csv"
CSV_P3 = OUT / f"pass3_rescued_{TS}.csv"

CATEGORY_LABEL = {
    "money": "赚钱认知·商业",
    "ai_tools": "AI·开源工具",
    "digital_tools": "数码工具·实用",
    "travel": "旅行攻略",
    "fitness": "健身运动",
}

# pass3 定点补捞（按顺序第一个命中即停）
RESCUE3 = [
    (
        "fitness",
        re.compile(r"游泳|自由泳|蛙泳|蝶泳|仰泳|泳姿|夜跑|晨跑|跑量|跳绳|铁人三项|划水机|动感单车"),
    ),
    (
        "money",
        re.compile(
            r"股市|A股|美股|港股|沪深|K线|止盈|止损|缠论|波段|期货|美联储|通胀|降息|加息"
            r"|交易心得|金融|经济|货币|汇率|债券|国债|证券|操盘|筹码|牛市|熊市",
            re.IGNORECASE,
        ),
    ),
    (
        "travel",
        re.compile(
            r"武功山|香格里拉|阿勒泰|黄山|泰山|华山|峨眉山|稻城亚丁|泸沽湖|洱海|青海湖"
            r"|喀纳斯|伊犁|独库公路|川藏线|318|漠河|长白山|九寨沟|张家界|凤凰古城|荔波|恩施"
        ),
    ),
    (
        "ai_tools",
        re.compile(r"(?-i:AI)(?![a-zA-Z])|AIGC|midjourney|comfyui|stable\s?diffusion", re.IGNORECASE),
    ),
    ("digital_tools", re.compile(r"剪映|达芬奇|final\s?cut|\bPR教程|premiere", re.IGNORECASE)),
]

# 剩余未分类的细主题（仅报告用，按顺序取第一个命中）
THEME3 = [
    ("影视解说", re.compile(r"悬疑|科幻|纪录片|电影|影视|观影|漫威|美剧|动漫|动画|解说|精讲|第一视角|万万没想到|硬核深度|零基础看懂|影娱|影评|剧集|配音|名场面")),
    ("搞笑梗图", re.compile(r"搞笑|抽象|内容过于真实|精神状态|沙雕|离谱|笑死|笑不活|爆笑|梗")),
    ("情感人生", re.compile(r"人生感悟|正能量|励志|遗憾|独白|感悟|精神内耗|mbti|infp|intj|自律|成长|治愈|遗憾|清醒|钝感力", re.IGNORECASE)),
    ("音乐演唱", re.compile(r"陈奕迅|陶喆|周杰伦|伴奏|卡点|翻唱|唱歌|音乐|吉他|钢琴|说唱|歌词|音源|歌曲|前奏|编曲|演唱会|音阶")),
    ("游戏电玩", re.compile(r"无畏契约|lol|英雄联盟|游戏|三角洲|云顶|金铲铲|steam|原神|王者|吃鸡|主机|\bps5\b|xbox|switch", re.IGNORECASE)),
    ("新闻时事", re.compile(r"热点|新闻|吃瓜|热搜|时事|大事件|国际局势|俄乌|中东|美国|日本")),
    ("学习考研", re.compile(r"考研|考公|四六级|刘晓艳|上岸|学习|单词|背书|刷题")),
    ("汽车出行", re.compile(r"买车|汽车|开车|驾驶|油车|电车|新车|提车|驾考|用车")),
    ("宠物萌宠", re.compile(r"猫|狗|宠物|猫咪|狗狗|铲屎|萌宠")),
    ("创作剪辑", re.compile(r"剪辑|视频剪辑|文案|拍摄|运镜|调色|转场|脚本|拍摄花絮")),
    ("知识科普", re.compile(r"科普|涨知识|冷知识|知识|历史|哲学|心理学|物理|化学|生物|宇宙|科学")),
    ("美食吃播", re.compile(r"美食|吃|饭|菜|味道|烹饪|做菜|早餐|晚餐|宵夜|探店")),
    ("生活日常", re.compile(r"日常|生活|vlog|plog|随拍|记录|妈妈|爸|家|朋友|大学生|校园", re.IGNORECASE)),
]
TAG_RE = re.compile(r"#([\w\u4e00-\u9fff·A-Za-z0-9]+)")


def theme3_of(title: str) -> str:
    for t, pat in THEME3:
        if pat.search(title):
            return t
    return "其他"


def main() -> int:
    with open(CSV_IN, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    rescued = []
    for r in rows:
        r["pass3_flag"] = r["pass3_label"] = r["pass3_action"] = r["pass3_matched"] = r["theme"] = ""
        if r["label"] != "未分类" or r["pass2_flag"]:
            continue
        title = r["title"]
        for cat, pat in RESCUE3:
            # AI 主题只看正文：剔除 #话题标签（官方创作活动贴纸不算 AI 主题）
            text = TAG_RE.sub("", title) if cat == "ai_tools" else title
            hits = [h for h in pat.findall(text) if h]
            if not hits:
                continue
            r["pass3_flag"] = "rescue"
            r["pass3_label"] = CATEGORY_LABEL[cat]
            r["pass3_action"] = "note"
            r["pass3_matched"] = "|".join(sorted(set(hits)))
            r["pass3_conf"] = "medium" if len(set(hits)) >= 2 else "low"
            rescued.append(r)
            break

    for r in rows:
        eff_unc = r["label"] == "未分类" and not r["pass2_flag"] and not r["pass3_flag"]
        if eff_unc:
            r["theme"] = theme3_of(r["title"])

    fields = list(rows[0].keys())
    if "pass3_conf" not in fields:
        fields.append("pass3_conf")
    with open(CSV_FULL, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    with open(CSV_P3, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rescued)

    by_cat = Counter(r["pass3_label"] for r in rescued)
    themes = Counter(r["theme"] for r in rows if r["theme"])
    note = sum(
        1
        for r in rows
        if r["action"] == "note" or r["pass2_action"] == "note" or r["pass3_action"] == "note"
    )
    remain = sum(1 for r in rows if not r["theme"] and r["label"] == "未分类")
    print(f"输入: {CSV_IN.name}")
    print(f"pass3 捞回 {len(rescued)} 条: {dict(by_cat.most_common())}")
    print(f"note 档 → {note}")
    print(f"剩余未分类 {remain} 条，细主题: {dict(themes.most_common())}")
    print("样例:")
    for r in rescued[:12]:
        print(f"  #{r['work_id']} [{r['pass3_label']}] {r['pass3_matched']} ← {r['title'][:36]}")
    print(f"\n全量 → {CSV_FULL.name} / 捞回 → {CSV_P3.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
