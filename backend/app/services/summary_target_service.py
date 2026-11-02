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
import shutil
from datetime import datetime, timedelta, timezone
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


def _parse_iso_z(value: Any) -> "datetime | None":
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _cli_probe(name: str) -> dict[str, Any]:
    """执行者 CLI/App 可用性探测（只探测本机安装事实，不装也不编造）。"""
    if name == "zcode":
        from monitor.note_dispatcher import _default_zcode_command

        detected = _default_zcode_command()
        return {"available": detected is not None, "note": "桌面 App 内置无头 CLI" if detected else "未找到 ZCode"}
    if name == "codex":
        found = shutil.which("codex") is not None
        return {"available": found, "note": "codex CLI 在 PATH" if found else "codex CLI 不在 PATH（可用 Codex 桌面端自动化替代）"}
    if name == "trae":
        found = shutil.which("trae-cli") is not None
        return {"available": found, "note": "trae-cli 在 PATH" if found else "trae-agent 未安装（github.com/bytedance/trae-agent）"}
    # workbuddy：App 内的自动化后端无法探测，如实 unknown
    return {"available": None, "note": "由 WorkBuddy App 定时自动化执行，后端无法探测"}


def health_report() -> dict[str, Any]:
    """各执行者健康报告（证据导向：只看队列里的真实消费记录，不编造连通性）。

    status 判定优先级：
      quota_exhausted（有额度耗尽回执，需人工切换）
      > active（7 天内有真实完成记录）
      > no-consumer（有 pending 但从未有 done）
      > idle（有 done 但已超过 7 天）
      > empty（队列空且无记录，无需消费者）
    """
    data = load_summary_settings()
    now = datetime.now(timezone.utc)
    targets = []
    for name in data["targets"]:
        resolved = resolve_target(name)
        pending_dir = Path(resolved["pending_dir"])
        pending_count = done_count = error_count = quota_hits = 0
        last_done_at: "datetime | None" = None
        last_executor: str | None = None
        if pending_dir.is_dir():
            for f in pending_dir.glob("*.json"):
                try:
                    pkg = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                status = pkg.get("status")
                if status == "pending":
                    pending_count += 1
                elif status == "done":
                    done_count += 1
                    dispatch = pkg.get("dispatch") or {}
                    ts = _parse_iso_z(dispatch.get("completed_at"))
                    if ts and (last_done_at is None or ts > last_done_at):
                        last_done_at, last_executor = ts, dispatch.get("executor")
            for e in pending_dir.glob("*.error.json"):
                try:
                    text = e.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                error_count += 1
                if "quota_exhausted" in text:
                    quota_hits += 1
        if quota_hits:
            status = "quota_exhausted"
        elif last_done_at is not None and now - last_done_at <= timedelta(days=7):
            status = "active"
        elif pending_count > 0 and done_count == 0:
            status = "no-consumer"
        elif done_count:
            status = "idle"
        else:
            status = "empty"
        targets.append(
            {
                "name": name,
                "kind": resolved["kind"],
                "is_default": name == data["default_target"],
                "pending_count": pending_count,
                "done_count": done_count,
                "error_count": error_count,
                "quota_exhausted_count": quota_hits,
                "last_done_at": last_done_at.isoformat() if last_done_at else None,
                "last_executor": last_executor,
                "status": status,
                "cli": _cli_probe(name),
            }
        )
    return {"default_target": data["default_target"], "targets": targets}


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
