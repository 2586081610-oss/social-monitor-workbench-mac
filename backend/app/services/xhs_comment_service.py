"""小红书单作品评论采集任务：文件型任务状态 + 子进程执行器 + 结果读取。

只处理用户提交的一条作品。任务状态与结果都保存在 ``workbuddy/xhs_comments/``
（本地私有目录，不进入 Git）：任务进度可轮询，结果与报告原子落盘，重试按评论
ID 合并，不会重复增加评论或覆盖已有的语音/图片成果。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from app.core.config import settings
from app.core.errors import conflict, not_found, validation
from app.services.link_analysis_service import parse_link_identity

logger = logging.getLogger("app.services.xhs_comments")

_URL_IN_TEXT = re.compile(r"https?://[^\s，。；、）】]+", re.I)
_NOTE_ID = re.compile(r"^[0-9A-Za-z]{8,40}$")
_IMAGE_NAME = re.compile(r"^c_[0-9A-Za-z]+_\d+\.(jpg|jpeg|png|webp|gif|avif)$", re.I)
_TERMINAL = {"completed", "incomplete", "failed"}
_ACTIVE = {"queued", "running"}
_CREATE_LOCK = threading.Lock()

MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
}


def _extract_url(text: str) -> str:
    match = _URL_IN_TEXT.search(text or "")
    return match.group(0) if match else (text or "").strip()


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


def _parse_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


class XhsCommentService:
    def __init__(self) -> None:
        self.store_root = Path(settings.workbuddy_dir) / "xhs_comments"
        self.jobs_dir = self.store_root / "jobs"

    # ------------------------------------------------------------------ paths
    def _note_dir(self, note_id: str) -> Path:
        if not _NOTE_ID.fullmatch(note_id or ""):
            raise validation("NOTE_ID_INVALID")
        return self.store_root / note_id

    def _job_path(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9]{8,20}-[0-9a-f]{6,16}", job_id or ""):
            raise validation("JOB_ID_INVALID")
        return self.jobs_dir / f"{job_id}.json"

    def _result_path(self, note_id: str) -> Path:
        return self._note_dir(note_id) / "comments.json"

    def _report_path(self, note_id: str) -> Path:
        return self._note_dir(note_id) / "report.md"

    # ------------------------------------------------------------------ public
    def _fixture_root(self) -> Path | None:
        raw = (os.getenv("XHS_COMMENT_FIXTURE_DIR") or "").strip()
        return Path(raw).expanduser().resolve() if raw else None

    def _mediacrawler_python(self) -> Path | None:
        mc_dir = Path(settings.mediacrawler_dir)
        for candidate in (mc_dir / ".venv" / "bin" / "python", mc_dir / ".venv" / "Scripts" / "python.exe"):
            if candidate.is_file():
                return candidate
        return None

    def _active_job_for_note(self, note_id: str) -> dict | None:
        for path in sorted(self.jobs_dir.glob("*.json"), reverse=True):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if job.get("note_id") != note_id or job.get("status") not in _ACTIVE:
                continue
            job = self._reconcile(job)
            if job.get("status") in _ACTIVE:
                return job
        return None

    def create_job(
        self,
        *,
        url_input: str,
        include_images: bool = True,
        include_voice: bool = False,
        voice_authorized: bool = False,
        voice_allow_open_page: bool = False,
    ) -> dict:
        url = _extract_url(url_input)
        identity = parse_link_identity(url)
        if identity.platform != "xhs":
            raise validation("XHS_NOTE_REQUIRED")
        note_id = identity.external_id
        if include_voice and not voice_authorized:
            raise validation("VOICE_AUTHORIZATION_REQUIRED")
        if voice_allow_open_page and not (include_voice and voice_authorized):
            raise validation("VOICE_AUTHORIZATION_REQUIRED")

        with _CREATE_LOCK:
            active = self._active_job_for_note(note_id)
            if active:
                return self._public_job(active)
            job_id = f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
            fixture_root = self._fixture_root()
            mode = "fixture" if fixture_root else "live"
            job = {
                "job_id": job_id,
                "note_id": note_id,
                "url": url,
                "status": "queued",
                "phase": "queued",
                "mode": mode,
                "include_images": bool(include_images),
                "include_voice": bool(include_voice),
                "voice_authorized": bool(include_voice) and bool(voice_authorized),
                "voice_allow_open_page": bool(include_voice) and bool(voice_allow_open_page),
                "created_at": _now(),
                "updated_at": _now(),
                "pid": None,
                "error": None,
                "result_available": False,
                "progress": {
                    "pages_fetched": 0,
                    "comments_fetched": 0,
                    "main_comments": 0,
                    "replies_fetched": 0,
                    "images_total": 0,
                    "images_done": 0,
                    "images_failed": 0,
                    "voice_status": "requested" if include_voice else "not_requested",
                    "message": "任务已创建",
                },
            }
            self.jobs_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self.jobs_dir / f"{job_id}.json", job)
            self._spawn(job, fixture_root)
            return self._public_job(job)

    def _spawn(self, job: dict, fixture_root: Path | None) -> None:
        root = Path(settings.workspace_root)
        notes_dir = str(self.store_root)
        if job["mode"] == "fixture":
            fixture_dir = fixture_root / job["note_id"] if fixture_root else None
            if fixture_dir is None or not (fixture_dir / "pages.json").is_file():
                self._fail_job(job, "模拟数据目录缺少该作品的 pages.json，无法执行模拟采集")
                return
            interpreter = Path(sys.executable)
            fixture_arg = ["--fixture", str(fixture_dir)]
        else:
            interpreter = self._mediacrawler_python()
            if interpreter is None:
                self._fail_job(job, "真实采集执行器不可用（未找到 MediaCrawler 运行环境）")
                return
            fixture_arg = []
        command = [
            str(interpreter),
            str(root / "tools" / "xhs_comment_collect.py"),
            "--job",
            str(self._job_path(job["job_id"])),
            "--store-dir",
            notes_dir,
            "--url",
            job["url"],
            "--note-id",
            job["note_id"],
            *fixture_arg,
        ]
        if job["include_images"]:
            command.append("--include-images")
        if job["include_voice"]:
            command.append("--include-voice")
        if job["include_voice"] and job.get("voice_allow_open_page"):
            command.append("--voice-allow-open-page")
        logs_dir = self.store_root / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_file = open(logs_dir / f"{job['job_id']}.log", "ab")  # noqa: SIM115
        except OSError:
            self._fail_job(job, "任务日志无法创建，执行器未启动")
            return
        try:
            process = subprocess.Popen(  # noqa: S603 - 固定参数与已校验的作品 ID
                command,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError:
            self._fail_job(job, "采集执行器未能启动，请检查后端服务")
            return
        finally:
            log_file.close()
        job["pid"] = process.pid
        job["status"] = "running"
        job["phase"] = "starting"
        job["updated_at"] = _now()
        _atomic_write_json(self._job_path(job["job_id"]), job)

    def _fail_job(self, job: dict, message: str) -> None:
        job["status"] = "failed"
        job["phase"] = "failed"
        job["error"] = message
        job["updated_at"] = _now()
        _atomic_write_json(self._job_path(job["job_id"]), job)

    # ------------------------------------------------------------------ jobs
    def _reconcile(self, job: dict) -> dict:
        if job.get("status") not in _ACTIVE:
            return job
        pid = job.get("pid")
        if _pid_alive(pid):
            return job
        result_path = self._result_path(job.get("note_id") or "")
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                result = {}
            status = (result.get("completeness") or {}).get("status")
            created = _parse_time(job.get("created_at"))
            collected = _parse_time(result.get("collected_at"))
            fresh = bool(created and collected and collected >= created)
            if status in {"complete", "incomplete", "failed"} and fresh:
                job["status"] = {"complete": "completed", "incomplete": "incomplete", "failed": "failed"}[status]
                job["phase"] = "completed" if status != "failed" else "failed"
                job["result_available"] = True
                job["error"] = None if status != "failed" else (result.get("completeness") or {}).get("fatal_error")
                job["updated_at"] = _now()
                _atomic_write_json(self._job_path(job["job_id"]), job)
                return job
        if job.get("status") == "queued" and pid is None:
            return job
        job["status"] = "failed"
        job["phase"] = "failed"
        job["error"] = "采集执行器已退出（未留下可用结果），可重新采集"
        job["updated_at"] = _now()
        _atomic_write_json(self._job_path(job["job_id"]), job)
        return job

    def get_job(self, job_id: str) -> dict:
        path = self._job_path(job_id)
        if not path.is_file():
            raise not_found("COMMENT_JOB_NOT_FOUND")
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raise not_found("COMMENT_JOB_NOT_FOUND") from None
        return self._public_job(self._reconcile(job))

    @staticmethod
    def _public_job(job: dict) -> dict:
        return {
            "job_id": job.get("job_id"),
            "note_id": job.get("note_id"),
            "url": job.get("url"),
            "status": job.get("status"),
            "phase": job.get("phase"),
            "mode": job.get("mode"),
            "include_images": job.get("include_images"),
            "include_voice": job.get("include_voice"),
            "voice_authorized": job.get("voice_authorized", False),
            "voice_allow_open_page": job.get("voice_allow_open_page", False),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "error": job.get("error"),
            "result_available": job.get("result_available"),
            "progress": job.get("progress") or {},
        }

    def retry_job(
        self,
        job_id: str,
        *,
        voice_authorized: bool = False,
        voice_allow_open_page: bool | None = None,
    ) -> dict:
        job = self.get_job(job_id)
        if job.get("status") in _ACTIVE:
            raise conflict("COMMENT_JOB_ALREADY_RUNNING")
        if job.get("include_voice") and not voice_authorized:
            raise validation("VOICE_AUTHORIZATION_REQUIRED")
        allow_open_page = (
            bool(job.get("voice_allow_open_page")) if voice_allow_open_page is None else bool(voice_allow_open_page)
        )
        return self.create_job(
            url_input=job["url"],
            include_images=bool(job.get("include_images")),
            include_voice=bool(job.get("include_voice")),
            voice_authorized=voice_authorized,
            voice_allow_open_page=allow_open_page,
        )

    # ------------------------------------------------------------------ results
    def get_result(self, note_id: str) -> dict:
        path = self._result_path(note_id)
        if not path.is_file():
            raise not_found("COMMENTS_NOT_COLLECTED")
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raise not_found("COMMENTS_NOT_COLLECTED") from None
        result["report_available"] = self._report_path(note_id).is_file()
        return result

    def report_path(self, note_id: str) -> Path:
        path = self._report_path(note_id)
        if not path.is_file():
            raise not_found("COMMENT_REPORT_NOT_FOUND")
        return path

    def image_file(self, note_id: str, filename: str) -> tuple[Path, str]:
        if not _IMAGE_NAME.fullmatch(filename or ""):
            raise not_found("COMMENT_IMAGE_NOT_FOUND")
        directory = (self._note_dir(note_id) / "images").resolve()
        path = (directory / filename).resolve()
        try:
            path.relative_to(directory)
        except ValueError:
            raise not_found("COMMENT_IMAGE_NOT_FOUND") from None
        if not path.is_file():
            raise not_found("COMMENT_IMAGE_NOT_FOUND")
        return path, MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")

    def list_notes(self, limit: int = 50) -> list[dict]:
        """已保存结果摘要；刷新页面后据此重新打开完整结果。"""
        items: list[dict] = []
        if not self.store_root.is_dir():
            return items
        for directory in self.store_root.iterdir():
            if not directory.is_dir() or not _NOTE_ID.fullmatch(directory.name):
                continue
            path = directory / "comments.json"
            if not path.is_file():
                continue
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            completeness = result.get("completeness") or {}
            note_meta = result.get("note_meta") or {}
            collector = result.get("collector") or {}
            items.append(
                {
                    "note_id": result.get("note_id") or directory.name,
                    "title": note_meta.get("title"),
                    "source_url": result.get("source_url"),
                    "collected_at": result.get("collected_at"),
                    "first_collected_at": result.get("first_collected_at"),
                    "mode": collector.get("mode"),
                    "completeness_status": completeness.get("status"),
                    "completeness_reasons": completeness.get("reasons") or [],
                    "counts": result.get("counts") or {},
                    "pinned_comment_id": result.get("pinned_comment_id"),
                    "voice_status": (result.get("voice_run") or {}).get("status"),
                    "report_available": (directory / "report.md").is_file(),
                }
            )
        items.sort(key=lambda item: item.get("collected_at") or "", reverse=True)
        return items[: max(1, min(limit, 200))]


xhs_comment_service = XhsCommentService()
