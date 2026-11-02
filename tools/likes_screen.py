#!/usr/bin/env python3
"""全量本地点赞分诊筛选（试验用途，只读，不回写任何库表/标签权重）。

只做三件事：读 works 表 → monitor.triage.classify 纯本地分诊 → 结果落 triage_out/。
不写 aggregator.db，不写 lexicon，不触发任何接口。

用法: ./.venv/bin/python tools/likes_screen.py
"""
import csv
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitor.triage import _read_transcript_text, classify  # noqa: E402
from monitor.store.shared_db import SessionLocal, settings  # noqa: E402
from app.models import Work  # noqa: E402
from sqlalchemy import select  # noqa: E402

OUT = ROOT / "triage_out"
OUT.mkdir(exist_ok=True)
TS = datetime.now().strftime("%Y%m%d-%H%M")
CSV_PATH = OUT / f"likes_screen_{TS}.csv"
MD_PATH = OUT / f"likes_screen_{TS}.md"


def main() -> int:
    with SessionLocal() as db:
        works = db.execute(select(Work).order_by(Work.id.asc())).scalars().all()

    rows = []
    for w in works:
        transcript = _read_transcript_text(settings.media_root, w.transcript_path)
        r = classify(w.title or "", transcript_text=transcript)
        rows.append(
            {
                "work_id": w.id,
                "platform": w.platform,
                "source_type": w.source_type,
                "status": w.status,
                "category": r.category,
                "label": r.label,
                "confidence": r.confidence,
                "tier": r.tier,
                "action": r.action,
                "matched": "|".join(r.matched),
                "title": (w.title or "").replace("\n", " ").strip(),
                "url": w.url or "",
            }
        )

    fields = list(rows[0].keys())
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    by_label = Counter(r["label"] for r in rows)
    by_cat = Counter(r["category"] for r in rows)
    by_action = Counter(r["action"] for r in rows)
    by_conf = Counter(r["confidence"] for r in rows)
    note_by_cat = Counter(r["category"] for r in rows if r["action"] == "note")
    total = len(rows)

    lines = [
        f"# 本地点赞全量分诊筛选（试验，只读不回写）",
        "",
        f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 数据源：aggregator.db works 表（本地），共 {total} 条",
        f"- 明细：`{CSV_PATH.name}`",
        f"- 动作分布：note {by_action.get('note', 0)} / archive {by_action.get('archive', 0)}",
        f"- 置信度：{dict(by_conf.most_common())}",
        "",
        "## 类目分布（label · note 档数 / 总数）",
        "",
        "| label | 总数 | note 档 | 占比 |",
        "|---|---:|---:|---:|",
    ]
    for label, cnt in by_label.most_common():
        note_cnt = note_by_cat.get(
            next(r["category"] for r in rows if r["label"] == label), 0
        )
        lines.append(f"| {label} | {cnt} | {note_cnt} | {cnt / total:.1%} |")
    lines += [
        "",
        f"## 未分类示例（最多 20 条）",
        "",
    ]
    for r in rows:
        if r["label"] == "未分类":
            lines.append(f"- #{r['work_id']} {r['title'][:60]}")
            if sum(1 for l in lines if l.startswith("- #")) >= 20:
                break

    MD_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"共 {total} 条 → {CSV_PATH}")
    print(f"note {by_action.get('note', 0)} / archive {by_action.get('archive', 0)}")
    for label, cnt in by_label.most_common():
        print(f"  {label}: {cnt}")
    print(f"汇总 → {MD_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
