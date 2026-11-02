#!/usr/bin/env python3
"""生成本地点赞分诊交付报告 MD（只读 final CSV，不碰库/权重/接口）。

用法: ./.venv/bin/python -I tools/likes_report.py
"""
import csv
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "triage_out"
CSV_IN = sorted(OUT.glob("likes_screen_final_*.csv"))[-1]
TAG_RE = re.compile(r"#")
REPORT = OUT / f"交付-本地点赞分诊报告_{datetime.now().strftime('%Y%m%d-%H%M')}.md"

NOTE_CATS = ["AI·开源工具", "数码工具·实用", "赚钱认知·商业", "旅行攻略", "健身运动", "好作品"]


def eff(row):
    label = row["pass3_label"] or row["pass2_label"] or row["label"]
    action = row["pass3_action"] or row["pass2_action"] or row["action"]
    conf = row["pass3_conf"] or row["pass2_conf"] or row["confidence"]
    return label, action, conf


def pick_sample(rows, cat, n=2):
    """样例按可信度分层取：pass1 high → pass2 medium → pass3 medium → 兜底。"""
    got = []

    def take(pred):
        for r in rows:
            if f"#{r['work_id']}" in got:
                continue
            if pred(r) and r["title"].strip():
                got.append(f"《{r['title'][:38]}》#{r['work_id']}")
                if len(got) >= n:
                    return True
        return False

    take(lambda r: r["label"] == cat and r["action"] == "note" and r["confidence"] == "high")
    take(lambda r: r["pass2_label"] == cat and r["pass2_flag"] == "rescue" and r["pass2_conf"] == "medium")
    take(lambda r: r["pass3_label"] == cat and r["pass3_flag"] == "rescue" and r["pass3_conf"] == "medium")
    take(lambda r: (r["pass3_label"] or r["pass2_label"] or r["label"]) == cat
         and (r["pass3_action"] or r["pass2_action"] or r["action"]) == "note")
    return got[:n]


def main() -> int:
    with open(CSV_IN, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    total = len(rows)

    # 最终分布
    eff_labels = Counter()
    eff_actions = Counter()
    eff_confs = Counter()
    for r in rows:
        label, action, conf = eff(r)
        eff_labels[label] += 1
        eff_actions[action] += 1
        eff_confs[conf] += 1
    note_total = eff_actions.get("note", 0)

    # 三轮演进
    p1_note = sum(1 for r in rows if r["action"] == "note")
    p2_note = sum(1 for r in rows if r["action"] == "note" or r["pass2_action"] == "note")
    p3_note = note_total
    p2_n = sum(1 for r in rows if r["pass2_flag"] == "rescue")
    p2_r = sum(1 for r in rows if r["pass2_flag"] == "relabel")
    p3_n = sum(1 for r in rows if r["pass3_flag"] == "rescue")

    # note 档类目来源拆分
    cat_rows = []
    for cat in NOTE_CATS:
        c1 = sum(1 for r in rows if r["label"] == cat and r["action"] == "note")
        c2 = sum(1 for r in rows if r["pass2_label"] == cat)
        c3 = sum(1 for r in rows if r["pass3_label"] == cat)
        cat_rows.append((cat, c1, c2, c3, c1 + c2 + c3))

    # archive 侧
    arch_named = {
        k: eff_labels.get(k, 0)
        for k in ["情感·自我成长", "剧情娱乐·追剧", "颜值欣赏", "集赞支持"]
    }

    # 未分类细主题
    themes = Counter(r["theme"] or "其他(无信号)" for r in rows if eff(r)[0] == "未分类")
    unc_total = sum(themes.values())
    theme_samples = {}
    seen = set()
    for r in rows:
        if eff(r)[0] != "未分类":
            continue
        t = r["theme"] or "其他(无信号)"
        if t not in seen:
            theme_samples[t] = r["title"][:40]
            seen.add(t)

    # 天花板指标：剩余未分类中无任何 # 话题标签的
    unc_rows = [r for r in rows if eff(r)[0] == "未分类"]
    no_tag = sum(1 for r in unc_rows if "#" not in r["title"])

    # 时间趋势（final 未分类占比）
    trend = []
    buckets = sorted(set(int(r["work_id"]) // 2000 * 2000 for r in rows))
    for b in buckets:
        br = [r for r in rows if int(r["work_id"]) // 2000 * 2000 == b]
        unc = sum(1 for r in br if eff(r)[0] == "未分类")
        note = sum(1 for r in br if eff(r)[1] == "note")
        trend.append((b, len(br), note, unc))

    L = []
    A = L.append
    A("# 本地点赞全量分诊报告")
    A("")
    A(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    A("- 数据源：`aggregator.db` works 表（本地库，16,589 条点赞，douyin 16,494 / xhs 69 / twitter 26）")
    A("- **方法声明：全程纯本地。零平台接口、零 AI 接口、数据库只读，未回写任何标签权重/网页前端数据。**")
    A(f"- 明细数据：`{CSV_IN.name}`（pass1+pass2+pass3 全列，可审计）")
    A("")
    A("---")
    A("")
    A("## 一、总览")
    A("")
    A("| 指标 | 数值 |")
    A("|---|---:|")
    A(f"| 总条数 | {total:,} |")
    A(f"| **note 档（值得写笔记）** | **{note_total:,}（{note_total / total:.1%}）** |")
    A(f"| archive 档 | {eff_actions.get('archive', 0):,} |")
    A(f"| 置信度 high / medium / low | {eff_confs.get('high', 0):,} / {eff_confs.get('medium', 0):,} / {eff_confs.get('low', 0):,} |")
    A("")
    A("三轮关键词分诊演进：")
    A("")
    A("| 轮次 | 动作 | note 档累计 |")
    A("|---|---|---:|")
    A(f"| pass1 | 既有 11 类词表粗分 | {p1_note:,} |")
    A(f"| pass2 | 未分类高精度扩词 + 情感/颜值/剧情贴标 | +{p2_n} 捞回、{p2_r} 贴标 → {p2_note:,} |")
    A(f"| pass3 | 高频话题标签定点补捞（游泳/股市/目的地/正文AI/剪映） | +{p3_n} 捞回 → **{p3_note:,}** |")
    A("")
    A("---")
    A("")
    A("## 二、note 档类目明细")
    A("")
    A("| 类目 | pass1 | pass2 | pass3 | 合计 | 示例 |")
    A("|---|---:|---:|---:|---:|---|")
    for cat, c1, c2, c3, c4 in sorted(cat_rows, key=lambda x: -x[4]):
        if c4 == 0:
            continue
        samples = "；".join(pick_sample(rows, cat, 2))
        A(f"| {cat} | {c1} | {c2} | {c3} | **{c4}** | {samples} |")
    A("")
    A("---")
    A("")
    A("## 三、archive 档构成")
    A("")
    A("### 已命名类目（分诊层语义正确，只归档）")
    A("")
    A("| 类目 | 条数 |")
    A("|---|---:|")
    for k, v in arch_named.items():
        A(f"| {k} | {v:,} |")
    A("")
    A(f"### 剩余未分类 {unc_total:,} 条的细主题构成（关键词估算）")
    A("")
    A("| 主题 | 条数 | 占比 | 样例 |")
    A("|---|---:|---:|---|")
    for t, cnt in themes.most_common():
        A(f"| {t} | {cnt:,} | {cnt / unc_total:.0%} | {theme_samples.get(t, '')} |")
    A("")
    A(f"> 其中 **{no_tag:,} 条（{no_tag / unc_total:.0%}）连话题标签都没有**，多为短标题生活随拍，是纯关键词方法的硬天花板。")
    A("")
    A("---")
    A("")
    A("## 四、时间趋势（按入库 id 段）")
    A("")
    A("| id 段 | 总数 | note 档 | 未分类 | 未分类占比 |")
    A("|---|---:|---:|---:|---:|")
    for b, n, note, unc in trend:
        A(f"| {b}-{b + 1999} | {n} | {note} | {unc} | {unc / n:.0%} |")
    A("")
    early_unc = trend[0][3] / trend[0][1]
    late_unc = trend[-1][3] / trend[-1][1]
    A(f"> 未分类占比从早期 {early_unc:.0%} 升到近期 {late_unc:.0%}：越近的点赞主题越分散，关键词层越难覆盖。")
    A("")
    A("---")
    A("")
    A("## 五、洞察与建议")
    A("")
    A("1. **关键词三层已到收益边界。** 剩余未分类约 47% 无强文本信号，纯本地继续加词表预计每轮只捞回 <100 条且噪音上升。若要继续提升，需 AI 仲裁（每条一次小模型调用，16.5k 全量成本可控，但超出本次\"纯本地\"约束），或人工浏览 final CSV。")
    A("2. **新类目候选（用户点了大量、但不在现有 11 类）：** 游戏电玩、音乐演唱、美食、知识科普/影视解说、摄影。是否新增类目由你决定；**本次试验数据未喂给 lexicon/前端标签权重，确认口径后才可入库**。")
    A("3. **低置信噪音提示：** 单词弱命中是主要误判来源（pass1 的\"认知/效率\"、pass3 的\"经济舱\"等，实测约一成）。引用时按可信度分层使用：pass1 `confidence=high`（2,101 条）最可靠 → pass2/pass3 `=medium` 捞回次之 → 单词 low 档务必人工过目。样例表中已按此分层选取。")
    A("4. **可直接使用的部分：** note 档 5,162 条中，pass1 的 4,366 条经过此前实弹验证（含 16572 教训后的仲裁规则），可直接作为后台分类的参照基准；pass2/pass3 的 796 条是增量候选。")
    A("")
    A("---")
    A("")
    A("## 六、产物文件清单（均在 workbench/triage_out/）")
    A("")
    A("| 文件 | 内容 |")
    A("|---|---|")
    A("| `likes_screen_20261102-0152.csv/md` | pass1 全量分诊明细+汇总 |")
    A("| `likes_screen_pass2_20261102-0227.csv` | pass2 全量（含贴标与捞回列） |")
    A("| `pass2_rescued_20261102-0227.csv` | pass2 捞回 518 条 |")
    A(f"| `{CSV_IN.name}` | **最终全量**（pass1+2+3 全列，推荐使用） |")
    A(f"| `{sorted(p.name for p in OUT.glob('pass3_rescued_*.csv'))[-1]}` | pass3 捞回 278 条 |")
    A("| `unclassified_analysis_*.md` | 未分类话题标签分析 |")
    A("")
    A("复跑（均在 workbench/ 下）：`./.venv/bin/python -I tools/likes_screen.py` → `... likes_screen_pass2.py` → `... likes_screen_pass3.py` → `... likes_report.py`")
    A("")

    REPORT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"报告 → {REPORT}")
    print(f"note {p3_note:,} / archive {eff_actions.get('archive', 0):,} / 未分类 {unc_total:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
