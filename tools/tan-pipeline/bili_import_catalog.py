"""任务1b+2前置：全量目录回填 aggregator.db（B站197+抖音174，upsert 认领不重复）
+ 双平台选片（点赞 Top N ∪ 收藏 Top N，去重）→ data/pipeline_selection.json。

安全默认（2026-11-03 起）：
- 无参数 = dry-run：只打印双平台选片计划，不写 pipeline_selection.json、不执行目录回填。
- 写盘需显式：--platform {bilibili,douyin,all} + --write（--top N 可选，默认 10）；
  写前自动把现有 pipeline_selection.json 备份为 *.bak-<UTC时间戳>（不覆盖旧备份），
  合并而非覆盖（未在本次重算的平台保留文件中原有条目，旧条目补 platform 字段）。
- 目录回填（upsert 写库）需显式 --import-catalog，默认不执行。
- 抖音图文作品（aweme 含 images）默认不参选，仅在计划中标注；
  --include-images 时与视频同口径参选并以 content_type=image 标注。

pipeline_runner.py 消费结构：SELECTION["selected"] 列表，元素需含 work_id/title/reason；
本脚本为每个条目补 platform 字段以区分平台，其余多余字段 runner 会忽略。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from tan_paths import DATA_ROOT, WORKBENCH_ROOT, inject_syspath  # noqa: E402

WS = DATA_ROOT  # 任务数据目录（含 data/ 的工作区根，TAN_DATA_DIR 可覆盖）
ROOT = WORKBENCH_ROOT  # workbench 仓库根（向上探测，TAN_WORKBENCH 可覆盖）
inject_syspath()

from app.models import Work  # noqa: E402
from app.repositories import work_repo  # noqa: E402
from monitor.platforms.douyin_content import infer_content_type  # noqa: E402
from monitor.store.shared_db import SessionLocal, init_db  # noqa: E402

DATA = WS / "data"
SELECTION_OUT = WS / "data" / "pipeline_selection.json"
BILI_CREATOR_ID = 3
DOUYIN_CREATOR_ID = 4

BILI_DETAILS = DATA / "bilibili" / "521903482" / "details"
DOUYIN_PAGES = DATA / "douyin" / "tanchengyi_822069263606973" / "pages"


def epoch_iso(ts) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def import_bilibili(db) -> int:
    n = 0
    for p in sorted(BILI_DETAILS.glob("BV*.json")):
        d = json.loads(p.read_text())
        if d.get("code") != 0:
            continue
        det = d["data"]
        fields = {
            "title": det.get("title") or p.stem,
            "url": f"https://www.bilibili.com/video/{p.stem}",
            "creator_id": BILI_CREATOR_ID,
            "source_type": "monitor",
            "content_type": "video",
            "cover_url": det.get("pic"),
            "published_at": epoch_iso(det.get("pubdate") or 0),
            "published_time_source": "bilibili_view_api_pubdate",
            "published_time_precision": "second",
            "published_time_timezone": "UTC",
        }
        work = work_repo.upsert(db, "bilibili", p.stem, **fields)
        if work.status == "NEW":
            pass  # 目录记录保持 NEW：不建 parse 任务，只有被显式选中的作品才进流水线
        n += 1
    return n


def import_douyin(db) -> int:
    n = 0
    for aid, a in _douyin_awemes().items():
        fields = {
            "title": (a.get("desc") or aid).strip(),
            "url": f"https://www.douyin.com/video/{aid}",
            "creator_id": DOUYIN_CREATOR_ID,
            "source_type": "monitor",
            "content_type": infer_content_type(a),
            "cover_url": ((a.get("video") or {}).get("cover") or {}).get("url_list", [None])[0],
            "published_at": epoch_iso(a.get("create_time") or 0),
            "published_time_source": "douyin_api_createtime",
            "published_time_precision": "second",
            "published_time_timezone": "UTC",
        }
        work_repo.upsert(db, "douyin", aid, **fields)
        n += 1
    return n


# ---------- 选片（双平台同一口径：点赞 Top N ∪ 收藏 Top N，去重） ----------

def _top_union(entries: list[dict], top: int) -> dict[str, dict]:
    """收藏 Top N ∪ 点赞 Top N 去重（纯函数）。

    entries: [{"key", "like", "favorite"}]；返回 key -> {"reason", "favorite", "like"}。
    与旧 B 站实现同口径：按指标倒序各取前 N，命中双榜的合并 reason。
    """
    by_fav = sorted(entries, key=lambda e: e["favorite"], reverse=True)[:top]
    by_like = sorted(entries, key=lambda e: e["like"], reverse=True)[:top]
    union: dict[str, dict] = {}
    for rank, e in enumerate(by_fav, 1):
        union[e["key"]] = {"reason": f"收藏榜第{rank}", "favorite": e["favorite"], "like": e["like"]}
    for rank, e in enumerate(by_like, 1):
        if e["key"] in union:
            union[e["key"]]["reason"] += f" + 点赞榜第{rank}"
        else:
            union[e["key"]] = {"reason": f"点赞榜第{rank}", "favorite": e["favorite"], "like": e["like"]}
    return union


def _bili_details(details_dir: Path = BILI_DETAILS) -> dict[str, dict]:
    details: dict[str, dict] = {}
    for p in sorted(details_dir.glob("BV*.json")):
        d = json.loads(p.read_text())
        if d.get("code") == 0:
            details[p.stem] = d["data"]
    return details


def _douyin_awemes(pages_dir: Path = DOUYIN_PAGES) -> dict[str, dict]:
    """读全部分页 JSON，按 aweme_id 去重（保持首见顺序）。"""
    ids: dict[str, dict] = {}
    for p in sorted(pages_dir.glob("page_*.json")):
        for a in json.loads(p.read_text())["aweme_list"]:
            if a.get("aweme_id") and a["aweme_id"] not in ids:
                ids[a["aweme_id"]] = a
    return ids


def _douyin_stat(aweme: dict, name: str) -> int:
    return int((aweme.get("statistics") or {}).get(name) or 0)


def select_top(db, top: int = 10, details_dir: Path = BILI_DETAILS) -> dict:
    """B 站选片：stat.favorite（收藏）∪ stat.like（点赞）Top N。"""
    details = _bili_details(details_dir)
    entries = [
        {
            "key": bvid,
            "like": (d.get("stat") or {}).get("like", 0),
            "favorite": (d.get("stat") or {}).get("favorite", 0),
        }
        for bvid, d in details.items()
    ]
    union = _top_union(entries, top)

    items = []
    for bvid, meta in union.items():
        w = work_repo.get_by_platform_external(db, "bilibili", bvid)
        items.append({
            "work_id": w.id if w else None,
            "platform": "bilibili",
            "bvid": bvid,
            "title": details[bvid]["title"],
            "reason": meta["reason"],
            "favorite": meta["favorite"],
            "like": meta["like"],
            "duration_sec": details[bvid].get("duration", 0),
            "content_type": "video",
        })
    missing = [i for i in items if i["work_id"] is None]
    return {"selected": items, "missing": missing}


def select_top_douyin(db, top: int = 10, include_images: bool = False,
                      pages_dir: Path = DOUYIN_PAGES) -> dict:
    """抖音选片：statistics.collect_count（收藏）∪ statistics.digg_count（点赞）Top N。

    图文作品（aweme 含 images）默认不参选，收进 skipped_images 标注；
    include_images=True 时与视频同口径参选并以 content_type 区分。
    """
    awemes = _douyin_awemes(pages_dir)
    videos = [(aid, a) for aid, a in awemes.items() if infer_content_type(a) == "video"]
    images = [(aid, a) for aid, a in awemes.items() if infer_content_type(a) == "image"]
    pool = videos + images if include_images else videos

    entries = [
        {"key": aid, "like": _douyin_stat(a, "digg_count"), "favorite": _douyin_stat(a, "collect_count")}
        for aid, a in pool
    ]
    union = _top_union(entries, top)
    by_id = dict(pool)

    items = []
    for aid, meta in union.items():
        a = by_id[aid]
        w = work_repo.get_by_platform_external(db, "douyin", aid)
        items.append({
            "work_id": w.id if w else None,
            "platform": "douyin",
            "aweme_id": aid,
            "title": (a.get("desc") or aid).strip(),
            "reason": meta["reason"],
            "favorite": meta["favorite"],  # 抖音 collect_count
            "like": meta["like"],          # 抖音 digg_count
            "duration_sec": int((a.get("video") or {}).get("duration") or 0) // 1000,
            "content_type": infer_content_type(a),
        })
    missing = [i for i in items if i["work_id"] is None]
    skipped_images = [
        {
            "aweme_id": aid,
            "title": (a.get("desc") or aid).strip()[:60],
            "favorite": _douyin_stat(a, "collect_count"),
            "like": _douyin_stat(a, "digg_count"),
            "images_count": len(a.get("images") or []),
            "note": "图文作品，默认不参选（--include-images 才按同口径选入）",
        }
        for aid, a in images
    ]
    return {"selected": items, "missing": missing, "skipped_images": skipped_images}


# ---------- 合并 / 备份（写盘路径才用） ----------

def merge_selection(existing: dict | None, parts: dict[str, dict], top: int) -> dict:
    """合并而非覆盖：本次未重算的平台，原样保留文件中的旧条目（补 platform 字段）。

    旧版文件条目无 platform 字段，一律视为 bilibili（历史只有 B 站选片）。
    """
    merged: list[dict] = []
    replaced = set(parts)
    for item in (existing or {}).get("selected", []):
        platform = item.get("platform") or "bilibili"
        if platform in replaced:
            continue  # 该平台本次已重算，旧条目由新结果替代
        kept = dict(item)
        kept["platform"] = platform
        merged.append(kept)
    missing: list[dict] = []
    for platform, part in parts.items():
        merged.extend(part["selected"])
        missing.extend(part.get("missing") or [])
    out: dict = {
        "selected": merged,
        "missing": missing,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "criteria": f"点赞 Top {top} ∪ 收藏 Top {top} 去重（B站 stat.like/stat.favorite；抖音 statistics.digg_count/collect_count）",
    }
    for platform, part in parts.items():
        if part.get("skipped_images"):
            out[f"skipped_images_{platform}"] = part["skipped_images"]
    return out


def backup_selection(path: Path = SELECTION_OUT) -> Path | None:
    """写前备份：存在则复制为 <文件名>.bak-<UTC时间戳>，同名加序号，绝不覆盖旧备份。"""
    if not path.exists():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak-{ts}")
    n = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{ts}-{n}")
        n += 1
    shutil.copy2(path, backup)
    return backup


def _print_plan(platform: str, part: dict, top: int) -> None:
    print(f"[{platform}] top={top} 计划选中 {len(part['selected'])} 条，missing={len(part.get('missing') or [])}")
    for i in part["selected"]:
        marker = " [图文]" if i.get("content_type") == "image" else ""
        print(f"  #{i['work_id']} {i['reason']} 藏{i['favorite']} 赞{i['like']} {i.get('duration_sec', 0)}s{marker} {i['title'][:24]}")
    for s in part.get("skipped_images") or []:
        print(f"  [跳过-图文] 藏{s['favorite']} 赞{s['like']} 图x{s['images_count']} {s['title'][:24]}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="双平台目录回填 + 选片（默认 dry-run，不写文件不写库）")
    parser.add_argument("--platform", choices=["bilibili", "douyin", "all"], default=None,
                        help="选片平台；缺省 dry-run 双平台。--write 时必须显式给出")
    parser.add_argument("--top", type=int, default=10, help="每榜取前 N（默认 10）")
    parser.add_argument("--write", action="store_true",
                        help="实际写入 pipeline_selection.json（写前自动备份，合并不覆盖）")
    parser.add_argument("--include-images", action="store_true",
                        help="抖音图文作品同口径参选（默认仅标注不选入）")
    parser.add_argument("--import-catalog", action="store_true",
                        help="执行目录回填（upsert 写库；默认不执行）")
    args = parser.parse_args(argv)

    if args.write and args.platform is None:
        parser.error("--write 必须显式指定 --platform（bilibili/douyin/all）")

    init_db()
    with SessionLocal() as db:
        if args.import_catalog:
            nb = import_bilibili(db)
            nd = import_douyin(db)
            db.commit()
            print(f"catalog upserted: bilibili={nb} douyin={nd}")

        total = {
            "bilibili": db.query(Work).filter(Work.platform == "bilibili").count(),
            "douyin": db.query(Work).filter(Work.platform == "douyin").count(),
        }
        print("db totals:", total)

        platforms = [args.platform] if args.platform else ["bilibili", "douyin"]
        if platforms == ["all"]:
            platforms = ["bilibili", "douyin"]
        parts: dict[str, dict] = {}
        for platform in platforms:
            if platform == "bilibili":
                parts[platform] = select_top(db, top=args.top)
            else:
                parts[platform] = select_top_douyin(db, top=args.top, include_images=args.include_images)
            _print_plan(platform, parts[platform], args.top)

        if not args.write:
            print("DRY-RUN：未写入 pipeline_selection.json（显式 --platform ... --write 才写，写前自动备份）")
            return

        existing = json.loads(SELECTION_OUT.read_text()) if SELECTION_OUT.exists() else None
        merged = merge_selection(existing, parts, args.top)
        kept = len(merged["selected"]) - sum(len(p["selected"]) for p in parts.values())
        backup = backup_selection(SELECTION_OUT)
        if backup is not None:
            print(f"已备份原文件 → {backup.name}")
        SELECTION_OUT.write_text(json.dumps(merged, ensure_ascii=False, indent=1))
        print(f"selection written: {len(merged['selected'])} works（保留旧条目 {kept} 条）, missing={len(merged['missing'])}")


if __name__ == "__main__":
    main()
