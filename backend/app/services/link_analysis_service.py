"""用户明确提交的一条视频链接：校验身份并幂等创建手动作品。

v2：新增单链接最低可用下载入口（``identify`` / ``quick_download``）——
只识别、只下载用户提交的这一条作品，不启动也不消费全局队列。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy.orm import Session

from app.core.config import ARCHIVE_PLATFORM_LABELS, settings
from app.utils.timeutil import now_iso
from app.core.errors import conflict, not_found, pipeline_offline, validation
from app.repositories import task_repo, work_repo
from app.services.archive_metadata_resolver import resolve_archive_metadata
from app.schemas.link_analysis import (
    LinkAnalysisCreate,
    LinkAnalysisDownloadResult,
    LinkAnalysisIdentifyResult,
    LinkAnalysisResult,
)
from app.schemas.work import Work as WorkSchema

logger = logging.getLogger("app.services.link_analysis")


@dataclass(frozen=True)
class LinkIdentity:
    platform: Literal["douyin", "bilibili", "xhs", "twitter"]
    external_id: str


_BILIBILI_ID = re.compile(r"/video/(BV[0-9A-Za-z]+)", re.I)
_DOUYIN_ID = re.compile(r"/video/(\d+)")
_XHS_ID = re.compile(r"/(?:explore|discovery/item)/([0-9a-zA-Z]+)")
_TWITTER_ID = re.compile(r"/status/(\d+)")
_SHORT_LINK_HOSTS = {"b23.tv", "v.douyin.com", "iesdouyin.com", "xhslink.com", "xhslink.cn", "t.co"}


def _resolve_short_link(url: str) -> str:
    try:
        current = url
        seen: set[str] = set()
        for _ in range(6):
            if current in seen:
                break
            seen.add(current)
            response = httpx.get(current, follow_redirects=False, timeout=10.0)
            location = response.headers.get("location") if 300 <= response.status_code < 400 else None
            if not location:
                break
            target = urljoin(current, location)
            if (urlsplit(target).hostname or "").lower() not in _SHORT_LINK_HOSTS:
                return target
            current = target
        raise validation("LINK_REDIRECT_FAILED")
    except httpx.HTTPError as exc:
        raise validation("LINK_REDIRECT_FAILED") from exc


def _spawn_single_link_runner(work_id: int, *, download_only: bool = True) -> bool:
    """后台拉起既有单作品 CLI；download_only=True 仅下载，False 走完整五步流水线。

    子进程继承后端环境（同一 venv/数据/媒体配置）；stdout/stderr 落到 logs/
    便于失败排查。启动失败返回 False，由调用方报"执行器不可用"。
    """
    root = settings.workspace_root
    logs_dir = root / "logs"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(logs_dir / f"single_link_work_{work_id}.log", "ab")  # noqa: SIM115
    except OSError:
        return False
    try:
        subprocess.Popen(  # noqa: S603 - 固定参数白名单命令，无用户可控片段
            [
                sys.executable,
                str(root / "tools" / "process_link_analysis.py"),
                "--work-id",
                str(work_id),
                *(["--download-only"] if download_only else []),
            ],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError:
        log_file.close()
        return False
    finally:
        log_file.close()
    return True


def parse_link_identity(
    url: str, resolve_redirect: Callable[[str], str] | None = None
) -> LinkIdentity:
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except (AttributeError, TypeError, ValueError):
        raise validation("LINK_IDENTITY_UNSUPPORTED") from None
    host = parsed.hostname.lower() if parsed.hostname else ""
    path = parsed.path
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 80, 443)
    ):
        raise validation("LINK_IDENTITY_UNSUPPORTED")

    if host in _SHORT_LINK_HOSTS:
        resolved = resolve_redirect(url) if resolve_redirect else _resolve_short_link(url)
        return parse_link_identity(resolved)

    if host == "bilibili.com" or host.endswith(".bilibili.com"):
        match = _BILIBILI_ID.search(path)
        if match:
            return LinkIdentity("bilibili", match.group(1))
    elif host == "douyin.com" or host.endswith(".douyin.com") or host.endswith(".iesdouyin.com"):
        match = _DOUYIN_ID.search(path)
        if match:
            return LinkIdentity("douyin", match.group(1))
    elif host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"):
        match = _XHS_ID.search(path)
        if match:
            return LinkIdentity("xhs", match.group(1))
    elif host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}:
        match = _TWITTER_ID.search(path)
        if match:
            return LinkIdentity("twitter", match.group(1))
    raise validation("LINK_IDENTITY_UNSUPPORTED")


class LinkAnalysisService:
    @staticmethod
    def _register_manual_work(
        db: Session,
        platform: str,
        external_id: str,
        url: str,
        *,
        content_type: str = "video",
        media_urls: list[str] | None = None,
        title: str | None = None,
    ):
        """幂等创建/更新手动作品 + 五阶段任务（不 commit，由调用方控制事务）。"""
        work = work_repo.upsert(
            db,
            platform,
            external_id,
            title=title or f"待解析 {external_id}",
            url=url,
            source_type="manual",
            content_type=content_type,
            status="QUEUED",
        )
        if media_urls:
            work.media_urls = json.dumps(media_urls, ensure_ascii=False)
        if platform == "bilibili":
            resolution = resolve_archive_metadata(
                work,
                query_bilibili_detail=True,
            )
            if resolution.title.verified and "title" not in resolution.conflicts and resolution.title.value:
                work.title = str(resolution.title.value)
            publication = resolution.published_at
            if publication.verified and "published_at" not in resolution.conflicts and publication.value:
                work.published_at = str(publication.value)
                work.published_time_source = publication.source
                work.published_time_precision = publication.precision
                work.published_time_timezone = publication.source_timezone
            if resolution.metadata_checked_at:
                work.metadata_checked_at = resolution.metadata_checked_at
        for stage in ("parse", "download", "transcribe", "frames", "note"):
            task_repo.upsert(db, work.id, stage, "pending", attempts=0)
        db.flush()
        return work

    def ingest(self, db: Session, request: LinkAnalysisCreate) -> LinkAnalysisResult:
        identity = parse_link_identity(request.url)
        if identity.platform != request.platform or identity.external_id != request.external_id:
            raise validation("LINK_IDENTITY_MISMATCH")

        existing = work_repo.get_by_platform_external(db, request.platform, request.external_id)
        if existing is not None:
            # FAILED 作品重新入队重试（与单链接重试同一语义：按 ID 合并不重复）
            if request.mode == "one_time" and existing.status == "FAILED":
                from monitor.pipeline.single_link import requeue_failed_for_download

                if not requeue_failed_for_download(db, existing.id):
                    raise conflict("NOT_RETRYABLE")
                db.commit()
                db.refresh(existing)
            # 已存在的待处理作品同样立即执行（幂等：处理中/已完成不重复拉起）
            if (
                request.mode == "one_time"
                and existing.status in {"NEW", "QUEUED"}
                and not _spawn_single_link_runner(existing.id, download_only=False)
            ):
                raise pipeline_offline("SINGLE_LINK_RUNNER_UNAVAILABLE")
            return LinkAnalysisResult(
                work=WorkSchema.model_validate(existing),
                created=False,
                mode=request.mode,
            )

        work = self._register_manual_work(
            db, request.platform, request.external_id, request.url
        )
        db.commit()
        db.refresh(work)
        if request.mode == "one_time" and work.status in {"NEW", "QUEUED"}:
            # one_time 语义：立即对这一条作品执行完整流水线（下载→转写→抽帧→就绪清单），
            # 不写触发文件、不消费全局队列；执行器拉起失败如实报错。
            if not _spawn_single_link_runner(work.id, download_only=False):
                raise pipeline_offline("SINGLE_LINK_RUNNER_UNAVAILABLE")
        return LinkAnalysisResult(
            work=WorkSchema.model_validate(work),
            created=True,
            mode=request.mode,
        )

    @staticmethod
    def identify(url: str) -> LinkAnalysisIdentifyResult:
        """只识别链接，不产生任何副作用（短链解析会出网）。"""
        identity = parse_link_identity(url)
        return LinkAnalysisIdentifyResult(
            platform=identity.platform, external_id=identity.external_id
        )

    @staticmethod
    def _probe_twitter_content(external_id: str) -> tuple[str, list[str] | None, str | None]:
        """推特匿名预探测：返回 (content_type, media_urls, title_hint)。"""
        from monitor.platforms.twitter_syndication import fetch_tweet_media

        probe = fetch_tweet_media(external_id)
        if probe is None:
            return "video", None, None
        if probe.has_video:
            return "video", None, (probe.text.strip()[:80] or None)
        if probe.photos:
            return "image", probe.photos, (probe.text.strip()[:80] or None)
        return "video", None, (probe.text.strip()[:80] or None)

    def quick_download(self, db: Session, url: str) -> LinkAnalysisDownloadResult:
        """单链接最低可用下载：识别 → 幂等建作品 → 拉起"只下载这一条"执行器。

        一次操作只处理这一条作品：不写触发文件、不启动/消费全局队列、
        不注册或启动任何后台任务。执行器为既有单作品 CLI 子进程。
        """
        identity = parse_link_identity(url)
        platform_label = ARCHIVE_PLATFORM_LABELS.get(identity.platform, identity.platform)

        existing = work_repo.get_by_platform_external(db, identity.platform, identity.external_id)
        if existing is None:
            content_type, media_urls, title_hint = "video", None, None
            if identity.platform == "twitter":
                content_type, media_urls, title_hint = self._probe_twitter_content(identity.external_id)
            work = self._register_manual_work(
                db,
                identity.platform,
                identity.external_id,
                url,
                content_type=content_type,
                media_urls=media_urls,
                title=title_hint,
            )
            db.commit()
            db.refresh(work)
            created = True
            message = f"已识别为{platform_label}作品，开始下载（仅这一条）"
        else:
            work = existing
            created = False
            if work.status == "FAILED":
                from monitor.pipeline.single_link import requeue_failed_for_download

                if not requeue_failed_for_download(db, work.id):
                    raise conflict("NOT_RETRYABLE")
                db.commit()
                db.refresh(work)
                message = f"该{platform_label}作品此前下载失败，已重新开始下载（仅这一条）"
            elif work.status in {"NEW", "QUEUED", "DOWNLOADING"}:
                message = f"该{platform_label}作品正在处理中，可稍后刷新查看结果"
            elif work.status in {"DOWNLOADED", "DONE"}:
                message = f"该{platform_label}作品已下载，可直接查看本地结果"
            else:
                message = f"该{platform_label}作品已在后续处理阶段，暂不重复下载"

        processing = work.status in {"NEW", "QUEUED", "DOWNLOADING"}
        if processing and not _spawn_single_link_runner(work.id):
            raise pipeline_offline("SINGLE_LINK_RUNNER_UNAVAILABLE")

        return LinkAnalysisDownloadResult(
            work=WorkSchema.model_validate(work),
            created=created,
            platform=identity.platform,
            external_id=identity.external_id,
            content_type=str(work.content_type or "video"),
            processing=processing,
            message=message,
        )

    def complete_note(
        self, db: Session, work_id: int, note_path: str, executor: str | None = None, target: str | None = None
    ) -> WorkSchema:
        """确认笔记完成：允许 Obsidian（workbuddy）或任一目录目标的 output 根。

        完成时写 *.provenance.json 回执（执行者/目标/来源/时间）；回执写入
        失败不影响状态推进，如实反映在 provenance_written。
        """
        from app.services import summary_target_service

        relative = Path(note_path.replace("\\", "/"))
        if relative.is_absolute() or relative.drive:
            raise validation("NOTE_PATH_INVALID")
        roots = [settings.obsidian_root.resolve()]
        for spec in summary_target_service.load_summary_settings()["targets"].values():
            if spec.get("kind") == "directory":
                roots.append((settings.workspace_root / str(spec.get("root", ""))).resolve() / "output")
        candidate = None
        matched_root = None
        for root in roots:
            probe = (root / relative).resolve()
            try:
                probe.relative_to(root)
            except ValueError:
                continue
            if probe.is_file():
                candidate, matched_root = probe, root
                break
        if candidate is None or candidate.suffix.lower() != ".md":
            raise validation("NOTE_PATH_INVALID") from None

        work = work_repo.get(db, work_id)
        if work is None:
            raise not_found("WORK_NOT_FOUND")
        work_repo.update(
            db,
            work,
            note_path=candidate.relative_to(matched_root).as_posix(),
            status="DONE",
        )
        task = task_repo.upsert(db, work.id, "note", "succeeded", attempts=1)
        task.error = None
        db.commit()
        db.refresh(work)

        provenance_written = False
        try:
            receipt = {
                "work_id": work.id,
                "source_url": work.url,
                "note_sha256": summary_target_service.sha256_of(candidate),
                "executor": executor,
                "target": target or summary_target_service.get_default_target(),
                "completed_at": now_iso(),
            }
            receipt_path = candidate.with_name(candidate.name + ".provenance.json")
            receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            provenance_written = receipt_path.is_file()
        except OSError:
            provenance_written = False
        return WorkSchema.model_validate(work)
        return WorkSchema.model_validate(work)


link_analysis_service = LinkAnalysisService()
