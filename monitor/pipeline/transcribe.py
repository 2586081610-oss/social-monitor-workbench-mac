"""流水线·转写：FunASR paraformer-zh（VAD 分句 + 标点，句级时间戳）（K14/K18；引擎未装则明确报错）。

实测结论（端到端）：
  - SenseVoice 不输出句级时间戳（VAD 模式仅返回 key/text）→ 转写主力改 paraformer-zh。
  - paraformer-zh（iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch）
    转写带标点、质量更佳，配 fsmn-vad 分句 + ct-punc-c 标点，sentence_timestamp=True 时可取句级时间戳。
  - 中文/空格/问号路径导致 funasr/torchaudio 调 ffmpeg 失败 → 入口先复制到 media/_work/{hash}/。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from monitor.pipeline.download import PipelineOfflineError
from monitor.pipeline.paths import ascii_work_copy, retry_write_text

logger = logging.getLogger("monitor.pipeline.transcribe")

_ENGINE = None

# 转写主力：paraformer-zh（16k 中文、带标点），配 fsmn-vad 分句 + ct-punc-c 标点
_ASR_MODEL = "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
_VAD_MODEL = "fsmn-vad"
_PUNC_MODEL = "ct-punc-c"
_ENGINE_NAME = "paraformer-zh"

# 兜底清洗：剔除可能的 <|...|> 元数据标记（paraformer 一般不产生，防御性保留）
_META_TAG_RE = re.compile(r"<\|[^|]+\|>")


def _get_engine():
    """懒加载 paraformer-zh 引擎（全局单例，避免重复加载模型）。"""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    try:
        import funasr  # noqa: F401
        from funasr import AutoModel
    except ImportError:
        raise PipelineOfflineError("转写引擎 funasr 未安装（首次部署需预下载 paraformer-zh 模型，K18）") from None
    logger.info("loading paraformer-zh model (first load may take seconds)...")
    # 三个模型在 ModelScope 均为滚动 master revision（2026-10-28 API 核验，证据
    # preflight/evidence/modelscope-iic_*.json）；内容级锚点由部署时生成的
    # sha256 manifest（preflight/evidence/asr-model-manifest-*.json）提供。
    # funasr 1.4.16 的参数名：主模型 model_revision（download_from_ms.py:53）、
    # vad_model_revision / punc_model_revision（auto_model.py:476/489），
    # 缺省值已为 master；此处显式写出，避免隐式行为变化时无据可查。
    _ENGINE = AutoModel(
        model=_ASR_MODEL,
        model_revision="master",
        vad_model=_VAD_MODEL,
        vad_model_revision="master",
        vad_kwargs={"max_single_segment_time": 30000},
        punc_model=_PUNC_MODEL,
        punc_model_revision="master",
        device="cpu",
        disable_update=True,
    )
    logger.info("paraformer-zh model ready")
    return _ENGINE


def _clean_text(raw: str) -> str:
    """去除可能的元数据标记，保留纯文本。"""
    return _META_TAG_RE.sub("", raw or "").strip()


def _audio_duration_seconds(item: dict) -> float | None:
    """从逐字符时间戳推断音频真实时长（毫秒 → 秒）；无则 None。"""
    ts = item.get("timestamp")
    if isinstance(ts, list) and ts:
        last = ts[-1]
        if isinstance(last, (list, tuple)) and len(last) >= 2:
            try:
                return float(last[1]) / 1000.0
            except (TypeError, ValueError):
                return None
    return None


def _extract_segments(item: dict, fallback_end: float) -> tuple[list[dict], bool]:
    """从 paraformer 结果提取句级时间戳 segments。

    paraformer + fsmn-vad + ct-punc-c 在 sentence_timestamp=True 时返回
    item['sentence_info'] = [{text, start(ms), end(ms), timestamp}, ...]；
    缺失时退化为单段（返回 has_ts=False，供上层按 timestamps=none 降级）。
    """
    sentence_info = item.get("sentence_info")
    segments = []
    has_ts = False
    if isinstance(sentence_info, list) and sentence_info:
        has_ts = True
        for si in sentence_info:
            text = _clean_text(si.get("text"))
            if not text:
                continue
            start_ms = si.get("start")
            end_ms = si.get("end")
            if start_ms is None or end_ms is None:
                continue
            segments.append(
                {
                    "start": round(float(start_ms) / 1000.0, 3),
                    "end": round(float(end_ms) / 1000.0, 3),
                    "text": text,
                }
            )
    if segments:
        return segments, has_ts
    # 退化：单段（无句级时间戳）。end 优先取真实音频时长，其次取转写耗时
    text = _clean_text(item.get("text"))
    if text:
        end = _audio_duration_seconds(item) or fallback_end
        segments.append({"start": 0.0, "end": round(end, 2), "text": text})
    return segments, False


def transcribe(video_path: str, out_dir: Path) -> str:
    """对视频/音频文件做语音转文字，返回带时间戳转写稿 JSON 路径。

    输出格式（transcript/*.json）：
        {
          "source": "<原始视频/音频路径>",
          "engine": "paraformer-zh",
          "created_at": "ISO8601",
          "timestamps": "sentence" | "none",
          "segments": [{"start": 0.0, "end": 2.5, "text": "..."}],
          "text": "全文拼接"
        }
    """
    # 路径规范化：非 ASCII（中文/空格/问号）路径先复制到 media/_work/{hash}/ 再处理。
    # with 退出时（含转写异常/中断）删除该副本；与抽帧嵌套时由最外层统一清理。
    with ascii_work_copy(video_path) as (work_path, original):
        source = original or work_path
        if not Path(work_path).exists():
            # 不带本机路径：该文案会进入 works.last_error / 任务错误 / 日志
            raise PipelineOfflineError("转写输入文件不存在（可能已被清理或下载不完整）")

        engine = _get_engine()
        out_dir.mkdir(parents=True, exist_ok=True)

        logger.info("transcribing: %s (work=%s)", source, work_path)
        t0 = time.time()
        # pred_timestamp=True：paraformer 产出逐字符时间戳（result["timestamp"]）；
        # sentence_timestamp=True：据此切分句级时间戳（sentence_info）。
        # 漏传 pred_timestamp 会触发 design-timestamps §4.4 坑#1：
        # "punctuation timestamps could not be aligned" → 退化为 VAD 段（30s 粒度），F-1。
        res = engine.generate(
            input=work_path,
            cache={},
            pred_timestamp=True,
            sentence_timestamp=True,
            batch_size_s=300,
        )
        elapsed = time.time() - t0
        logger.info("transcribe done in %.1fs", elapsed)

        if not res:
            raise PipelineOfflineError("转写结果为空")

        item = res[0]
        segments, has_ts = _extract_segments(item, fallback_end=elapsed)
        full_text = _clean_text(item.get("text")) or "".join(seg["text"] for seg in segments).strip()

        # 提取文件名（去除扩展名）作为转写稿名
        stem = Path(video_path).stem
        out_path = out_dir / f"{stem}.json"
        doc = {
            "source": source,
            "engine": _ENGINE_NAME,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "timestamps": "sentence" if has_ts else "none",
            "segments": segments,
            "text": full_text,
        }
        retry_write_text(out_path, json.dumps(doc, ensure_ascii=False, indent=2))
        logger.info("transcript saved: %s", out_path)
        return str(out_path)
