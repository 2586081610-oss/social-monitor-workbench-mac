"""监控层任务：心跳、轮询、trigger 消费、可选流水线执行。"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

# 先导入 shared_db（把 backend 加入 sys.path），再导入 app.* 与 monitor.*
from monitor.store.shared_db import SessionLocal, settings  # noqa: E402

from app.models import ArchiveEntry, Creator, Work  # noqa: E402
from app.repositories import health_repo, lexicon_repo, task_repo, work_repo  # noqa: E402
from app.repositories import archive_repo  # noqa: E402
from app.services.archive_checkpoint_service import (  # noqa: E402
    archive_metadata_seed,
    assert_work_archive_identity,
    publication_provenance_from_work,
    record_download_checkpoint,
    register_legacy_media,
    resolve_work_directory,
)
from app.services.archive_metadata_resolver import resolve_archive_metadata  # noqa: E402
from app.services.archive_sync_service import sync_archive_entry  # noqa: E402
from app.utils.archive_paths import relative_path_from_root, resolve_archive_path  # noqa: E402
from app.utils.timeutil import now_iso  # noqa: E402

from monitor.pipeline import download, frames, notify, transcribe  # noqa: E402
from monitor.pipeline.download import PipelineOfflineError  # noqa: E402
from monitor.platforms.douyin_cdp import DouyinCDPSession  # noqa: E402
from monitor.platforms.douyin_mediacrawler import DouyinMediaCrawlerAdapter  # noqa: E402
from monitor.platforms.rsshub import RSSHubError, RSSHubRouterAdapter  # noqa: E402
from monitor.platforms.xhs_mediacrawler import XhsMediacrawlerAdapter  # noqa: E402

logger = logging.getLogger("monitor.jobs")

RSSHUB_PLATFORMS = ("bilibili", "twitter")


def _producer_time_fields(item: dict) -> dict:
    """Accept only publication fields that name a real source and precision."""
    fields = {}
    raw = item.get("published_at")
    source = item.get("published_time_source")
    precision = item.get("published_time_precision")
    source_timezone = item.get("published_time_timezone")
    if isinstance(raw, str) and raw and isinstance(source, str) and source.strip() and precision in {"day", "minute", "second"}:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.year > 1970:
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    if precision != "day" or not isinstance(source_timezone, str):
                        raise ValueError("publication source timezone missing")
                    parsed = parsed.replace(tzinfo=ZoneInfo(source_timezone))
                if not isinstance(source_timezone, str) or not source_timezone.strip():
                    source_timezone = "UTC"
                else:
                    ZoneInfo(source_timezone)
                fields.update({
                    "published_at": raw,
                    "published_time_source": source.strip(),
                    "published_time_precision": precision,
                    "published_time_timezone": source_timezone,
                })
        except (ValueError, TypeError, ZoneInfoNotFoundError, OverflowError, OSError):
            pass

    checked_at = item.get("metadata_checked_at")
    if isinstance(checked_at, str) and checked_at:
        try:
            checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
            if checked.year > 1970 and checked.tzinfo is not None and checked.utcoffset() is not None:
                fields["metadata_checked_at"] = checked.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError, OverflowError, OSError):
            pass
    return fields


def write_heartbeat() -> None:
    """写心跳文件：聚合 API 依据其新鲜度判断监控层在线。"""
    settings.workbuddy_dir.mkdir(parents=True, exist_ok=True)
    settings.monitor_heartbeat_file.write_text(
        json.dumps({"ts": time.time(), "pid": os.getpid()}), encoding="utf-8"
    )


def _update_cookie(db: Session, platform: str, status: str, note: str | None) -> None:
    health_repo.upsert_cookie(db, platform, status, note=note, last_checked_at=now_iso())


def _upsert_creator_entries(db: Session, creator: Creator, entries: list[dict]) -> int:
    """把某博主最新作品增量落库 + 登记 parse 任务，返回新增数；更新 last_poll_at。"""
    new_count = 0
    for item in entries:
        media_urls = item.get("media_urls")
        fields = {
            "title": item["title"],
            "url": item["url"],
            "creator_id": creator.id,
            "source_type": "monitor",
            "content_type": item.get("content_type", "video"),
        }
        fields.update(_producer_time_fields(item))
        if isinstance(media_urls, list):
            fields["media_urls"] = json.dumps(media_urls, ensure_ascii=False)
        if "cover_url" in item:
            fields["cover_url"] = item.get("cover_url")
        work = work_repo.upsert(
            db,
            item["platform"],
            item["external_id"],
            **fields,
        )
        if work.status == "NEW":
            new_count += 1
            task_repo.upsert(db, work.id, "parse", "pending")
            work.status = "QUEUED"
            creator.last_work_at = now_iso()
    creator.last_poll_at = now_iso()
    return new_count


def poll_rsshub_platform(platform: str) -> None:
    """单平台轮询：拉取启用博主 → 增量 upsert 新作品 → 登记 parse 任务。"""
    adapter = RSSHubRouterAdapter(settings.rsshub_base_url)
    if not adapter.is_configured():
        logger.warning("RSSHUB_BASE_URL 未配置，跳过 %s 轮询", platform)
        return
    started = now_iso()
    with SessionLocal() as db:
        creators = db.execute(
            select(Creator).where(Creator.platform == platform, Creator.enabled == 1)
        ).scalars().all()
        total_new = 0
        for creator in creators:
            try:
                entries = adapter.fetch_entries(platform, creator.external_id)
            except RSSHubError as exc:
                _update_cookie(db, platform, "WARN", exc.message)
                health_repo.create_poll_log(db, platform, "fail", detail=exc.category, started_at=started)
                db.commit()
                logger.warning("%s 轮询失败 category=%s msg=%s", platform, exc.category, exc.message)
                continue
            new_count = _upsert_creator_entries(db, creator, entries)
            total_new += new_count
            db.commit()
            if new_count:
                logger.info("%s 博主 %s 新增 %d 条", platform, creator.name, new_count)
        _update_cookie(db, platform, "OK", None)
        health_repo.create_poll_log(db, platform, "ok", detail=None, started_at=started, new_works=total_new)
        db.commit()


def poll_rsshub_creator(creator_id: int) -> None:
    """单博主轮询（一次性爬取用）：按 creator.platform 走 RSSHub per-user 路由。

    复用 _upsert_creator_entries 完成增量落库 + 登记 parse 任务，并更新 last_poll_at；
    成功/失败均写 poll_logs（platform 级）。
    """
    adapter = RSSHubRouterAdapter(settings.rsshub_base_url)
    if not adapter.is_configured():
        logger.warning("RSSHUB_BASE_URL 未配置，跳过博主 %s", creator_id)
        return
    started = now_iso()
    with SessionLocal() as db:
        creator = db.get(Creator, creator_id)
        if creator is None:
            raise ValueError(f"creator {creator_id} 不存在")
        platform = creator.platform
        if platform not in RSSHUB_PLATFORMS:
            logger.warning("平台 %s 无 RSSHub 路由，跳过博主 %s", platform, creator_id)
            health_repo.create_poll_log(db, platform, "fail", detail="no_rsshub_route", started_at=started)
            db.commit()
            return
        try:
            entries = adapter.fetch_entries(platform, creator.external_id)
        except RSSHubError as exc:
            _update_cookie(db, platform, "WARN", exc.message)
            health_repo.create_poll_log(db, platform, "fail", detail=exc.category, started_at=started)
            db.commit()
            logger.warning("博主 %s 轮询失败 category=%s msg=%s", creator.name, exc.category, exc.message)
            return
        new_count = _upsert_creator_entries(db, creator, entries)
        _update_cookie(db, platform, "OK", None)
        health_repo.create_poll_log(db, platform, "ok", detail=None, started_at=started, new_works=new_count)
        db.commit()
        if new_count:
            logger.info("博主 %s 新增 %d 条", creator.name, new_count)


def poll_douyin() -> None:
    """抖音监测：复用 MediaCrawler 登录态 + DouYinClient 抓首页最新（低风控，替代 CDP 占位）。

    无 MediaCrawler/无 cookie → fail（poll_logs 记 detail）；单博主异常不阻塞其余。
    """
    adapter = DouyinMediaCrawlerAdapter(settings.mediacrawler_dir)
    started = now_iso()
    with SessionLocal() as db:
        if not adapter.available():
            msg = "MediaCrawler 未就绪（tools/mediacrawler 缺失或 COOKIES 为空，可用 tools/export_dy_cookie.py 导出）"
            _update_cookie(db, "douyin", "UNKNOWN", msg)
            health_repo.create_poll_log(db, "douyin", "failed", detail=msg, started_at=started)
            db.commit()
            return
        creators = db.execute(
            select(Creator).where(Creator.platform == "douyin", Creator.enabled == 1)
        ).scalars().all()
        total_new = 0
        ok_count = 0
        failed_count = 0
        for creator in creators:
            sec_uid = creator.external_id or ""
            if not sec_uid:
                continue
            try:
                entries = adapter.poll_creator(sec_uid)
            except Exception as e:  # noqa: BLE001  单博主失败不阻塞其余
                failed_count += 1
                logger.warning("douyin 博主 %s 爬取异常: %s", creator.name, e)
                health_repo.create_poll_log(db, "douyin", "fail", detail=f"mediacrawler 异常: {str(e)[:120]}", started_at=started)
                continue
            total_new += _upsert_creator_entries(db, creator, entries)
            ok_count += 1
        _update_cookie(db, "douyin", "OK" if ok_count and not failed_count else "FAIL", "MediaCrawler 监测")
        health_repo.create_poll_log(db, "douyin", "ok" if ok_count and not failed_count else "fail", detail=(f"{failed_count} creator poll(s) failed" if failed_count else None), started_at=started, new_works=total_new)
        db.commit()


def _poll_xhs_creators(db, creators) -> int:
    """遍历小红书博主抓最新笔记并落库，返回新增数。"""
    adapter = XhsMediacrawlerAdapter()
    total_new = 0
    for creator in creators:
        profile_url = creator.profile_url or ""
        if not profile_url:
            continue
        try:
            entries = adapter.poll_creator(profile_url)
        except Exception as e:  # noqa: BLE001  单博主失败不阻塞其余
            logger.warning("xhs 博主 %s 爬取异常: %s", creator.name, e)
            continue
        total_new += _upsert_creator_entries(db, creator, entries)
    return total_new


def poll_xhs() -> None:
    """小红书监测：Playwright 拦截 user_posted API 抓博主最新笔记（替代 RSSHub xhs 路由）。"""
    adapter = XhsMediacrawlerAdapter()
    started = now_iso()
    with SessionLocal() as db:
        if not adapter.available():
            msg = "xhs 抓取工具缺失（tools/xhs_fetch_notes.py）"
            _update_cookie(db, "xhs", "UNKNOWN", msg)
            health_repo.create_poll_log(db, "xhs", "failed", detail=msg, started_at=started)
            db.commit()
            return
        creators = db.execute(
            select(Creator).where(Creator.platform == "xhs", Creator.enabled == 1)
        ).scalars().all()
        try:
            total_new = _poll_xhs_creators(db, creators)
        except Exception as e:  # noqa: BLE001
            logger.warning("xhs 轮询整体异常: %s", e)
            _update_cookie(db, "xhs", "FAIL", f"xhs 异常: {str(e)[:80]}")
            health_repo.create_poll_log(db, "xhs", "failed", detail=f"xhs 异常: {str(e)[:120]}", started_at=started)
            db.commit()
            return
        _update_cookie(db, "xhs", "OK", "xhs 拦截监测")
        health_repo.create_poll_log(db, "xhs", "ok", detail=None, started_at=started, new_works=total_new)
        db.commit()


def poll_xhs_creator(creator_id: int) -> None:
    """单博主小红书立即爬取（onetime --creator 用）。"""
    started = now_iso()
    with SessionLocal() as db:
        creator = db.get(Creator, creator_id)
        if creator is None or creator.platform != "xhs":
            health_repo.create_poll_log(db, "xhs", "fail", detail="creator_not_found", started_at=started)
            db.commit()
            return
        try:
            total_new = _poll_xhs_creators(db, [creator])
        except Exception as e:  # noqa: BLE001
            health_repo.create_poll_log(db, "xhs", "fail", detail=f"xhs 异常: {str(e)[:120]}", started_at=started)
            db.commit()
            return
        health_repo.create_poll_log(db, "xhs", "ok", detail=None, started_at=started, new_works=total_new)
        db.commit()


def write_personal_heartbeat() -> None:
    """个人任务进程（personal_worker）独立心跳：只运行已授权个人任务时在线。

    与监控层心跳分开：个人任务进程不注册 RSSHub/博主轮询，不应让页面误判
    "监控层在线"（那意味着其他平台轮询也在跑）。
    """
    settings.workbuddy_dir.mkdir(parents=True, exist_ok=True)
    (settings.workbuddy_dir / "personal_worker_heartbeat.json").write_text(
        json.dumps({"ts": time.time(), "pid": os.getpid()}), encoding="utf-8"
    )


def personal_source_download_locks(db: Session) -> int:
    """个人来源下载积压数（兼容入口；实现在 app.services.personal_source_service）。"""
    from app.services import personal_source_service

    return personal_source_service.count_download_locks(db)


def _work_has_personal_facts(db: Session, work_id: int) -> bool:
    """作品是否被个人来源事实覆盖（点赞/收藏标记或收藏夹归属）。"""
    from app.models import PersonalSourceFolderItem, PersonalSourceMark
    from sqlalchemy import exists, select as _select

    has_mark = db.execute(
        _select(exists().where(PersonalSourceMark.work_id == work_id))
    ).scalar()
    has_folder_item = db.execute(
        _select(exists().where(PersonalSourceFolderItem.work_id == work_id))
    ).scalar()
    return bool(has_mark or has_folder_item)


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但属其他用户：视为存活


def _claim_stale(claimed_at: str | None, claimed_pid: int | None) -> bool:
    """认领是否可安全自动恢复：**仅当认领进程已死亡**。

    PID 存活的认领一律视为活跃（无论认领时长）——长时下载不能因超时被重置；
    PID 复用造成的"假活"不会误恢复（安全方向），会以中断状态呈现并可手动重试。
    无认领戳（既有全局流水线路径）不归个人恢复管。
    """
    if not claimed_at and not claimed_pid:
        return False
    return not _pid_alive(claimed_pid)


_AUTO_DOWNLOAD_SQL = (
    "(SELECT value FROM app_settings WHERE key = 'douyin_personal_auto_download') = 'true'"
)


def _claim_work_for_processing(db: Session, work_id: int, *, personal: bool) -> bool:
    """跨入口统一的跨进程原子认领：NEW/QUEUED → DOWNLOADING + 认领戳。

    全局流水线、个人下载步骤、手动同步（--download）、重试后的处理共用本认领，
    单条 UPDATE 保证并发下唯一赢家。``personal=True`` 时授权开关在同一原子语句内
    校验——另一会话已提交关闭后，认领必然失败（关闭已提交即不会开始新作品）。
    """
    from sqlalchemy import text, update

    conditions = [Work.id == work_id, Work.status.in_(["NEW", "QUEUED"])]
    if personal:
        conditions.append(text(_AUTO_DOWNLOAD_SQL))
    result = db.execute(
        update(Work)
        .where(*conditions)
        .values(status="DOWNLOADING", download_claimed_at=now_iso(), download_claim_pid=os.getpid())
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount > 0


def _clear_claim(db: Session, work_id: int) -> None:
    """成功/失败后清除认领元数据（DONE/FAILED 不应残留活跃认领信息）。"""
    from sqlalchemy import update

    db.execute(
        update(Work)
        .where(Work.id == work_id)
        .values(download_claimed_at=None, download_claim_pid=None)
        .execution_options(synchronize_session=False)
    )


def _recover_stale_claim(db: Session, work_id: int, observed_at: str | None, observed_pid: int | None) -> bool:
    """CAS 恢复僵死认领：仅当 DB 中认领仍是观察到的旧值时重置为 QUEUED。

    两个恢复者同时看到旧认领时，第一个恢复并重新认领后，第二个的旧观察
    不再匹配新认领 → rowcount 0，不会重置活跃的新认领。
    """
    from sqlalchemy import update

    result = db.execute(
        update(Work)
        .where(
            Work.id == work_id,
            Work.status == "DOWNLOADING",
            Work.download_claimed_at == observed_at if observed_at is not None else Work.download_claimed_at.is_(None),
            Work.download_claim_pid == observed_pid if observed_pid is not None else Work.download_claim_pid.is_(None),
        )
        .values(status="QUEUED", download_claimed_at=None, download_claim_pid=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount > 0


def _authorization_granted() -> bool:
    """批次级预检（授权的权威校验在认领语句内联执行）。"""
    from app.services import personal_source_service

    with SessionLocal() as db:
        return personal_source_service.read_config(db).auto_download


# 允许原样展示给用户/落库的固定文案（人工维护的白名单字面量；动态插值不进白名单）
_SAFE_ERROR_LITERALS = frozenset({
    # download.py（PipelineOfflineError 常量文案）
    "Douyin downloader configuration is unavailable",
    "Douyin downloader is not configured",
    "MediaCrawler directory is unavailable",
    "MediaCrawler is not configured",
    "XHS download requires a configured browser cookie",
    "XHS downloader is not configured",
    "download produced no video file",
    "media work directory is unavailable",
    "yt-dlp is not installed",
    "yutto is not installed",
    "douyin image posts are not supported for automatic download yet "
    "(no verified downloader); the work stays registered but fails honestly",
    # transcribe.py 常量文案
    "转写引擎 funasr 未安装（首次部署需预下载 paraformer-zh 模型，K18）",
    "转写结果为空",
    "转写输入文件不存在（可能已被清理或下载不完整）",
    # twitter_image.py 常量文案
    "image album changed during validation",
    "image album contains an empty file",
    "image album contains an invalid image file",
    "image album contains an invalid source URL",
    "image album file could not be validated",
    "image manifest could not be written",
    "image work directory cannot be a link",
    "image work directory is unavailable",
    "image work has no expected source images",
    "registered image record is invalid",
    "this image adapter supports Twitter/X media only",
    # douyin_images.py 常量文案（含 detail 失败 kind 映射）
    "图集作品目录不可用",
    "图集存在未完成的恢复，已停止本次下载",
    "作品详情未包含可下载的图片地址（可能不是图集作品）",
    "作品详情身份与目标作品不一致，已拒绝下载",
    "图片下载失败：CDN 返回空内容",
    "图片下载失败：内容不是完整可解码的图片（可能是错误页、截断或损坏数据）",
    "未配置抖音登录态文件，请先完成登录导出（config/f2/douyin_cookie.txt 或 F2_DOUYIN_COOKIE_FILE）",
    "F2 隔离运行时未安装（tools/f2-runtime，见其 README）",
    "抖音登录态可能已过期，请重新导出登录态文件",
    "触发平台限流，请稍后重试",
    "作品详情获取超时，请稍后重试",
    "作品详情返回异常，请稍后重试",
    "作品详情获取失败，请稍后重试",
    # douyin_video.py 常量文案（含 detail 失败 kind 映射）
    "视频作品目录不可用",
    "作品详情未包含可下载的视频直链（可能不是视频作品）",
    "作品详情身份与目标作品不一致，已拒绝下载",
    "视频下载失败：CDN 返回空内容",
    "视频下载失败：内容不是有效 MP4（可能是错误页或损坏数据）",
    "视频下载失败：内容不是有效 MP4（缺少容器元数据）",
    "视频下载失败：内容不是有效 MP4（没有视频轨）",
    "视频下载失败：内容不是可播放的视频（AVFoundation 无法读取）",
    "视频下载失败：媒体验证不可用（缺少 macOS AVFoundation 工具）",
    "视频下载失败：媒体验证执行失败",
    "视频下载失败：所有候选直链均不可用",
    "视频下载失败：文件不可读",
})

# 受控动态文案：前缀白名单 + 余量全match给定模式（无自由文本、无凭据/路径/ID）
_SAFE_DYNAMIC_RULES: tuple[tuple[str, str], ...] = (
    ("图片下载失败：CDN 返回 HTTP ", r"\d+"),
    ("视频下载失败：CDN 返回 HTTP ", r"\d+"),
    ("图片下载失败（第 ", r"\d+ 张）：内容不是完整可解码的图片（可能是错误页、截断或损坏数据）"),
    ("图片下载失败（第 ", r"\d+ 张）：\w+Error"),
    ("视频下载失败：", r"\w+Error"),
    ("download checkpoint failed (", r"\w+Error\)"),
)


def _safe_dynamic_text(text: str) -> str | None:
    """受控动态文案：仅允许固定前缀 + 限定模式的余量，否则 None。"""
    import re

    for prefix, pattern in _SAFE_DYNAMIC_RULES:
        if text.startswith(prefix) and re.fullmatch(re.escape(prefix) + pattern, text):
            return text
    return None


def _safe_error_text(exc: Exception) -> str:
    """失败持久化（works.last_error / 任务 stage error / API / 页面）用的安全文案。

    白名单机制：仅人工维护的字面量与受控动态形态（固定前缀 + 纯数字）允许
    原样展示；其余一律只记录异常类别名——凭据、路径、未知敏感值不可能进入
    DB/API/页面（不依赖正则预测内容）。
    """
    text = str(exc)
    if text in _SAFE_ERROR_LITERALS:
        return text
    dynamic = _safe_dynamic_text(text)
    if dynamic is not None:
        return dynamic
    if isinstance(exc, PipelineOfflineError):
        return "下载通道不可用（下载器配置缺失或平台临时拒绝，详见运行日志的 error_type）"
    try:
        from monitor.pipeline.douyin_images import DouyinImageError

        if isinstance(exc, DouyinImageError):
            return "图集下载失败（登录态、运行时或平台返回异常，详见运行日志的 error_type）"
    except ImportError:  # pragma: no cover
        pass
    try:
        from monitor.pipeline.douyin_video import DouyinVideoError

        if isinstance(exc, DouyinVideoError):
            return "视频直取失败（登录态、运行时或平台返回异常，详见运行日志的 error_type）"
    except ImportError:  # pragma: no cover
        pass
    try:
        from monitor.pipeline.twitter_image import TwitterImageError

        if isinstance(exc, TwitterImageError):
            return "图片下载失败（归档校验未通过，详见运行日志的 error_type）"
    except ImportError:  # pragma: no cover
        pass
    return type(exc).__name__


def download_personal_source_works(*, limit: int = 10) -> int:
    """个人来源自动下载（独立授权开关控制，默认关）。

    授权校验在**认领语句内联原子执行**：另一进程已提交关闭后，认领必然失败，
    不会开始新作品；已在进行中的下载按其原有流程走完，不中途丢弃。

    只处理**个人来源事实覆盖**的作品（EXISTS 判定，按作品去重）：
    - source_type=personal_source（F2/DDR 登记，含夹内-only 作品）；
    - 被 personal_source_marks 关联（点赞/收藏事实）；
    - 被 personal_source_folder_items 关联（仅收藏夹归属的跨来源作品）。
    状态含 NEW / QUEUED / （僵死认领的）DOWNLOADING：
    - 认领通过统一原子语句完成，跨进程并发只有一个赢家；
    - DOWNLOADING 且认领进程已死 → 按观察到的旧认领 CAS 恢复为 QUEUED 再认领；
      PID 存活的认领（无论时长）一律视为活跃，不自动恢复；
      无认领戳的 DOWNLOADING（既有全局流水线路径）不恢复、如实计入积压并可通过
      手动重试入口操作。
    下载任务来源决定兜底策略：本步骤**一律禁用**浏览器播放兜底，失败如实标记。
    绝不触碰其他来源的队列。
    """
    from app.models import PersonalSourceFolderItem, PersonalSourceMark
    from app.services import personal_source_service
    from sqlalchemy import exists, or_

    with SessionLocal() as db:
        config = personal_source_service.read_config(db)
        if not config.auto_download:
            return 0
        interrupted = db.execute(
            select(Work.id, Work.download_claimed_at, Work.download_claim_pid)
            .where(Work.status == "DOWNLOADING")
            .where(
                or_(
                    Work.source_type == "personal_source",
                    exists().where(PersonalSourceMark.work_id == Work.id),
                    exists().where(PersonalSourceFolderItem.work_id == Work.id),
                )
            )
        ).all()
        for work_id, claimed_at, claimed_pid in interrupted:
            if _claim_stale(claimed_at, claimed_pid):
                if _recover_stale_claim(db, work_id, claimed_at, claimed_pid):
                    logger.info("个人来源下载：恢复僵死认领 work_id=%s", work_id)
        selected = db.execute(
            select(Work.id)
            .where(Work.status.in_(["NEW", "QUEUED"]))
            .where(
                or_(
                    Work.source_type == "personal_source",
                    exists().where(PersonalSourceMark.work_id == Work.id),
                    exists().where(PersonalSourceFolderItem.work_id == Work.id),
                )
            )
            .order_by(Work.id)
            .limit(limit)
        ).scalars().all()

    processed = 0
    for work_id in selected:
        # 批次级预检（权威校验在认领语句内联）：授权关闭则不再开始剩余作品
        if not _authorization_granted():
            logger.info("个人来源自动下载：授权已关闭，本批剩余作品不再开始")
            break
        with SessionLocal() as db:
            work = db.get(Work, work_id)
            if work is None:
                continue
            if not _claim_work_for_processing(db, work_id, personal=True):
                continue  # 授权已关闭 / 已被其他进程认领或处理
            try:
                _process_work(db, work, write_pending=True, allow_browser_fallback=False)
                _clear_claim(db, work_id)
                db.commit()
                processed += 1
            except PipelineOfflineError as exc:
                failed_stage = "transcribe" if work.status == "TRANSCRIBING" else "download"
                work.status = "FAILED"
                work.last_error = _safe_error_text(exc)
                task_repo.mark_failed(db, work.id, failed_stage, _safe_error_text(exc))
                _clear_claim(db, work_id)
                db.commit()
                logger.warning("personal-source work download offline: work_id=%s error_type=%s",
                               work.id, type(exc).__name__)
            except Exception as exc:  # noqa: BLE001
                failed_stage = {
                    "DOWNLOADING": "download",
                    "TRANSCRIBING": "transcribe",
                    "FRAME_EXTRACTING": "frames",
                }.get(work.status, "pipeline")
                work.status = "FAILED"
                work.last_error = _safe_error_text(exc)
                task_repo.mark_failed(db, work.id, failed_stage, _safe_error_text(exc))
                _clear_claim(db, work_id)
                db.commit()
                logger.warning("personal-source work pipeline failed: work_id=%s error_type=%s",
                               work.id, type(exc).__name__)
    return processed


def check_douyin_personal_sync() -> None:
    """个人来源定时任务 tick：同步到期间隔执行；下载积压每个 tick 都消费。

    配置由前端 /api/v1/personal-source/schedule 维护，默认全关、不会隐式启动。
    同步与下载是两项独立授权（enabled / auto_download），**异常也相互隔离**：
    同步抛错只记录并继续进入下载消费，不会跳过已授权的积压处理。
    日志只记数量与类别（error_type），不写原始异常文本/作品/收藏夹/账号标识。
    """
    from app.services import personal_source_service
    from app.utils.timeutil import parse_iso
    from monitor import personal_source_ingest

    with SessionLocal() as db:
        config = personal_source_service.read_config(db)
    if not (config.enabled or config.auto_download):
        return

    if config.enabled:
        due = True
        try:
            with SessionLocal() as db:
                last_started = personal_source_service.last_sync_started_at(db)
            if last_started:
                last = parse_iso(last_started)
                if last is not None:
                    elapsed_minutes = (datetime.now(timezone.utc) - last).total_seconds() / 60
                    due = elapsed_minutes >= config.interval_minutes
                else:
                    logger.warning("个人来源同步：无法解析上次时间，按已到期处理")
            if due:
                logger.info("个人来源定时同步到期，开始执行")
                report = personal_source_ingest.run_ingest("f2")
                logger.info(
                    "个人来源同步完成: 新作品 %s，marks 新增 %s，归属新增 %s，不完整项 %s",
                    report.works_created, report.marks_created, report.folder_items_created,
                    len(report.incomplete_lists),
                )
        except Exception as exc:  # noqa: BLE001 - 同步失败只记录安全类别，不阻塞下载
            logger.warning("个人来源定时同步失败: error_type=%s", type(exc).__name__)

    if config.auto_download:
        try:
            processed = download_personal_source_works()
            if processed:
                logger.info("个人来源自动下载处理 %s 个作品", processed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("个人来源自动下载失败: error_type=%s", type(exc).__name__)


def consume_triggers() -> None:
    """消费聚合 API /monitor/poll 写下的 trigger 文件，处理完即删除。"""
    trigger_dir: Path = settings.trigger_dir
    if not trigger_dir.exists():
        return
    for path in sorted(trigger_dir.glob("poll-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            continue
        platform = payload.get("platform")
        if platform:
            if platform in RSSHUB_PLATFORMS:
                poll_rsshub_platform(platform)
            elif platform == "douyin":
                poll_douyin()
            elif platform == "xhs":
                poll_xhs()
            else:
                logger.warning("未知平台 trigger: %s", platform)
        else:
            for p in RSSHUB_PLATFORMS:
                poll_rsshub_platform(p)
            poll_douyin()
            poll_xhs()
        path.unlink(missing_ok=True)


def _existing_archive_entry(db: Session, work: Work):
    entries = [entry for entry in archive_repo.list_by_work(db, work.id) if entry.root_key == "media"]
    if work.media_path:
        relative = relative_path_from_root("media", str(work.media_path))
        entry = archive_repo.get_entry_by_path(db, "media", relative)
        if entry is not None and entry.platform == work.platform and str(entry.external_id or "") == str(work.external_id):
            return entry
        return None
    entries = [
        entry for entry in entries
        if entry.platform == work.platform and str(entry.external_id or "") == str(work.external_id)
    ]
    if not entries:
        return None
    return next((entry for entry in entries if entry.check_status == "verified"), entries[0])


def _resolve_work_metadata(db: Session, work: Work):
    """Resolve stored and available publisher evidence before choosing a new path."""
    existing_entry = _existing_archive_entry(db, work)
    seed = archive_metadata_seed(work, existing_entry)
    stored_source = seed.get("published_time_source")
    stored_precision = seed.get("published_time_precision")
    already_detailed = stored_source == "bilibili_view_api" and stored_precision == "second"
    resolution = resolve_archive_metadata(
        work,
        existing_metadata=seed,
        query_bilibili_detail=(work.platform == "bilibili" and not already_detailed),
    )
    if resolution.title.verified and "title" not in resolution.conflicts and resolution.title.value:
        work.title = str(resolution.title.value)
    publication = resolution.published_at
    if publication.verified and "published_at" not in resolution.conflicts and publication.value:
        work.published_at = str(publication.value)
        work.published_time_source = publication.source
        work.published_time_precision = publication.precision
        if publication.source_timezone:
            work.published_time_timezone = publication.source_timezone
        elif publication.precision == "second":
            work.published_time_timezone = "UTC"
    if resolution.metadata_checked_at:
        work.metadata_checked_at = resolution.metadata_checked_at
    return resolution, existing_entry


def _work_media_dir(work, publication_provenance=None, existing_entry=None) -> Path:
    """Resolve the stored physical directory or build a stable new archive name."""
    _relative, path = resolve_work_directory(
        work,
        root_key="media",
        publication_provenance=publication_provenance,
        existing_entry=existing_entry,
    )
    return path


def _registered_files_for_adapter(entry, relative_directory: str, *, images_only: bool = False) -> list[dict]:
    registered = []
    for item in entry.files_json or []:
        if not isinstance(item, dict):
            continue
        relative = item.get("relative_path")
        if not isinstance(relative, str):
            continue
        relative = relative.replace("\\", "/")
        prefix = relative_directory.rstrip("/") + "/"
        if not os.path.normcase(relative).startswith(os.path.normcase(prefix)):
            raise ValueError("registered media leaves its work directory")
        work_relative = relative[len(prefix):]
        if images_only and not os.path.normcase(work_relative).startswith(os.path.normcase("images/")):
            raise ValueError("image work conflicts with a non-image media registration")
        absolute = resolve_archive_path("media", relative)
        registered.append({
            "path": str(absolute),
            "size_bytes": item.get("size_bytes"),
            "media_type": item.get("media_type"),
            "sha256": item.get("sha256"),
        })
    return registered


def _sync_checkpoint_after_commit(entry_id: int, work_id: int) -> None:
    try:
        sync_archive_entry(entry_id)
    except Exception as exc:  # noqa: BLE001 - the durable pending job retries it
        logger.warning("archive sidecar sync deferred work_id=%s error_type=%s", work_id, type(exc).__name__)


def _pipeline_error_message(exc: Exception) -> str:
    if isinstance(exc, PipelineOfflineError):
        return str(exc)
    try:
        from monitor.pipeline.douyin_images import DouyinImageError

        if isinstance(exc, DouyinImageError):
            return str(exc)  # 白名单/脱敏后的图集失败文案
    except ImportError:
        pass
    try:
        from monitor.pipeline.twitter_image import TwitterImageError

        if isinstance(exc, TwitterImageError):
            return str(exc)
    except ImportError:
        pass
    return f"download checkpoint failed ({type(exc).__name__})"


@dataclass(frozen=True)
class _DownloadCheckpoint:
    media_dir: Path
    result: object
    manifest_path: Path | None = None


def download_and_checkpoint(
    db: Session,
    work: Work,
    *,
    allow_browser_fallback: bool | None = None,
) -> _DownloadCheckpoint:
    """Download/reuse, persist Work + ArchiveEntry + sync job, then sync sidecars.

    The checkpoint commit occurs before the independent sync call. Callers may
    safely begin transcription only after this function returns.

    ``allow_browser_fallback=None`` 按**作品事实**推断：个人来源作品或被
    个人来源事实（点赞/收藏标记、收藏夹归属）覆盖的作品一律禁用——跨来源
    去重后 source_type 仍是 monitor 的作品同样禁用；显式传入时以调用方的
    **下载任务来源**为准（个人来源同步步骤恒为 False）。
    """
    if allow_browser_fallback is None:
        allow_browser_fallback = work.source_type != "personal_source" and not _work_has_personal_facts(
            db, work.id
        )
    resolution, existing_entry = _resolve_work_metadata(db, work)
    publication = resolution.publication_provenance() or publication_provenance_from_work(work)
    source_context = resolution.as_metadata()
    # Keep newly checked publication evidence even when the later downloader
    # fails; a cached Work value is never passed back as a fresh collector.
    db.commit()

    relative_directory, media_dir = resolve_work_directory(
        work,
        root_key="media",
        publication_provenance=publication,
        existing_entry=existing_entry,
    )
    registered_entry = archive_repo.get_entry_by_path(db, "media", relative_directory)
    assert_work_archive_identity(db, registered_entry, work)

    try:
        legacy = register_legacy_media(
            db,
            work,
            root_key="media",
            publication_provenance=publication,
            source_context=source_context,
        )
        if legacy is not None:
            db.commit()
            _sync_checkpoint_after_commit(legacy.archive_entry_id, work.id)
            registered_entry = db.get(ArchiveEntry, legacy.archive_entry_id)
            registered_files = list(legacy.files)
        else:
            registered_entry = archive_repo.get_entry_by_path(db, "media", relative_directory)
            assert_work_archive_identity(db, registered_entry, work)
            registered_files = (
                _registered_files_for_adapter(
                    registered_entry,
                    relative_directory,
                    images_only=(str(work.content_type or "").lower() in {"image", "text"}),
                )
                if registered_entry is not None else []
            )

        work.status = "DOWNLOADING"
        prior_task = task_repo.get_by_work_stage(db, work.id, "download")
        attempts = (prior_task.attempts if prior_task is not None else 0) + 1
        task = task_repo.upsert(db, work.id, "download", "running", attempts=attempts)
        task.error = None
        db.commit()

        manifest_path = None
        content_type = str(work.content_type or "video").lower()
        if content_type == "video":
            try:
                result = download.download_with_result(
                    work.platform,
                    work.url,
                    media_dir,
                    registered_files=registered_files,
                    allow_browser_fallback=allow_browser_fallback,
                )
                download_source = f"monitor.pipeline.download:{work.platform}"
            except PipelineOfflineError:
                # 个人来源禁用网页播放兜底：jiji 主路径失败后改用 F2 详情直链直取
                # （仍是 API/CDN，无网页无播放）。其他来源保持既有行为不变。
                if allow_browser_fallback or work.platform != "douyin":
                    raise
                from monitor.pipeline import douyin_video

                result = douyin_video.download_f2_video_with_result(
                    work,
                    media_dir,
                    registered_files=registered_files,
                )
                download_source = "monitor.pipeline.douyin_video"
        elif work.platform == "douyin":
            # 抖音图集：F2 detail 只读接口取图片直链 + CDN 直取，全程不打开/播放
            # 作品网页；失败走 DouyinImageError 的白名单/脱敏文案，如实标记。
            from monitor.pipeline import douyin_images

            images_dir = media_dir / "images"
            manifest_path, result = douyin_images.download_douyin_images_with_result(
                work,
                images_dir,
                registered_files=registered_files,
            )
            download_source = "monitor.pipeline.douyin_images"
            if manifest_path is not None:
                work.frame_manifest_path = relative_path_from_root("media", str(manifest_path))
        else:
            from monitor.pipeline import twitter_image

            images_dir = media_dir / "images"
            manifest_path, result = twitter_image.download_images_with_result(
                work,
                images_dir,
                registered_files=registered_files,
            )
            download_source = "monitor.pipeline.twitter_image"
            work.frame_manifest_path = relative_path_from_root("media", str(manifest_path))

        entry_id = record_download_checkpoint(
            db,
            work,
            result,
            root_key="media",
            download_source=download_source,
            publication_provenance=publication,
            source_context=source_context,
        )
        task_repo.upsert(db, work.id, "download", "succeeded", attempts=attempts)
        work.last_error = None
        db.commit()
        _sync_checkpoint_after_commit(entry_id, work.id)
        return _DownloadCheckpoint(media_dir=media_dir, result=result, manifest_path=manifest_path)
    except Exception as exc:  # noqa: BLE001 - preserve safe failure state and leave no receipt
        safe_message = _pipeline_error_message(exc)
        work.status = "FAILED"
        work.last_error = safe_message
        task_repo.mark_failed(db, work.id, "download", safe_message)
        db.commit()
        if isinstance(exc, PipelineOfflineError):
            raise
        raise PipelineOfflineError(safe_message) from None


def process_link_analysis(work_id: int) -> dict[str, str | int]:
    """只处理用户刚提交的一条视频作品，不依赖也不触发全局队列。"""
    with SessionLocal() as db:
        work = db.get(Work, work_id)
        if work is None or work.status not in {"NEW", "QUEUED", "FAILED"}:
            raise ValueError(f"work {work_id} is not runnable")
        if (work.content_type or "video") != "video":
            work.status = "FAILED"
            work.last_error = "链接分析仅支持视频作品"
            task_repo.mark_failed(db, work.id, "download", work.last_error)
            db.commit()
            return {"work_id": work.id, "status": work.status}
        try:
            _process_work(db, work, write_pending=True)
            db.commit()
        except PipelineOfflineError as exc:
            failed_stage = "transcribe" if work.status == "TRANSCRIBING" else "download"
            # 与全局流水线同一安全文案：异常原文/堆栈可能含本机路径或敏感值
            safe_message = _safe_error_text(exc)
            work.status = "FAILED"
            work.last_error = safe_message
            task_repo.mark_failed(db, work.id, failed_stage, safe_message)
            db.commit()
            logger.warning("link analysis %s pipeline offline: error_type=%s", work.id, type(exc).__name__)
        except Exception as exc:  # noqa: BLE001
            failed_stage = "transcribe" if work.status == "TRANSCRIBING" else "pipeline"
            safe_message = _safe_error_text(exc)
            work.status = "FAILED"
            work.last_error = safe_message
            task_repo.mark_failed(db, work.id, failed_stage, safe_message)
            db.commit()
            logger.warning("link analysis %s pipeline failed: error_type=%s", work.id, type(exc).__name__)
        return {"work_id": work.id, "status": work.status}


def run_pipeline() -> None:
    """处理 NEW/QUEUED 作品：下载→转写→抽帧→素材就绪清单；失败落 FAILED 可重试（AC-8）。

    source_type=personal_source 且状态 NEW 的作品**不在这里处理**：个人来源作品
    的自动下载由独立的授权开关（douyin_personal_auto_download）与专用步骤负责，
    避免个人来源同步隐式启动/扩大全局下载队列。显式重试（QUEUED）仍走本流水线。
    浏览器播放兜底按**作品事实**统一裁决：被个人来源事实覆盖的作品（含先由
    博主监控登记、后被 F2 发现的跨来源作品）一律禁用，不因来源字段绕开策略。
    """
    if not settings.monitor_run_pipeline:
        return
    with SessionLocal() as db:
        works = db.execute(
            select(Work)
            .where(Work.status.in_(["NEW", "QUEUED"]))
            .where(~((Work.source_type == "personal_source") & (Work.status == "NEW")))
            .order_by(Work.id)
            .limit(10)
        ).scalars().all()
        for selected in works:
            # 统一原子认领（与个人下载/手动同步/重试同一条语句）：
            # identity map 中的旧状态不可信，认领以数据库为准，失败者跳过
            if not _claim_work_for_processing(db, selected.id, personal=False):
                continue
            work = db.get(Work, selected.id)
            work.status = "DOWNLOADING"  # 与认领后的数据库状态对齐（identity map 可能是旧值）
            allow_browser_fallback = (
                work.source_type != "personal_source"
                and not _work_has_personal_facts(db, work.id)
            )
            try:
                _process_work(db, work, write_pending=True, allow_browser_fallback=allow_browser_fallback)
                _clear_claim(db, work.id)
                db.commit()
            except PipelineOfflineError as exc:
                failed_stage = "transcribe" if work.status == "TRANSCRIBING" else "download"
                work.status = "FAILED"
                work.last_error = _safe_error_text(exc)
                task_repo.mark_failed(db, work.id, failed_stage, _safe_error_text(exc))
                _clear_claim(db, work.id)
                db.commit()
                logger.warning("work %s 流水线离线: error_type=%s", work.id, type(exc).__name__)
            except Exception as exc:  # noqa: BLE001
                failed_stage = {
                    "DOWNLOADING": "download",
                    "TRANSCRIBING": "transcribe",
                    "FRAME_EXTRACTING": "frames",
                }.get(work.status, "pipeline")
                work.status = "FAILED"
                work.last_error = _safe_error_text(exc)
                task_repo.mark_failed(db, work.id, failed_stage, _safe_error_text(exc))
                _clear_claim(db, work.id)
                db.commit()
                logger.warning("work %s 流水线异常: error_type=%s", work.id, type(exc).__name__)


def _process_work(
    db: Session,
    work,
    *,
    write_pending: bool = True,
    allow_browser_fallback: bool | None = None,
) -> None:
    """单作品流水线。v0.4：按 content_type 分流——
    video 走原五步（下载→转写→抽帧→就绪）；image/text（推特图文）走
    下载图片→写 manifest→跳过转写/抽帧→直接进 AI 笔记。

    ``allow_browser_fallback`` 透传给下载检查点；None 按作品 source_type 推断。
    """
    content_type = work.content_type or "video"
    checkpoint = download_and_checkpoint(db, work, allow_browser_fallback=allow_browser_fallback)
    if content_type != "video":
        work.status = "NOTE_GENERATING"
        if write_pending:
            notify.notify_ready(db, work)
        else:
            note_task = task_repo.upsert(db, work.id, "note", "pending", attempts=0)
            note_task.error = None
        return

    video_file = next((item for item in checkpoint.result.files if item.media_type == "video"), checkpoint.result.files[0])
    video_path = str(video_file.path)
    media_dir = checkpoint.media_dir

    work.status = "TRANSCRIBING"
    transcribe_task = task_repo.upsert(db, work.id, "transcribe", "running", attempts=1)
    transcribe_task.error = None
    transcript_dir = media_dir / "transcript"
    transcript_path = transcribe.transcribe(video_path, transcript_dir)
    work.transcript_path = relative_path_from_root("media", str(transcript_path))
    task_repo.upsert(db, work.id, "transcribe", "succeeded", attempts=1)

    work.status = "FRAME_EXTRACTING"
    frame_task = task_repo.upsert(db, work.id, "frames", "running", attempts=1)
    frame_task.error = None
    frames_dir = media_dir / "frames"
    entries = [
        {"term": e.term, "action": e.action, "weight": e.weight, "enabled": bool(e.enabled)}
        for e in lexicon_repo.list_entries(db)
        if e.enabled
    ]
    segments = _load_segments(work.transcript_path)
    windows = frames.match_windows(segments, entries) if segments else []
    extracted = frames.extract_frames(video_path, windows, frames_dir)
    manifest_path = frames.write_manifest(work.id, extracted, frames_dir)
    work.frame_manifest_path = relative_path_from_root("media", str(manifest_path))
    task_repo.upsert(db, work.id, "frames", "succeeded", attempts=1)

    work.status = "NOTE_GENERATING"
    if write_pending:
        notify.notify_ready(db, work)
    else:
        note_task = task_repo.upsert(db, work.id, "note", "pending", attempts=0)
        note_task.error = None


def _load_segments(transcript_rel: str | None) -> list[dict]:
    """读取带时间戳转写稿 [{start,end,text}]；损坏/空返回 []（K15 空文本降级）。"""
    if not transcript_rel:
        return []
    try:
        path = resolve_archive_path("media", relative_path_from_root("media", transcript_rel), must_exist=True)
    except (OSError, ValueError):
        return []
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(payload, list):
        return payload
    segments = payload.get("segments")
    return segments if isinstance(segments, list) else []
