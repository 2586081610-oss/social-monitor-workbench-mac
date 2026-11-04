"""抽帧策略测试：教学视频自适应预算 / 场景与清晰度优选 / manifest 兼容。

只测纯函数与配置开关（不调 ffmpeg），保证可在任意环境复现。
"""
from __future__ import annotations

import json

from monitor.pipeline import frames


def _cfg() -> dict:
    return dict(frames.DEFAULT_TEACHING)


def test_frame_budget_adapts_by_duration():
    cfg = _cfg()
    # 80 秒短视频走下限 6 帧；24 分钟约每 25 秒一帧；48 分钟封顶 60 帧
    assert frames.frame_budget(80, cfg) == 6
    assert frames.frame_budget(1440, cfg) == 58
    assert frames.frame_budget(2881, cfg) == 60
    assert frames.frame_budget(0, cfg) == 0


def test_frame_budget_respects_min_gap_for_tiny_clips():
    cfg = _cfg()
    # 10 秒片长：min_gap=3 时最多 3 帧，不能因 min_frames=6 过密
    assert frames.frame_budget(10, cfg) == 3


def test_parse_ffmpeg_metadata_reads_blur_entropy_scene():
    output = "\n".join(
        [
            "[Parsed_metadata_3 @ 0x1] frame:0    pts:0       pts_time:0",
            "[Parsed_metadata_3 @ 0x1] lavfi.blur=6.539567",
            "[Parsed_metadata_3 @ 0x1] lavfi.entropy.normalized_entropy.normal.Y=0.907635",
            "[Parsed_metadata_3 @ 0x1] frame:1    pts:1       pts_time:5",
            "[Parsed_metadata_3 @ 0x1] lavfi.scene_score=0.459747",
        ]
    )
    records = frames.parse_ffmpeg_metadata(output)
    assert [r["time"] for r in records] == [0.0, 5.0]
    assert records[0]["blur"] == 6.539567
    assert records[0]["entropy"] == 0.907635
    assert records[1]["scene_score"] == 0.459747


def test_select_teaching_windows_covers_every_bucket_and_prefers_semantic():
    cfg = _cfg()
    duration = 300.0
    budget = 10
    windows = [{"start": 40.0, "end": 50.0, "hits": [{"term": "深蹲", "action": "increase", "weight": 1.0}]}]
    samples = [{"time": t, "blur": 2.0, "entropy": 0.95} for t in range(0, 301, 25)]
    samples[2] = {"time": 50.0, "blur": 8.0, "entropy": 0.2}  # 同桶内低分样本不应压过词库命中
    cuts = [{"time": 125.5, "score": 0.6}]

    selections = frames.select_teaching_windows(duration, windows, cuts, samples, cfg, budget)

    assert len(selections) == budget
    assert all(selection["end"] > selection["start"] for selection in selections)
    assert [s["time"] for s in selections] == sorted(s["time"] for s in selections)
    semantic = [s for s in selections if s["source"] == "semantic"]
    assert len(semantic) == 1
    assert semantic[0]["hits"][0]["term"] == "深蹲"
    # 每桶恰有一帧且落在桶内 → 全片时间覆盖（长视频不再只有词库命中的零散几帧）
    assert all(s["start"] <= s["time"] <= s["end"] for s in selections)


def test_select_teaching_windows_midpoint_fallback_keeps_coverage():
    cfg = _cfg()
    selections = frames.select_teaching_windows(120.0, [], [], [], cfg, budget=6)
    assert len(selections) == 6
    assert all(s["source"] == "midpoint" for s in selections)
    assert all(s["hits"] == [] for s in selections)


def test_clustered_semantic_windows_are_all_kept():
    cfg = _cfg()
    windows = [
        {"start": t, "end": t + 2, "hits": [{"term": f"动作{i}", "action": "increase", "weight": 1.0}]}
        for i, t in enumerate([10, 15, 60, 65, 100])
    ]
    selections = frames.select_teaching_windows(300.0, windows, [], [], cfg, budget=6)
    semantic_times = [s["time"] for s in selections if s["source"] == "semantic"]
    # 词库命中帧逐个保留（不再因时长桶只选一帧而丢失），且补充未覆盖桶
    assert semantic_times == [11.0, 16.0, 61.0, 66.0, 101.0]


def test_quality_score_prefers_sharp_detailed_frames():
    sharp_detailed = {"time": 0, "blur": 1.0, "entropy": 0.95}
    blurry_flat = {"time": 0, "blur": 11.0, "entropy": 0.30}
    assert frames._quality_score(sharp_detailed) > frames._quality_score(blurry_flat)


def test_write_manifest_keeps_legacy_schema_and_records_strategy(tmp_path):
    frames_list = [
        {
            "file": "frames/frame_0001.jpg",
            "time_seconds": 12.5,
            "window_start": 0.0,
            "window_end": 25.0,
            "hits": [],
            "confidence": 0.8,
        }
    ]
    path = frames.write_manifest(9, frames_list, tmp_path, semantic_aligned=False, fallback=True, strategy="teaching")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["work_id"] == 9
    assert payload["semantic_aligned"] is False
    assert payload["fallback"] is True
    assert payload["strategy"] == "teaching"
    assert payload["frames"] == frames_list


def test_strategy_switch_defaults_teaching_and_env_overrides(monkeypatch):
    # 2026-11-04 起默认 teaching；classic 保留为显式回退。
    monkeypatch.delenv(frames.STRATEGY_ENV, raising=False)
    assert frames.active_strategy() == "teaching"
    monkeypatch.setenv(frames.STRATEGY_ENV, "classic")
    assert frames.active_strategy() == "classic"
    monkeypatch.setenv(frames.STRATEGY_ENV, "unknown")
    assert frames.active_strategy() == "teaching"
