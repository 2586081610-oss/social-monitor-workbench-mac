"""抽帧新旧策略对照工具：同一批视频分别产出 classic / teaching 帧清单与量化指标。

只写 --out 指定目录（默认 triage_out/frame_strategy_compare），
不覆盖 media/ 下已完成的 frames/ 与 frame_manifest.json，不改数据库。

用法（workbench 根目录）：
  .venv/bin/python tools/frame_strategy_compare.py --work-ids 16591,16613,16743,16628,16608
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor.pipeline import frames  # noqa: E402


def load_works(db_path: Path, work_ids: list[int]) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, title, media_path, transcript_path, frame_manifest_path FROM works "
            "WHERE id IN (%s)" % ",".join(str(int(w)) for w in work_ids)
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": row[0], "title": row[1], "media_path": row[2], "transcript_path": row[3], "manifest": row[4]}
        for row in rows
    ]


def load_lexicon(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT term, action, weight, enabled FROM lexicon WHERE enabled=1"
        ).fetchall()
    finally:
        conn.close()
    return [{"term": r[0], "action": r[1], "weight": r[2], "enabled": bool(r[3])} for r in rows]


def load_segments(media_root: Path, transcript_rel: str | None) -> list[dict]:
    if not transcript_rel:
        return []
    path = media_root / transcript_rel
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    segments = payload.get("segments")
    return segments if isinstance(segments, list) else []


def _occupancy(times: list[float], duration: float, bucket_seconds: float = 300.0) -> float:
    if duration <= 0:
        return 0.0
    buckets = max(1, int(duration // bucket_seconds) + (1 if duration % bucket_seconds else 0))
    occupied = {min(buckets - 1, max(0, int(t // bucket_seconds))) for t in times}
    return round(len(occupied) / buckets, 4)


def _max_gap(times: list[float], duration: float) -> float:
    points = [0.0] + sorted(times) + [duration]
    return round(max(b - a for a, b in zip(points, points[1:])), 1)


def _semantic_coverage(windows: list[dict], times: list[float], tolerance: float = 2.0) -> float:
    if not windows:
        return 1.0
    covered = sum(
        1
        for window in windows
        if any(window["start"] - tolerance <= t <= window["end"] + tolerance for t in times)
    )
    return round(covered / len(windows), 4)


def _scene_coverage(cuts: list[dict], times: list[float], tolerance: float = 10.0) -> float:
    if not cuts:
        return 0.0
    covered = sum(1 for cut in cuts if any(abs(t - cut["time"]) <= tolerance for t in times))
    return round(covered / len(cuts), 4)


def _info_stats(times: list[float], samples: list[dict]) -> dict:
    if not times or not samples:
        return {"entropy_mean": None, "detail_ratio": None, "blur_mean": None}
    nearest = [min(samples, key=lambda s: abs(s["time"] - t)) for t in times]
    entropies = [s.get("entropy") for s in nearest if s.get("entropy") is not None]
    blurs = [s.get("blur") for s in nearest if s.get("blur") is not None]
    return {
        "entropy_mean": round(statistics.mean(entropies), 4) if entropies else None,
        "detail_ratio": round(sum(1 for e in entropies if e >= 0.85) / len(entropies), 4) if entropies else None,
        "blur_mean": round(statistics.mean(blurs), 3) if blurs else None,
    }


def summarize(
    name: str,
    times: list[float],
    duration: float,
    windows: list[dict],
    cuts: list[dict],
    samples: list[dict],
    has_lexicon_hits: bool = True,
) -> dict:
    stats = {
        "strategy": name,
        "frames": len(times),
        "occupancy_5min": _occupancy(times, duration),
        "max_gap_seconds": _max_gap(times, duration),
        "lexicon_window_coverage": _semantic_coverage(windows, times) if has_lexicon_hits else None,
        "scene_cut_coverage": _scene_coverage(cuts, times),
    }
    stats.update(_info_stats(times, samples))
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="抽帧新旧策略对照")
    parser.add_argument("--work-ids", required=True, help="逗号分隔 work_id")
    parser.add_argument("--db", default=str(ROOT / "aggregator.db"))
    parser.add_argument("--media", default=str(ROOT / "media"))
    parser.add_argument("--out", default=str(ROOT / "triage_out" / "frame_strategy_compare"))
    args = parser.parse_args()

    work_ids = [int(part) for part in args.work_ids.split(",") if part.strip()]
    db_path = Path(args.db)
    media_root = Path(args.media)
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    entries = load_lexicon(db_path)
    cfg = frames.load_settings()["teaching"]
    results: list[dict] = []

    for work in load_works(db_path, work_ids):
        media_dir = media_root / str(work["media_path"])
        videos = sorted(media_dir.glob("*.mp4"))
        if not videos:
            print(f"skip work={work['id']}: no mp4 in {media_dir}", file=sys.stderr)
            continue
        video = videos[0]
        segments = load_segments(media_root, work["transcript_path"])
        work_dir = out_root / str(work["id"])
        work_dir.mkdir(parents=True, exist_ok=True)
        work_path, _ = frames.ensure_ascii_work_copy(video)
        duration = frames.video_duration_seconds(work_path)
        if not duration:
            print(f"skip work={work['id']}: duration unavailable", file=sys.stderr)
            continue

        # 与 monitor/jobs.py::run_frames_stage 完全一致的上游窗口构造
        hits_windows = frames.match_windows(segments, entries) if segments else []
        windows = hits_windows
        fallback = False
        if not windows:
            windows = frames.uniform_windows(duration, interval=30.0, max_frames=12)
            fallback = True

        classic_dir = work_dir / "classic"
        classic_frames = frames.extract_frames_classic(work_path, windows, classic_dir / "frames")
        frames.write_manifest(
            work["id"], classic_frames, classic_dir,
            semantic_aligned=bool(segments), fallback=fallback, strategy="classic",
        )

        cuts = frames.scan_scene_cuts(work_path, cfg)
        samples = frames.scan_frame_quality(work_path, cfg)
        budget = frames.frame_budget(duration, cfg)
        selections = frames.select_teaching_windows(duration, windows, cuts, samples, cfg, budget)
        teaching_dir = work_dir / "teaching"
        teaching_frames = frames.extract_selected_frames(work_path, selections, teaching_dir / "frames")
        frames.write_manifest(
            work["id"], teaching_frames, teaching_dir,
            semantic_aligned=bool(segments), fallback=fallback, strategy="teaching",
        )

        classic_times = [f["time_seconds"] for f in classic_frames]
        teaching_times = [f["time_seconds"] for f in teaching_frames]
        existing = None
        if work["manifest"]:
            existing_path = media_root / work["manifest"]
            if existing_path.is_file():
                existing = len(json.loads(existing_path.read_text(encoding="utf-8")).get("frames", []))
        row = {
            "work_id": work["id"],
            "title": work["title"],
            "duration_seconds": round(duration, 1),
            "lexicon_hit_windows": len(hits_windows),
            "digest_windows_used": len(windows),
            "fallback": fallback,
            "scene_cuts": len(cuts),
            "quality_samples": len(samples),
            "existing_manifest_frames": existing,
            "classic": summarize("classic", classic_times, duration, windows, cuts, samples, bool(hits_windows)),
            "teaching": summarize("teaching", teaching_times, duration, windows, cuts, samples, bool(hits_windows)),
            "teaching_source_mix": _source_mix(teaching_frames),
        }
        results.append(row)
        print(json.dumps(row, ensure_ascii=False))

    (out_root / "comparison.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_root / "comparison.md").write_text(_render_markdown(results), encoding="utf-8")
    print(f"written: {out_root / 'comparison.json'} / {out_root / 'comparison.md'}")
    return 0


def _source_mix(frames_list: list[dict]) -> dict:
    mix: dict[str, int] = {}
    for frame in frames_list:
        source = str(frame.get("source") or "classic")
        mix[source] = mix.get(source, 0) + 1
    return mix


def _render_markdown(rows: list[dict]) -> str:
    lines = [
        "| work_id | 时长 | 词库命中窗口 | 兜底窗口 | 旧帧数 | 新帧数 | 旧5min覆盖 | 新5min覆盖 | 旧最大间隔 | 新最大间隔 | 旧命中窗口覆盖 | 新命中窗口覆盖 | 旧切变覆盖 | 新切变覆盖 | 新帧来源 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        old, new = row["classic"], row["teaching"]
        old_cov = "-" if old["lexicon_window_coverage"] is None else f"{old['lexicon_window_coverage']:.0%}"
        new_cov = "-" if new["lexicon_window_coverage"] is None else f"{new['lexicon_window_coverage']:.0%}"
        lines.append(
            f"| {row['work_id']} | {row['duration_seconds']}s | {row['lexicon_hit_windows']} | "
            f"{row['digest_windows_used'] if row['fallback'] else '-'} | "
            f"{old['frames']} | {new['frames']} | "
            f"{old['occupancy_5min']:.0%} | {new['occupancy_5min']:.0%} | "
            f"{old['max_gap_seconds']}s | {new['max_gap_seconds']}s | "
            f"{old_cov} | {new_cov} | "
            f"{old['scene_cut_coverage']:.0%} | {new['scene_cut_coverage']:.0%} | "
            f"{json.dumps(row['teaching_source_mix'], ensure_ascii=False)} |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
