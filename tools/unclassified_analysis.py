#!/usr/bin/env python3
"""未分类条目主题分析（只读 pass1 CSV，不碰库/权重）。

统计未分类标题里的 #话题标签 高频词，对照 note 档，摸清 61.4% 盲区构成。
用法: ./.venv/bin/python -I tools/unclassified_analysis.py
"""
import csv
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "triage_out"

csvs = sorted(OUT.glob("likes_screen_*.csv"))
if not csvs:
    print("找不到 pass1 CSV")
    sys.exit(1)
CSV_PATH = csvs[-1]

TAG_RE = re.compile(r"#([\w\u4e00-\u9fff·A-Za-z0-9]+)")


def load():
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def top_tags(rows, n=50):
    c = Counter()
    for r in rows:
        c.update(t for t in TAG_RE.findall(r["title"]) if len(t) >= 2)
    return c.most_common(n)


def main() -> int:
    rows = load()
    unc = [r for r in rows if r["label"] == "未分类"]
    note = [r for r in rows if r["action"] == "note"]
    print(f"数据源: {CSV_PATH.name}，未分类 {len(unc)} 条，note 档 {len(note)} 条\n")

    print("=== 未分类 Top50 话题标签 ===")
    for tag, cnt in top_tags(unc):
        print(f"{cnt:>5}  #{tag}")

    print("\n=== 未分类按 id 段分布（入库顺序≈时间线）===")
    buckets = Counter(int(r["work_id"]) // 2000 * 2000 for r in unc)
    total_by_bucket = Counter(int(r["work_id"]) // 2000 * 2000 for r in rows)
    for b in sorted(total_by_bucket):
        print(f"id {b:>5}-{b + 1999:>5}: 未分类 {buckets.get(b, 0):>5} / 总 {total_by_bucket[b]:>5}")

    lines = [
        "# 未分类主题分析（只读，基于 pass1 CSV）",
        "",
        f"- 数据源：`{CSV_PATH.name}`，未分类 {len(unc)} 条",
        "",
        "## 未分类 Top50 话题标签",
        "",
        "| 标签 | 次数 |",
        "|---|---:|",
    ]
    for tag, cnt in top_tags(unc):
        lines.append(f"| #{tag} | {cnt} |")
    md = OUT / f"unclassified_analysis_{CSV_PATH.stem.split('_')[-1]}.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n汇总 → {md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
