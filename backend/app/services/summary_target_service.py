"""AI 总结交付目标：默认工具可配置，任务包按目标投递（方案 A，目录投递版）。

设计（2026-10-31 用户需求）：
- 总结任务包（素材就绪清单）不再硬编码走 WorkBuddy；支持 workbuddy / codex /
  trae / zcode 及自定义目录目标；
- 前端可修改"默认工具"，素材信息投递到默认目标下；
- 数据来源留痕：清单带 provenance（来源 URL、素材 sha256、转写引擎/模型、
  抽帧统计、生成时间）；笔记完成回执写 *.provenance.json。

配置持久化在 config/summary_settings.json（gitignore 内，属本机配置）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.errors import not_found, validation
from app.utils.timeutil import now_iso

_SETTINGS_FILE = "summary_settings.json"

_BUILTIN_TARGETS: dict[str, dict[str, str]] = {
    "workbuddy": {"kind": "workbuddy"},
    "codex": {"kind": "directory", "root": "summary_targets/codex"},
    "trae": {"kind": "directory", "root": "summary_targets/trae"},
    "zcode": {"kind": "directory", "root": "summary_targets/zcode"},
}

_KNOWN_TARGET_NAMES = tuple(_BUILTIN_TARGETS)


def _settings_path() -> Path:
    return settings.workspace_root / "config" / _SETTINGS_FILE


def load_summary_settings() -> dict[str, Any]:
    """读取目标配置；文件缺失/损坏时返回内置默认（读路径无副作用，不落盘）。"""
    path = _settings_path()
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            data = {}
    default = data.get("default_target")
    targets = data.get("targets")
    if default not in _KNOWN_TARGET_NAMES:
        default = "workbuddy"
    merged = {name: dict(spec) for name, spec in _BUILTIN_TARGETS.items()}
    if isinstance(targets, dict):
        for name, spec in targets.items():
            if name in _BUILTIN_TARGETS and isinstance(spec, dict):
                merged[name].update(spec)
    return {"default_target": default, "targets": merged}


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_default_target() -> str:
    return str(load_summary_settings()["default_target"])


def set_default_target(name: str) -> dict[str, Any]:
    if name not in _BUILTIN_TARGETS:
        raise not_found("SUMMARY_TARGET_NOT_FOUND")
    data = load_summary_settings()
    data["default_target"] = name
    _atomic_write(_settings_path(), data)
    return data


def list_targets() -> dict[str, Any]:
    data = load_summary_settings()
    targets = []
    for name, spec in data["targets"].items():
        targets.append(
            {
                "name": name,
                "kind": spec.get("kind"),
                "is_default": name == data["default_target"],
                "root": spec.get("root"),
            }
        )
    return {"default_target": data["default_target"], "targets": targets}


def resolve_target(name: str | None = None) -> dict[str, Any]:
    """解析目标为投递参数：workbuddy 维持现契约；目录目标给 pending/output 根。"""
    data = load_summary_settings()
    name = name or data["default_target"]
    spec = data["targets"].get(name)
    if not isinstance(spec, dict):
        raise not_found("SUMMARY_TARGET_NOT_FOUND")
    kind = spec.get("kind")
    if kind == "workbuddy":
        return {"name": name, "kind": "workbuddy", "pending_dir": settings.pending_dir, "expected_root": settings.obsidian_root}
    if kind == "directory":
        raw_root = str(spec.get("root") or "").strip()
        if not raw_root:
            raise validation("SUMMARY_TARGET_ROOT_MISSING")
        root = Path(raw_root)
        if not root.is_absolute():
            root = settings.workspace_root / root
        return {"name": name, "kind": "directory", "pending_dir": root / "pending", "expected_root": root / "output"}
    raise validation("SUMMARY_TARGET_KIND_UNKNOWN")


def sha256_of(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def build_provenance(work, *, transcript_path: Path | None = None, video_path: Path | None = None, frames_count: int | None = None) -> dict[str, Any]:
    """数据来源留痕：来源、素材指纹与生成环境；读不到的项如实留空，不编造。"""
    media_files = []
    for label, path in (("video", video_path), ("transcript", transcript_path)):
        if path is not None:
            media_files.append({"role": label, "path": str(path), "sha256": sha256_of(path), "bytes": path.stat().st_size if path.is_file() else None})
    return {
        "source_url": work.url,
        "platform": work.platform,
        "external_id": work.external_id,
        "generated_at": now_iso(),
        "media_files": media_files,
        "transcribe": {"engine": "paraformer-zh", "local_inference": True} if transcript_path else None,
        "frames_count": frames_count,
    }
