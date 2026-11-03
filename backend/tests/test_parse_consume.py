"""parse 阶段任务的消费：作品经下载检查点（元数据解析完成）时 parse pending → succeeded。

背景：轮询登记作品只创建 parse=pending（jobs._upsert_creator_entries），历史上
只有 single_link 执行器会把它标成功，导致 parse 长期挂起。修复后
download_and_checkpoint 在元数据解析提交后消费该任务；可用
MONITOR_MARK_PARSE_SUCCEEDED=false（jobs._MARK_PARSE_SUCCEEDED）关闭。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.repositories import task_repo
from app.utils.timeutil import now_iso
from app.models import Work
from monitor import jobs


def _make_work(db, *, external_id: str) -> Work:
    work = Work(
        platform="douyin",
        external_id=external_id,
        title="parse 消费测试",
        url=f"https://www.douyin.com/video/{external_id}",
        source_type="monitor",
        status="QUEUED",
        content_type="video",
        published_at="2026-01-01T00:00:00Z",
        published_time_source="douyin_web_create_time",
        published_time_precision="second",
        published_time_timezone="UTC",
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    db.add(work)
    db.commit()
    return work


def _stub_downloader(monkeypatch) -> None:
    def _fake(platform, url, media_dir):
        target = Path(media_dir) / "fake_original.mp4"
        target.write_bytes(b"\x00" * 2048)
        return None

    monkeypatch.setattr(jobs.download, "_invoke_downloader", _fake)


def _parse_task(db, work_id: int):
    return task_repo.get_by_work_stage(db, work_id, "parse")


def test_parse_pending_consumed_on_download_checkpoint(db, monkeypatch):
    """登记留下的 parse=pending：下载检查点完成后翻为 succeeded（消费而非新增）。"""
    work = _make_work(db, external_id="parse-consume-1")
    task_repo.upsert(db, work.id, "parse", "pending", attempts=0)
    db.commit()
    _stub_downloader(monkeypatch)

    jobs.download_and_checkpoint(db, work)

    task = _parse_task(db, work.id)
    assert task.status == "succeeded"
    assert task.attempts == 1


def test_parse_not_created_when_never_registered(db, monkeypatch):
    """从未登记 parse 的作品（如目录导入）：处理后仍无 parse 行，不凭空造任务。"""
    work = _make_work(db, external_id="parse-consume-2")
    _stub_downloader(monkeypatch)

    jobs.download_and_checkpoint(db, work)

    assert _parse_task(db, work.id) is None


def test_parse_already_succeeded_untouched(db, monkeypatch):
    """已被 single_link 标成功的 parse 行：不重复迁移，attempts 不再累加。"""
    work = _make_work(db, external_id="parse-consume-3")
    task_repo.upsert(db, work.id, "parse", "succeeded", attempts=1)
    db.commit()
    _stub_downloader(monkeypatch)

    jobs.download_and_checkpoint(db, work)

    task = _parse_task(db, work.id)
    assert task.status == "succeeded"
    assert task.attempts == 1


def test_parse_consumed_even_when_download_fails(db, monkeypatch):
    """下载失败不回滚 parse：元数据解析已完成，失败的是下载阶段（与 single_link 同语义）。"""
    work = _make_work(db, external_id="parse-consume-4")
    task_repo.upsert(db, work.id, "parse", "pending", attempts=0)
    db.commit()

    def _boom(platform, url, media_dir):
        raise RuntimeError("simulated downloader crash")

    monkeypatch.setattr(jobs.download, "_invoke_downloader", _boom)

    with pytest.raises(jobs.PipelineOfflineError):
        jobs.download_and_checkpoint(db, work)

    task = _parse_task(db, work.id)
    assert task.status == "succeeded"
    db.refresh(work)
    assert work.status == "FAILED"


def test_parse_consumption_can_be_disabled(db, monkeypatch):
    """开关关闭：回到只登记不消费的旧行为，parse 保持 pending。"""
    monkeypatch.setattr(jobs, "_MARK_PARSE_SUCCEEDED", False)
    work = _make_work(db, external_id="parse-consume-5")
    task_repo.upsert(db, work.id, "parse", "pending", attempts=0)
    db.commit()
    _stub_downloader(monkeypatch)

    jobs.download_and_checkpoint(db, work)

    assert _parse_task(db, work.id).status == "pending"
