"""note 业务：生成/刷新「素材就绪清单」（workbuddy/pending/*.json，SPEC §10 契约）。"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import not_found
from app.repositories import task_repo, work_repo
from app.utils.filename import sanitize_filename
from app.utils.timeutil import to_date


def _creator_name(work) -> str:
    return work.creator.name if work.creator else work.external_id


def _group_name(work) -> str:
    return work.group.name if work.group else "待分组"


def _media_file(path: Path) -> Path | None:
    """清单留痕用：素材文件存在才返回，绝不编造路径。"""
    return path if path.is_file() else None


def _first_video(media_prefix_dir: Path) -> Path | None:
    """media 前缀目录下的首个视频文件（按名排序，稳定选择）。"""
    if not media_prefix_dir.is_dir():
        return None
    for candidate in sorted(media_prefix_dir.glob("*.mp4")):
        return candidate
    return None


def _frames_count(frames_dir: Path) -> int | None:
    """从 frame_manifest.json 读抽帧条数；清单不存在返回 None（未抽帧）。"""
    manifest = frames_dir / "frame_manifest.json"
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    entries = data.get("frames") if isinstance(data, dict) else data
    return len(entries) if isinstance(entries, list) else None


def write_pending_manifest(work, *, target: str | None = None) -> dict:
    """写"素材就绪清单"到指定总结目标的 pending 目录，返回相对工作区根路径。

    target=None 或 "workbuddy" 维持原契约（workbuddy/pending/ + Obsidian 输出）；
    其他目标（codex/trae/zcode/自定义）投递到各自目录，expected_output 指向
    该目标自己的 output/。清单统一带 provenance（数据来源留痕）。
    """
    from app.services import summary_target_service

    resolved = summary_target_service.resolve_target(target)
    pending_dir: Path = Path(resolved["pending_dir"])
    expected_root: Path = Path(resolved["expected_root"])
    pending_dir.mkdir(parents=True, exist_ok=True)

    # Keep the existing path naming convention while preserving unknown publication facts.
    publish_date = to_date(work.published_at) or "unknown"
    date = to_date(work.published_at) or to_date(work.created_at) or "unknown"
    safe_title = sanitize_filename(work.title)
    safe_creator = sanitize_filename(_creator_name(work))
    group = _group_name(work)

    media_prefix = work.media_path or f"{group}/{safe_creator}/{date}-{safe_title}"
    transcript_path = work.transcript_path or f"{media_prefix}/transcript/transcript.json"
    frames_dir = f"{media_prefix}/frames"
    if resolved["kind"] == "workbuddy":
        expected_output = f"Obsidian/{group}/{safe_creator}/{date}-{safe_title}.md"
    else:
        out_path = expected_root / f"{date}-{safe_title}.md"
        try:
            expected_output = str(out_path.resolve().relative_to(settings.workspace_root.resolve()))
        except ValueError:
            expected_output = str(out_path)

    manifest = {
        "work_id": work.id,
        "platform": work.platform,
        "blogger": _creator_name(work),
        "title": work.title,
        "url": work.url,
        "publish_date": publish_date,
        "transcript_path": transcript_path,
        "frames_dir": frames_dir,
        "lexicon_hits": [],  # 流水线抽帧后回填
        "expected_output": expected_output.replace("\\", "/"),
        "status": "pending",
        "target": resolved["name"],
        "provenance": summary_target_service.build_provenance(
            work,
            transcript_path=_media_file(settings.media_root / transcript_path),
            video_path=_first_video(settings.media_root / media_prefix),
            frames_count=_frames_count(settings.media_root / frames_dir),
        ),
    }
    filename = f"{work.id}-{date}-{safe_title}.json"
    path = pending_dir / filename
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        rel = str(path.relative_to(settings.workspace_root))
    except ValueError:
        # 目录不在工作区根下（如测试临时目录）：workbuddy 按契约路径返回，其他目标给绝对路径
        if resolved["kind"] == "workbuddy":
            rel = str(Path("workbuddy") / "pending" / filename)
        else:
            rel = str(path)
    return rel.replace("\\", "/")


class NoteService:
    def analyze(self, db: Session, work_id: int) -> dict:
        work = work_repo.get(db, work_id)
        if work is None:
            raise not_found("WORK_NOT_FOUND")
        pending_file = write_pending_manifest(work)
        work_repo.update(db, work, status="NOTE_GENERATING")
        task_repo.upsert(db, work_id, "note", "pending", error=None, attempts=0)
        db.commit()
        return {"note_pending": True, "pending_file": pending_file}


note_service = NoteService()
