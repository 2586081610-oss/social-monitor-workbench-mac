#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3 内容形态判定与路由：features.json → content_kind / 模板 / 读帧预算（纯标准库）。

判定的是「内容形态」（口播、动作教学、跟练、图文收藏、vlog），不是
monitor/triage.py 的「兴趣类目」（健身、AI 工具、赚钱）。两者正交：
兴趣类目决定要不要做笔记，内容形态决定用哪个模板、读多少帧。

用法：
    python3 classify_content.py --input features.json
    cat features.json | python3 classify_content.py --stdin --json
    python3 classify_content.py --self-test

规则与阈值见同目录上一级《00-内容类型判定与路由.md》，本文件是该文档的
可执行实现；改动公式时必须同步文档。
"""
from __future__ import annotations

import argparse
import json
import sys

KIND_ORDER = ("talking_head", "workout_demo", "follow_along", "collection_post", "vlog", "unknown")

KIND_LABELS = {
    "talking_head": "口播/理论",
    "workout_demo": "动作教学/锻炼",
    "follow_along": "跟练/带练",
    "collection_post": "图文/收藏",
    "vlog": "生活记录/对话剧情",
    "unknown": "置信不足",
}

# 路由：模板文件、读帧密度（秒/帧）与上下限、建议输出结构
ROUTES = {
    "talking_head": {
        "template": "docs/note-templates/v3/01-口播理论类.md",
        "template_note": "",
        "per_frame_seconds": 40.0,
        "min_frames": 6,
        "max_frames": 24,
        "structure": ["一句话结论", "概念定义", "机制链", "分节要点", "反例与边界条件", "外部对照", "证据台账"],
    },
    "workout_demo": {
        "template": "docs/note-templates/v3/02-动作教学类.md",
        "template_note": "",
        "per_frame_seconds": 18.0,
        "min_frames": 8,
        "max_frames": 40,
        "structure": [
            "一句话结论",
            "动作定位",
            "握法·站位·关节角度表",
            "动作分解表",
            "口令清单",
            "常见错误",
            "变式与退阶",
            "证据台账",
        ],
    },
    "follow_along": {
        "template": "docs/note-templates/v3/03-跟练类.md",
        "template_note": "",
        "per_frame_seconds": 30.0,
        "min_frames": 8,
        "max_frames": 32,
        "structure": ["一句话结论", "训练参数表", "分节要点", "画面实读", "纠错", "外部对照", "证据台账"],
    },
    "collection_post": {
        "template": "docs/note-templates/v3/04-图文收藏类.md",
        "template_note": "",
        "per_frame_seconds": 0.0,
        "min_frames": 0,
        "max_frames": 0,
        "structure": ["条目清单", "主题归并", "去重与边界", "来源可信度分级", "外部核查", "证据台账"],
    },
    "vlog": {
        "template": "docs/note-templates/v3/01-口播理论类.md",
        "template_note": "v3 暂无 vlog 专用模板：暂用口播模板，删掉机制链/外部对照，改为时间线结构",
        "per_frame_seconds": 45.0,
        "min_frames": 4,
        "max_frames": 16,
        "structure": ["一句话结论", "时间线（按场景）", "关键信息与人/物", "与其它笔记的关系", "待确认"],
    },
    "unknown": {
        "template": None,
        "template_note": "低置信：补齐 features 或交 LLM/人工复核后再路由",
        "per_frame_seconds": 0.0,
        "min_frames": 0,
        "max_frames": 0,
        "structure": [],
    },
}

# 数值字段的保守默认值（缺失时使用，并记入 missing_fields）
NUMERIC_DEFAULTS = {
    "duration_seconds": 0.0,
    "speech_ratio": 0.0,
    "silence_ratio": None,  # 缺失时按 1 - speech_ratio 推导
    "scene_cut_per_min": 0.0,
    "continuous_motion_ratio": 0.0,
    "motion_spike_ratio": 0.0,
    "set_rep_density": 0.0,
    "chars_per_minute": 0.0,
    "dialogue_turn_per_min": 0.0,
    "image_count": 0,
}

CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}


def clamp01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def ramp(value: float, low: float, high: float) -> float:
    """线性升函数：value<=low 取 0，>=high 取 1。"""
    if high <= low:
        return 1.0 if value >= high else 0.0
    return clamp01((value - low) / (high - low))


def band(value: float, start: float, full_low: float, full_high: float, end: float) -> float:
    """梯形隶属：两端衰减，中间取 1。用于「中等覆盖」这类区间信号。"""
    if value <= start or value >= end:
        return 0.0
    if value < full_low:
        return ramp(value, start, full_low)
    if value <= full_high:
        return 1.0
    return 1.0 - ramp(value, full_high, end)


def cap_confidence(confidence: str, ceiling: str) -> str:
    if CONFIDENCE_RANK[confidence] > CONFIDENCE_RANK[ceiling]:
        return ceiling
    return confidence


def _normalize(payload) -> tuple:
    """把输入 JSON 归一成内部 features，缺失字段用保守默认并记录。"""
    missing: list = []
    warnings: list = []
    source = payload if isinstance(payload, dict) else {}

    def num(key: str, default):
        if key not in source or source.get(key) is None:
            missing.append(key)
            return default
        raw = source.get(key)
        if isinstance(raw, bool):
            missing.append(key)
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            missing.append(key)
            warnings.append(f"字段 {key} 不是数值，已用默认值 {default}")
            return default

    def flag(key: str, default: bool) -> bool:
        if key not in source:
            missing.append(key)
            return default
        raw = source.get(key)
        if isinstance(raw, bool):
            return raw
        missing.append(key)
        warnings.append(f"字段 {key} 不是布尔值，已用默认值 {default}")
        return default

    features = {key: num(key, default) for key, default in NUMERIC_DEFAULTS.items() if default is not None}

    speech_ratio = clamp01(features["speech_ratio"])
    # 静音比例缺失时按 1 - 语音覆盖推导，并如实记入缺失字段
    if "silence_ratio" in source and source.get("silence_ratio") is not None:
        silence_ratio = clamp01(num("silence_ratio", 1.0 - speech_ratio))
    else:
        missing.append("silence_ratio")
        silence_ratio = clamp01(1.0 - speech_ratio)
    features["speech_ratio"] = speech_ratio
    features["silence_ratio"] = silence_ratio
    features["image_count"] = max(0.0, features["image_count"])
    features["duration_seconds"] = max(0.0, features["duration_seconds"])

    features["has_video"] = flag("has_video", True)
    features["has_transcript"] = flag("has_transcript", True)

    raw_hits = source.get("keyword_hits")
    hits = {"follow_along": 0.0, "action": 0.0, "theory": 0.0}
    if not isinstance(raw_hits, dict):
        missing.append("keyword_hits")
    else:
        for key in hits:
            if key not in raw_hits or raw_hits.get(key) is None:
                missing.append(f"keyword_hits.{key}")
                continue
            try:
                hits[key] = max(0.0, float(raw_hits[key]))
            except (TypeError, ValueError):
                missing.append(f"keyword_hits.{key}")
                warnings.append(f"keyword_hits.{key} 不是数值，已按 0 处理")
    features["keyword_hits"] = hits
    return features, missing, warnings


def _score_all(features: dict) -> tuple:
    """按文档公式算 5 类得分（0~100，已按各自权重上限归一）。"""
    duration = features["duration_seconds"]
    duration_min = max(duration, 60.0) / 60.0
    speech = features["speech_ratio"]
    silence = features["silence_ratio"]
    voice = clamp01((speech + (1.0 - silence)) / 2.0)
    cut = max(0.0, features["scene_cut_per_min"])
    motion = clamp01(features["continuous_motion_ratio"])
    spike = clamp01(features["motion_spike_ratio"])
    setrep = max(0.0, features["set_rep_density"])
    chars = max(0.0, features["chars_per_minute"])
    dialogue = max(0.0, features["dialogue_turn_per_min"])
    images = features["image_count"]

    hits = features["keyword_hits"]
    follow_rate = hits["follow_along"] / duration_min
    action_rate = hits["action"] / duration_min
    theory_rate = hits["theory"] / duration_min

    evidence = {kind: [] for kind in KIND_ORDER}

    def add(kind: str, signal: str, value: float, points: float, text: str) -> None:
        evidence[kind].append(
            {"signal": signal, "value": round(float(value), 2), "points": round(points, 1), "text": text}
        )

    # ---- 口播/理论：语音覆盖 + 静态画面 + 理论词密度 ----
    talk = 0.0
    talk += 26.0 * ramp(voice, 0.40, 0.78)
    add("talking_head", "语音覆盖率", voice, 26.0 * ramp(voice, 0.40, 0.78), f"语音覆盖约 {voice:.0%}")
    talk += 18.0 * (1.0 - ramp(motion, 0.20, 0.55))
    add("talking_head", "连续运动覆盖率", motion, 18.0 * (1.0 - ramp(motion, 0.20, 0.55)), "画面以讲解/静态为主")
    talk += 22.0 * ramp(theory_rate, 0.15, 0.90)
    add("talking_head", "理论词密度", theory_rate, 22.0 * ramp(theory_rate, 0.15, 0.90), f"理论词 {theory_rate:.2f} 次/分")
    talk += 12.0 * (1.0 - ramp(cut, 1.5, 6.0))
    add("talking_head", "场景切变率", cut, 12.0 * (1.0 - ramp(cut, 1.5, 6.0)), "机位稳定、切镜少")
    talk += 8.0 * (1.0 - ramp(spike, 0.05, 0.30))
    add("talking_head", "运动尖峰比例", spike, 8.0 * (1.0 - ramp(spike, 0.05, 0.30)), "无运动波形尖峰")
    talk += 6.0 * (1.0 - ramp(setrep, 0.5, 2.5))
    add("talking_head", "组数次数密度", setrep, 6.0 * (1.0 - ramp(setrep, 0.5, 2.5)), "无组数/次数口令")
    talk += 8.0 * ramp(chars, 120.0, 280.0)
    add("talking_head", "口播字速", chars, 8.0 * ramp(chars, 120.0, 280.0), f"约 {chars:.0f} 字/分")

    # ---- 动作教学：中等连续运动 + 动作词 + 组数讲解 ----
    demo = 0.0
    demo += 24.0 * band(motion, 0.18, 0.30, 0.55, 0.82)
    add("workout_demo", "连续运动覆盖率", motion, 24.0 * band(motion, 0.18, 0.30, 0.55, 0.82), "示范与讲解交替（中等覆盖）")
    demo += 18.0 * ramp(spike, 0.05, 0.30)
    add("workout_demo", "运动尖峰比例", spike, 18.0 * ramp(spike, 0.05, 0.30), "有单次动作尖峰")
    demo += 24.0 * ramp(action_rate, 0.40, 2.00)
    add("workout_demo", "动作词密度", action_rate, 24.0 * ramp(action_rate, 0.40, 2.00), f"动作词 {action_rate:.2f} 次/分")
    demo += 12.0 * ramp(setrep, 0.30, 1.50)
    add("workout_demo", "组数次数密度", setrep, 12.0 * ramp(setrep, 0.30, 1.50), f"组数/次数 {setrep:.2f} 次/分")
    demo += 12.0 * (1.0 - ramp(cut, 3.0, 9.0))
    add("workout_demo", "场景切变率", cut, 12.0 * (1.0 - ramp(cut, 3.0, 9.0)), "切镜克制")
    demo += 10.0 * ramp(speech, 0.15, 0.60)
    add("workout_demo", "语音覆盖率", speech, 10.0 * ramp(speech, 0.15, 0.60), "有讲解声")

    # ---- 跟练：高连续运动 + 长时长 + 组数密集 + 跟练词 ----
    follow = 0.0
    follow += 34.0 * ramp(motion, 0.50, 0.85)
    add("follow_along", "连续运动覆盖率", motion, 34.0 * ramp(motion, 0.50, 0.85), "长时间连续运动")
    follow += 12.0 * ramp(spike, 0.10, 0.35)
    add("follow_along", "运动尖峰比例", spike, 12.0 * ramp(spike, 0.10, 0.35), "运动波形密集")
    follow += 12.0 * ramp(duration_min, 6.0, 20.0)
    add("follow_along", "时长", duration_min, 12.0 * ramp(duration_min, 6.0, 20.0), f"{duration_min:.1f} 分钟")
    follow += 14.0 * ramp(setrep, 1.0, 3.5)
    add("follow_along", "组数次数密度", setrep, 14.0 * ramp(setrep, 1.0, 3.5), f"组数/次数 {setrep:.2f} 次/分")
    follow += 22.0 * ramp(follow_rate, 0.12, 0.80)
    add("follow_along", "跟练词密度", follow_rate, 22.0 * ramp(follow_rate, 0.12, 0.80), f"跟练词 {follow_rate:.2f} 次/分")
    follow += 6.0 * (1.0 - ramp(cut, 0.8, 3.0))
    add("follow_along", "场景切变率", cut, 6.0 * (1.0 - ramp(cut, 0.8, 3.0)), "几乎不切镜")
    if 0.0 < duration < 45.0:
        follow *= 0.5
        evidence["follow_along"].append(
            {"signal": "时长", "value": round(duration, 1), "points": 0.0, "text": "短片惩罚：不足 45 秒不判跟练（得分乘 0.5）"}
        )

    # ---- 图文/收藏：无视频是主信号 ----
    collection = 0.0
    no_video = 0.0 if features["has_video"] else 60.0
    collection += no_video
    add("collection_post", "是否有视频", 0.0 if features["has_video"] else 1.0, no_video, "无视频" if not features["has_video"] else "有视频，此项不计分")
    collection += 30.0 * ramp(images, 0.0, 9.0)
    add("collection_post", "图片数", images, 30.0 * ramp(images, 0.0, 9.0), f"{images:.0f} 张图")
    collection += 10.0 * (1.0 - ramp(duration, 30.0, 180.0))
    add("collection_post", "时长", duration, 10.0 * (1.0 - ramp(duration, 30.0, 180.0)), "时长极短或无视频")

    # ---- vlog：必须由切变率或对话轮次「正向」触发，再叠加其它扣分项 ----
    vlog = 0.0
    gate = max(ramp(cut, 2.0, 8.0), ramp(dialogue, 1.0, 4.0))
    vlog += 34.0 * ramp(cut, 3.0, 12.0)
    add("vlog", "场景切变率", cut, 34.0 * ramp(cut, 3.0, 12.0), f"{cut:.1f} 次/分")
    vlog += 30.0 * ramp(dialogue, 1.5, 6.0)
    add("vlog", "对话轮次密度", dialogue, 30.0 * ramp(dialogue, 1.5, 6.0), f"{dialogue:.1f} 轮/分")
    vlog += gate * 12.0 * (1.0 - ramp(motion, 0.15, 0.55))
    add("vlog", "连续运动覆盖率（门控）", motion, gate * 12.0 * (1.0 - ramp(motion, 0.15, 0.55)), "运动不连续")
    vlog += gate * 12.0 * (1.0 - ramp(setrep, 0.2, 1.2))
    add("vlog", "组数次数密度（门控）", setrep, gate * 12.0 * (1.0 - ramp(setrep, 0.2, 1.2)), "无训练组数")
    vlog += gate * 12.0 * (1.0 - ramp(follow_rate, 0.05, 0.50))
    add("vlog", "跟练词密度（门控）", follow_rate, gate * 12.0 * (1.0 - ramp(follow_rate, 0.05, 0.50)), "无跟练口令")

    # 无视频时，所有依赖画面的形态直接判 0
    if not features["has_video"]:
        talk = demo = follow = vlog = 0.0
        for kind in ("talking_head", "workout_demo", "follow_along", "vlog"):
            evidence[kind] = [
                {"signal": "是否有视频", "value": 0.0, "points": 0.0, "text": "无视频，该形态不适用"}
            ]

    scores = {
        "talking_head": round(talk, 1),
        "workout_demo": round(demo, 1),
        "follow_along": round(follow, 1),
        "collection_post": round(collection, 1),
        "vlog": round(vlog, 1),
    }
    return scores, evidence


def frame_budget(kind: str, duration: float, image_count: float) -> int:
    """建议读帧预算：图文按图片数，视频按形态密度 × 时长。"""
    if kind == "collection_post":
        return int(max(0, round(image_count)))
    route = ROUTES.get(kind, ROUTES["unknown"])
    if route["per_frame_seconds"] <= 0 or duration <= 0:
        return int(route["min_frames"])
    raw = round(duration / route["per_frame_seconds"])
    return int(min(route["max_frames"], max(route["min_frames"], raw)))


def classify(payload) -> dict:
    """主入口：features → kind / confidence / scores / evidence / template / needs_llm。"""
    features, missing, warnings = _normalize(payload)
    scores, evidence_all = _score_all(features)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], KIND_ORDER.index(item[0])))
    best_kind, best_score = ranked[0]
    second_kind, second_score = ranked[1]
    margin = round(best_score - second_score, 1)

    if best_score < 45.0:
        kind, confidence, needs_llm = "unknown", "low", True
    elif margin >= 12.0:
        kind = best_kind
        confidence = "high" if best_score >= 70.0 else "medium"
        needs_llm = False
    elif margin >= 6.0:
        kind, confidence, needs_llm = best_kind, "medium", True
    else:
        if best_score >= 60.0:
            kind, confidence, needs_llm = best_kind, "low", True
        else:
            kind, confidence, needs_llm = "unknown", "low", True

    # 无视频：只能走图文；连图片也没有则确实判不了
    if not features["has_video"]:
        if features["image_count"] > 0:
            kind = "collection_post"
            confidence = "high" if features["image_count"] >= 6 else "medium"
            needs_llm = False
        else:
            kind, confidence, needs_llm = "unknown", "low", True
            warnings.append("无视频且无图片：无法判定内容形态")

    # 最强信号缺失：不允许高置信硬判
    if features["has_video"] and "continuous_motion_ratio" in missing:
        confidence = cap_confidence(confidence, "medium")
        needs_llm = True
        warnings.append("缺少 continuous_motion_ratio：最强信号缺失，置信封顶 medium")

    # 无转写：关键词全为 0；运动信号仍可判形态，但组数/次数缺失，必须复核
    if features["has_video"] and not features["has_transcript"]:
        confidence = cap_confidence(confidence, "medium")
        needs_llm = True
        warnings.append("无转写（ASR 失败或无转写文件）：关键词信号记 0，需复核组数与次数")

    if features["has_video"] and features["duration_seconds"] <= 0:
        needs_llm = True
        warnings.append("缺少时长：读帧预算按最小值，建议复核")

    duration = features["duration_seconds"]
    if features["has_video"] and 0 < duration < 20:
        needs_llm = True
        warnings.append("时长不足 20 秒：信号不足，建议人工或 LLM 复核")

    route = ROUTES[kind]
    evidence = sorted(evidence_all.get(kind, []), key=lambda item: -item["points"])[:5]
    if kind == "unknown":
        evidence = sorted(evidence_all.get(best_kind, []), key=lambda item: -item["points"])[:5]

    return {
        "kind": kind,
        "label": KIND_LABELS[kind],
        "confidence": confidence,
        "scores": scores,
        "margin": margin,
        "runner_up": second_kind,
        "evidence": evidence,
        "template": route["template"],
        "template_note": route["template_note"],
        "frame_budget": frame_budget(kind, duration, features["image_count"]),
        "structure": list(route["structure"]),
        "needs_llm": needs_llm,
        "missing_fields": missing,
        "warnings": warnings,
    }


def format_summary(result: dict) -> str:
    """人类可读摘要。"""
    lines = [
        f"内容形态：{result['label']}（{result['kind']}）",
        f"置信度：{result['confidence']}（与第二名相差 {result['margin']} 分，第二名 {result['runner_up']}）",
        "得分：",
    ]
    for kind, score in sorted(result["scores"].items(), key=lambda item: -item[1]):
        lines.append(f"  {kind:<16} {score:>6.1f}")
    lines.append("主要证据：")
    if result["evidence"]:
        for item in result["evidence"]:
            lines.append(f"  - {item['signal']} = {item['value']} → {item['points']} 分：{item['text']}")
    else:
        lines.append("  - 无")
    if result["template"]:
        lines.append(f"模板：{result['template']}")
    else:
        lines.append("模板：无（未路由）")
    if result["template_note"]:
        lines.append(f"说明：{result['template_note']}")
    lines.append(f"建议读帧预算：{result['frame_budget']} 帧")
    if result["structure"]:
        lines.append("建议输出结构：" + " / ".join(result["structure"]))
    lines.append(f"是否需要 LLM 复核：{'是' if result['needs_llm'] else '否'}")
    lines.append("缺失字段：" + ("、".join(result["missing_fields"]) if result["missing_fields"] else "无"))
    if result["warnings"]:
        lines.append("提示：")
        for warning in result["warnings"]:
            lines.append(f"  - {warning}")
    return "\n".join(lines)


SELF_TEST_CASES = (
    (
        "口播/理论",
        {
            "duration_seconds": 720,
            "speech_ratio": 0.82,
            "silence_ratio": 0.15,
            "scene_cut_per_min": 0.8,
            "continuous_motion_ratio": 0.08,
            "motion_spike_ratio": 0.02,
            "set_rep_density": 0.1,
            "chars_per_minute": 260,
            "dialogue_turn_per_min": 0.2,
            "keyword_hits": {"follow_along": 0, "action": 1, "theory": 9},
            "has_video": True,
            "has_transcript": True,
            "image_count": 0,
        },
        "talking_head",
    ),
    (
        "动作教学",
        {
            "duration_seconds": 240,
            "speech_ratio": 0.45,
            "silence_ratio": 0.45,
            "scene_cut_per_min": 3.5,
            "continuous_motion_ratio": 0.50,
            "motion_spike_ratio": 0.12,
            "set_rep_density": 1.2,
            "chars_per_minute": 150,
            "dialogue_turn_per_min": 0.3,
            "keyword_hits": {"follow_along": 1, "action": 6, "theory": 2},
            "has_video": True,
            "has_transcript": True,
            "image_count": 0,
        },
        "workout_demo",
    ),
    (
        "跟练",
        {
            "duration_seconds": 1500,
            "speech_ratio": 0.55,
            "silence_ratio": 0.35,
            "scene_cut_per_min": 1.2,
            "continuous_motion_ratio": 0.74,
            "motion_spike_ratio": 0.24,
            "set_rep_density": 3.2,
            "chars_per_minute": 200,
            "dialogue_turn_per_min": 0.4,
            "keyword_hits": {"follow_along": 12, "action": 12, "theory": 4},
            "has_video": True,
            "has_transcript": True,
            "image_count": 0,
        },
        "follow_along",
    ),
    (
        "图文收藏",
        {
            "duration_seconds": 0,
            "speech_ratio": 0.0,
            "silence_ratio": 1.0,
            "scene_cut_per_min": 0.0,
            "continuous_motion_ratio": 0.0,
            "motion_spike_ratio": 0.0,
            "set_rep_density": 0.0,
            "chars_per_minute": 0,
            "dialogue_turn_per_min": 0,
            "keyword_hits": {"follow_along": 0, "action": 0, "theory": 2},
            "has_video": False,
            "has_transcript": False,
            "image_count": 12,
        },
        "collection_post",
    ),
    (
        "短视频不乱判跟练",
        {
            "duration_seconds": 30,
            "speech_ratio": 0.3,
            "silence_ratio": 0.6,
            "scene_cut_per_min": 2.0,
            "continuous_motion_ratio": 0.7,
            "motion_spike_ratio": 0.3,
            "set_rep_density": 2.0,
            "chars_per_minute": 80,
            "dialogue_turn_per_min": 0.2,
            "keyword_hits": {"follow_along": 0, "action": 1, "theory": 0},
            "has_video": True,
            "has_transcript": True,
            "image_count": 0,
        },
        "workout_demo",
    ),
)


def self_test() -> int:
    """内置用例自检：形态判定正确且 needs_llm 已标注。"""
    failures = 0
    for name, payload, expected in SELF_TEST_CASES:
        result = classify(payload)
        ok = result["kind"] == expected
        print(f"[{'PASS' if ok else 'FAIL'}] {name}：期望 {expected}，实际 {result['kind']}"
              f"（{result['confidence']}，needs_llm={result['needs_llm']}）")
        if ok:
            print(f"       得分 {result['scores']}，领先 {result['margin']} 分")
        else:
            failures += 1

    # 缺字段不得崩溃，且必须标明缺失
    broken = classify({"duration_seconds": 600, "has_video": True})
    ok_missing = "continuous_motion_ratio" in broken["missing_fields"] and broken["needs_llm"]
    print(f"[{'PASS' if ok_missing else 'FAIL'}] 缺字段兜底：kind={broken['kind']}，"
          f"缺失 {len(broken['missing_fields'])} 项，needs_llm={broken['needs_llm']}")
    if not ok_missing:
        failures += 1

    print(f"--- {len(SELF_TEST_CASES) + 1 - failures}/{len(SELF_TEST_CASES) + 1} 通过 ---")
    return 0 if failures == 0 else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="v3 内容形态判定与路由（只读，不联网，不写文件）",
    )
    parser.add_argument("--input", help="features JSON 文件路径")
    parser.add_argument("--stdin", action="store_true", help="从标准输入读取 JSON")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非摘要")
    parser.add_argument("--self-test", action="store_true", help="运行内置用例")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    if not args.input and not args.stdin:
        parser.print_help()
        return 2

    try:
        if args.stdin:
            payload = json.load(sys.stdin)
        else:
            with open(args.input, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"读取输入失败：{exc}", file=sys.stderr)
        return 2

    result = classify(payload)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_summary(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
