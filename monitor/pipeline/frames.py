"""流水线·语义对齐抽帧：词库命中 → 重点时间窗口 → ffmpeg 抽帧 → frame_manifest.json（SPEC §8.2）。

K20：同窗多词条、增删矛盾 → 取权重净额，冲突写日志。
无时间戳/空文本降级由上层处理（fallback 标记）。

segments 契约：[{start, end, text}]（秒）。paraformer-zh 的 segments 即此形状；
若退化单段（transcript 标注 timestamps: none），match_windows 用 start/end 默认值兜底不报错。
extract_frames 入口同样做 ASCII 路径规范化（与转写同根因：中文/空格/问号路径 ffmpeg 失败）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from monitor.pipeline.paths import ensure_ascii_work_copy, retry_write_text

logger = logging.getLogger("monitor.pipeline.frames")

# 抽帧策略开关：默认 teaching（2026-11-04 起，教学视频按片长自适应 + 场景/文字画面优先）；
# 需要旧行为时设 MONITOR_FRAME_STRATEGY=classic，或在 config/frames.json 写 {"strategy": "classic"}。
# 优先级：代码默认 < config/frames.json < 环境变量。
STRATEGY_ENV = "MONITOR_FRAME_STRATEGY"
DEFAULT_STRATEGY = "teaching"
FRAME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "frames.json"
DEFAULT_TEACHING = {
    "seconds_per_frame": 25.0,   # 每多少秒一帧 → 预算随片长自适应
    "min_frames": 6,             # 短视频下限
    "max_frames": 60,            # 长视频上限（48 分钟 ≈ 每 48 秒一帧）
    "scene_threshold": 0.30,     # ffmpeg scene 切变阈值
    "sample_interval_seconds": 5.0,  # 清晰度/信息量采样步长
    "min_gap_seconds": 3.0,      # 相邻帧最小间隔
    "max_total_frames": 90,      # 词库命中密集时的总帧数硬上限
    "scan_timeout_seconds": 900, # 单次全片扫描超时
}
VALID_STRATEGIES = ("classic", "teaching")


def load_settings() -> dict:
    """读取抽帧策略配置：config/frames.json → 环境变量 MONITOR_FRAME_STRATEGY 覆盖。"""
    strategy = DEFAULT_STRATEGY
    teaching = dict(DEFAULT_TEACHING)
    if FRAME_CONFIG_PATH.is_file():
        try:
            payload = json.loads(FRAME_CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            raw_strategy = str(payload.get("strategy") or strategy).strip()
            if raw_strategy:
                strategy = raw_strategy
            overrides = payload.get("teaching")
            if isinstance(overrides, dict):
                for key, default in DEFAULT_TEACHING.items():
                    if key in overrides:
                        try:
                            teaching[key] = type(default)(overrides[key])
                        except (TypeError, ValueError):
                            logger.warning("frames.json teaching.%s 值无效，忽略", key)
    env_strategy = os.getenv(STRATEGY_ENV, "").strip()
    if env_strategy:
        strategy = env_strategy
    if strategy not in VALID_STRATEGIES:
        logger.warning("未知抽帧策略 %r，回退默认 %s", strategy, DEFAULT_STRATEGY)
        strategy = DEFAULT_STRATEGY
    return {"strategy": strategy, "teaching": teaching}


def active_strategy() -> str:
    return str(load_settings()["strategy"])


def ffmpeg_binary() -> str:
    """ffmpeg 可执行文件：FFMPEG_BIN → PATH → imageio_ffmpeg 内置二进制。"""
    env_bin = os.getenv("FFMPEG_BIN", "").strip()
    if env_bin:
        return env_bin
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # 本机 requirements 已随转写链路安装

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - 无内置二进制时保留原 PATH 行为
        return "ffmpeg"


def match_windows(segments: list[dict], entries: list[dict]) -> list[dict]:
    """segments: [{start, end, text}]；entries: [{term, action, weight, enabled}] → 重点窗口。

    - paraformer 句级 segments 直接消费（[{start,end,text}]，start/end 单位为秒）
    - 退化单段（无 start/end）时用默认值兜底，不抛错（K15 降级）
    """
    windows = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        hits = []
        for entry in entries:
            if not entry.get("enabled", True):
                continue
            term = entry.get("term", "")
            if term and term in text:
                hits.append(
                    {"term": term, "action": entry.get("action", "increase"), "weight": entry.get("weight", 1.0)}
                )
        if hits:
            try:
                start = float(seg.get("start", 0.0))
            except (TypeError, ValueError):
                start = 0.0
            try:
                end = float(seg.get("end", start + 1.0))
            except (TypeError, ValueError):
                end = start + 1.0
            windows.append({"start": start, "end": end, "hits": hits})
    return windows


def _frame_count_for_window(window: dict) -> int:
    net = sum(h["weight"] for h in window["hits"] if h["action"] == "increase") - sum(
        h["weight"] for h in window["hits"] if h["action"] == "decrease"
    )
    count = max(1, round(net))
    if net != round(net):
        logger.info("lexicon conflict resolved by net weight: %s", window["hits"])
    return count


def video_duration_seconds(video_path: str | Path) -> float | None:
    """读取视频时长（秒）。本机无 ffprobe，解析 ffmpeg -i 输出；失败如实返回 None（上层放弃兜底）。"""
    try:
        result = subprocess.run(
            [ffmpeg_binary(), "-hide_banner", "-i", str(video_path)],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    match = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", result.stderr)
    if not match:
        return None
    hours, minutes, seconds = (float(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def uniform_windows(duration: float, interval: float = 30.0, max_frames: int = 12) -> list[dict]:
    """零命中兜底窗口：全片均匀取样（无词库命中），约每 interval 秒一帧，上限 max_frames。"""
    if duration <= 0:
        return []
    count = min(max_frames, max(1, round(duration / interval)))
    step = duration / count
    return [{"start": i * step, "end": (i + 1) * step, "hits": []} for i in range(count)]


def extract_frames(video_path: str, windows: list[dict], out_dir: Path) -> list[dict]:
    """抽取关键帧入口：默认 classic；strategy=teaching 时走教学视频自适应策略。

    teaching 失败（无时长/ffmpeg 不可用/零产出）时记录日志并回退 classic，
    调用方（monitor/jobs.py）无需改动即可通过配置/environment 切换。
    """
    if active_strategy() == "teaching":
        frames = extract_frames_teaching(video_path, windows, out_dir)
        if frames:
            return frames
        logger.warning("teaching 抽帧无产出，回退 classic（video=%s）", video_path)
    return extract_frames_classic(video_path, windows, out_dir)


def extract_frames_classic(video_path: str, windows: list[dict], out_dir: Path) -> list[dict]:
    """旧策略：每窗口默认 1 帧，increase 按权重增帧 / decrease 减帧（下限 1）。

    路径规范化：中文/空格/问号路径先复制到 media/_work/{hash}/ 再喂 ffmpeg
    （与转写同根因，规避 Windows 子进程非 ASCII 路径失败）。
    """
    work_path, _ = ensure_ascii_work_copy(video_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    idx = 1
    for window in windows:
        count = _frame_count_for_window(window)
        span = max(window["end"] - window["start"], 1.0)
        for k in range(count):
            t = window["start"] + span * (k + 0.5) / count
            name = f"frame_{idx:04d}.jpg"
            if _extract_one(work_path, t, out_dir / name):
                frames.append(
                    {
                        "file": f"frames/{name}",
                        "time_seconds": round(t, 2),
                        "window_start": round(window["start"], 2),
                        "window_end": round(window["end"], 2),
                        "hits": window["hits"],
                        "confidence": 0.9,
                    }
                )
                idx += 1
            else:
                logger.warning("ffmpeg frame extract failed at %s", t)
    return frames


def _extract_one(work_path: str, t: float, out_path: Path) -> bool:
    try:
        result = subprocess.run(
            [ffmpeg_binary(), "-y", "-ss", f"{t:.3f}", "-i", work_path, "-frames:v", "1", "-q:v", "3", str(out_path)],
            capture_output=True,
        )
    except OSError as exc:
        logger.warning("ffmpeg 不可用（%s）", exc)
        return False
    return result.returncode == 0


# ---- teaching 策略：按时长自适应帧数 + 场景切变/清晰度/信息量优先 ---- #


_PTS_RE = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")
_META_RE = re.compile(r"lavfi\.(blur|scene_score|entropy\.normalized_entropy\.normal\.Y)=([0-9.eE+-]+)")


def parse_ffmpeg_metadata(output: str) -> list[dict]:
    """解析 `metadata=print` 输出：每帧一条 {time, blur?, scene_score?, entropy?}。"""
    records: list[dict] = []
    current: dict | None = None
    for line in output.splitlines():
        pts = _PTS_RE.search(line)
        if pts:
            current = {"time": float(pts.group(1))}
            records.append(current)
            continue
        if current is None:
            continue
        for key, raw in _META_RE.findall(line):
            name = "entropy" if key.startswith("entropy") else key
            try:
                current[name] = float(raw)
            except ValueError:
                continue
    return records


def _run_ffmpeg(args: list[str], timeout: int) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffmpeg 扫描失败（%s）", exc)
        return None


def scan_scene_cuts(video_path: str, cfg: dict, *, ffmpeg: str | None = None) -> list[dict]:
    """场景切变候选：ffmpeg scene 分数超过阈值的时刻（用于优先覆盖画面切换）。"""
    ffmpeg = ffmpeg or ffmpeg_binary()
    threshold = float(cfg["scene_threshold"])
    result = _run_ffmpeg(
        [
            ffmpeg, "-hide_banner", "-nostdin", "-i", video_path, "-an",
            "-vf", f"select='gt(scene,{threshold})',metadata=print",
            "-f", "null", "-",
        ],
        int(cfg["scan_timeout_seconds"]),
    )
    if result is None or result.returncode != 0:
        return []
    cuts = [
        {"time": rec["time"], "score": rec.get("scene_score", threshold)}
        for rec in parse_ffmpeg_metadata(result.stderr)
        if "time" in rec
    ]
    return cuts[:800]  # 防止开场闪白等高频切换撑爆候选池


def scan_frame_quality(video_path: str, cfg: dict, *, ffmpeg: str | None = None) -> list[dict]:
    """全片低分辨率等距采样：readability（熵，越高文字/表格细节越多）+ 模糊度。"""
    ffmpeg = ffmpeg or ffmpeg_binary()
    interval = float(cfg["sample_interval_seconds"])
    result = _run_ffmpeg(
        [
            ffmpeg, "-hide_banner", "-nostdin", "-i", video_path, "-an",
            "-vf", f"fps=1/{interval:g},scale=320:-2,entropy,blurdetect,metadata=print",
            "-f", "null", "-",
        ],
        int(cfg["scan_timeout_seconds"]),
    )
    if result is None or result.returncode != 0:
        return []
    return [rec for rec in parse_ffmpeg_metadata(result.stderr) if "time" in rec]


def frame_budget(duration: float, cfg: dict) -> int:
    """按片长自适应帧预算，并受 min_gap 约束（短视频不因 min_frames 过度密集）。"""
    if duration <= 0:
        return 0
    raw = round(duration / max(float(cfg["seconds_per_frame"]), 1.0))
    count = min(int(cfg["max_frames"]), max(int(cfg["min_frames"]), int(raw)))
    gap = float(cfg["min_gap_seconds"])
    if gap > 0:
        count = min(count, max(1, int(duration // gap)))
    return max(1, count)


def _quality_score(sample: dict | None) -> float:
    """单帧信息量得分：归一化熵占 0.7，清晰度（blur 越低越清晰）占 0.3。"""
    if not sample:
        return 0.5
    entropy = sample.get("entropy")
    blur = sample.get("blur")
    entropy_score = 0.0 if entropy is None else max(0.0, min(1.0, float(entropy)))
    blur_score = 0.5 if blur is None else 1.0 - min(max(float(blur), 0.0), 12.0) / 12.0
    return round(0.7 * entropy_score + 0.3 * blur_score, 4)


def _nearest_quality(time_seconds: float, samples: list[dict]) -> float:
    if not samples:
        return 0.5
    nearest = min(samples, key=lambda s: abs(s["time"] - time_seconds))
    return _quality_score(nearest)


def _overlapping_hits(window_start: float, window_end: float, windows: list[dict]) -> list[dict]:
    hits: list[dict] = []
    seen: set[tuple] = set()
    for window in windows:
        if window.get("end", 0) < window_start or window.get("start", 0) > window_end:
            continue
        for hit in window.get("hits") or []:
            key = (hit.get("term"), hit.get("action"))
            if key in seen:
                continue
            seen.add(key)
            hits.append(hit)
    return hits


def select_teaching_windows(
    duration: float,
    windows: list[dict],
    cuts: list[dict],
    samples: list[dict],
    cfg: dict,
    budget: int | None = None,
) -> list[dict]:
    """教学策略选帧：词库命中帧全部保留 → 其余时长桶按场景切变/清晰度优选 → 桶中点兜底。

    与旧策略的关键差别：
    1. 词库命中的窗口逐个出帧（近邻窗口按 min_gap 合并），不再被桶覆盖逻辑丢弃；
    2. 未被词库帧覆盖的时长桶各补一帧，保证长视频全片有画面；
    3. 桶内非词库帧优先场景切变/高熵清晰画面，而不是均匀切分。
    返回值与旧窗口同形（start/end/hits），额外带 time/source/score 供 manifest 追溯。
    """
    if duration <= 0:
        return []
    budget = budget or frame_budget(duration, cfg)
    if budget <= 0:
        return []
    gap = float(cfg["min_gap_seconds"])
    max_total = int(cfg.get("max_total_frames", 90))

    # 1) 词库命中帧：按时间排序，近邻（< min_gap）合并 hits，避免同句重复出帧
    semantic: list[dict] = []
    for window in sorted(windows, key=lambda w: float(w.get("start", 0.0))):
        try:
            start = float(window.get("start", 0.0))
            end = float(window.get("end", start))
        except (TypeError, ValueError):
            continue
        t = max(0.0, min(duration - 0.05, (start + end) / 2.0))
        hits = window.get("hits") or []
        if not hits:
            # 均匀兜底窗口没有语义信息，不强制出帧；由后面的覆盖桶按画面质量选优
            continue
        if semantic and t - semantic[-1]["time"] < gap:
            for hit in hits:
                if hit not in semantic[-1]["hits"]:
                    semantic[-1]["hits"].append(hit)
            continue
        semantic.append(
            {
                "start": start,
                "end": end,
                "time": round(t, 2),
                "hits": list(hits),
                "source": "semantic",
                "score": round(0.8 + 0.2 * min(len(hits), 3), 4),
            }
        )
    if len(semantic) > int(cfg["max_frames"]):
        # 命中过密时保留命中最多的 max_frames 个，仍按时间输出
        top = sorted(semantic, key=lambda s: (len(s["hits"]), s["score"]), reverse=True)[: int(cfg["max_frames"])]
        semantic = sorted(top, key=lambda s: s["time"])

    # 2) 覆盖桶：只给「没有词库命中帧」的桶补帧
    bucket = duration / budget
    buckets: list[list[dict]] = [[] for _ in range(budget)]
    for sample in samples:
        t = max(0.0, min(duration - 0.05, float(sample["time"])))
        buckets[min(budget - 1, max(0, int(t // bucket)))].append(
            {"time": t, "hits": [], "source": "sample", "score": _quality_score(sample)}
        )
    for cut in cuts:
        t = max(0.0, min(duration - 0.05, float(cut["time"])))
        buckets[min(budget - 1, max(0, int(t // bucket)))].append(
            {
                "time": t,
                "hits": [],
                "source": "scene",
                "score": round(0.2 + 0.8 * _nearest_quality(t, samples), 4),
            }
        )

    covered_buckets = {min(budget - 1, max(0, int(s["time"] // bucket))) for s in semantic}
    coverage: list[dict] = []
    previous: float | None = None
    for idx in range(budget):
        if idx in covered_buckets:
            continue
        start = idx * bucket
        end = duration if idx == budget - 1 else (idx + 1) * bucket
        candidates = buckets[idx]
        center = (start + end) / 2.0
        if candidates:
            chosen = max(candidates, key=lambda c: (c["score"], -abs(c["time"] - center)))
        else:
            chosen = {"time": center, "hits": [], "source": "midpoint", "score": 0.5}
        t = min(max(chosen["time"], start + 0.1), max(start + 0.1, end - 0.1))
        if previous is not None and t - previous < gap:
            t = min(previous + gap, end - 0.1)
            if t <= previous:  # 桶宽极端小于 gap 时保底给一个递增时刻
                t = previous + 0.01
        coverage.append(
            {
                "start": round(start, 2),
                "end": round(end, 2),
                "time": round(max(0.0, min(duration - 0.05, t)), 2),
                "hits": _overlapping_hits(start, end, windows),
                "source": chosen["source"],
                "score": chosen["score"],
            }
        )
        previous = coverage[-1]["time"]

    # 3) 合并并按时间输出；超总量时优先丢弃评分最低的非词库帧
    selections = semantic + coverage
    if len(selections) > max_total:
        extra = len(selections) - max_total
        drop = sorted(coverage, key=lambda s: (s["score"], s["time"]))[:extra]
        drop_ids = {id(item) for item in drop}
        selections = [s for s in selections if id(s) not in drop_ids]
    selections.sort(key=lambda s: s["time"])
    return selections


def extract_selected_frames(work_path: str, selections: list[dict], out_dir: Path) -> list[dict]:
    """按 select_teaching_windows 的选帧结果抽帧，返回 manifest frames 列表。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[dict] = []
    for idx, selection in enumerate(selections, 1):
        name = f"frame_{idx:04d}.jpg"
        if not _extract_one(work_path, selection["time"], out_dir / name):
            logger.warning("ffmpeg frame extract failed at %s", selection["time"])
            continue
        confidence = {"semantic": 0.9, "scene": 0.85, "sample": 0.8, "midpoint": 0.6}.get(
            selection["source"], 0.75
        )
        frames.append(
            {
                "file": f"frames/{name}",
                "time_seconds": selection["time"],
                "window_start": selection["start"],
                "window_end": selection["end"],
                "hits": selection["hits"],
                "confidence": confidence,
                "source": selection["source"],
                "score": selection["score"],
            }
        )
    return frames


def extract_frames_teaching(
    video_path: str, windows: list[dict], out_dir: Path, *, settings: dict | None = None
) -> list[dict]:
    """教学视频抽帧：全片扫描（场景切变 + 清晰度/熵）→ 桶内选帧 → 定点抽帧。

    manifest 字段与 classic 完全兼容，额外 source/score 仅用于追溯与对照。
    """
    cfg = (settings or load_settings())["teaching"]
    work_path, _ = ensure_ascii_work_copy(video_path)
    duration = video_duration_seconds(work_path)
    if not duration:
        logger.info("teaching 策略无法获取视频时长：%s", video_path)
        return []
    cuts = scan_scene_cuts(work_path, cfg)
    samples = scan_frame_quality(work_path, cfg)
    budget = frame_budget(duration, cfg)
    selections = select_teaching_windows(duration, windows, cuts, samples, cfg, budget)
    logger.info(
        "teaching select: duration=%.1fs budget=%d scene_cuts=%d samples=%d selected=%d",
        duration, budget, len(cuts), len(samples), len(selections),
    )
    return extract_selected_frames(work_path, selections, out_dir)


def write_manifest(
    work_id: int,
    frames: list[dict],
    out_dir: Path,
    semantic_aligned: bool = True,
    fallback: bool = False,
    strategy: str | None = None,
) -> Path:
    manifest = {
        "work_id": work_id,
        "semantic_aligned": semantic_aligned,
        "fallback": fallback,
        "strategy": strategy or active_strategy(),
        "frames": frames,
    }
    path = out_dir / "frame_manifest.json"
    retry_write_text(path, json.dumps(manifest, ensure_ascii=False, indent=2))
    return path
