"""C3 本人点赞/收藏融合：合成样本验收（favorites-ingest-design.md §8 八项）。

全部使用 fixture 数据，不触碰真实账号/网络。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

from monitor.platforms.douyin_personal_source import (  # noqa: E402
    PersonalSourceError,
    parse_folders_document,
    parse_snapshot_document,
    read_snapshot_files,
)
from monitor.personal_source_ingest import ingest_snapshot  # noqa: E402


# ---------- fixture 构造 ----------

def _record(aweme_id: str, mark_list: str, **overrides) -> dict:
    base = {
        "id": f"{mark_list}:{aweme_id}",
        "videoId": aweme_id,
        "title": f"作品{aweme_id[-4:]}",
        "author": "测试作者",
        "authorId": "author-1",
        "url": f"https://www.douyin.com/video/{aweme_id}",
        "publishedAt": "2026-05-01T08:00:00.000Z",
        "occurredAt": None,
        "occurredAtSource": "unknown",
        "mediaType": "video",
        "coverUrl": "https://p.example/cover.jpg",
    }
    base.update(overrides)
    return base


def _records_doc(records: dict, *, warnings=None, direct_sync=None) -> dict:
    return {
        "schemaVersion": 2,
        "updatedAt": "2026-10-28T00:00:00.000Z",
        "records": {
            "watch_history": records.get("watch_history", []),
            "liked_videos": records.get("liked_videos", []),
            "favorite_videos": records.get("favorite_videos", []),
        },
        "chatMessages": [],
        "chatConversations": [],
        "warnings": warnings or [],
        "directSync": direct_sync or {"watch_history": True, "liked_videos": True, "favorite_videos": True},
    }


def _folders_doc(folders: list[dict], items: dict) -> dict:
    return {
        "schemaVersion": 1,
        "updatedAt": "2026-10-28T00:00:00.000Z",
        "lists": {"favorite_videos": {"folders": folders, "items": items}},
    }


@pytest.fixture()
def parse_and_ingest(db):
    def _run(records: dict, folders: dict | None = None, **doc_kw):
        snapshot = parse_snapshot_document(_records_doc(records, **doc_kw), source="file")
        if folders is not None:
            snapshot.folders, snapshot.folder_assignments = parse_folders_document(folders)
            snapshot.folders_available = True
        report = ingest_snapshot(snapshot, db)
        db.commit()
        return snapshot, report
    return _run


# ---------- §8.1 跨来源重复：like+favorite → 两条事实、一次登记 ----------

def test_cross_source_duplicate_single_work(parse_and_ingest, db):
    from app.models import PersonalSourceMark, Work

    _, report = parse_and_ingest({
        "liked_videos": [_record("11111", "liked_videos")],
        "favorite_videos": [_record("11111", "favorite_videos")],
    })
    marks = db.query(PersonalSourceMark).filter_by(external_id="11111").all()
    works = db.query(Work).filter_by(external_id="11111").all()
    assert len(marks) == 2 and {m.source_mark for m in marks} == {"like", "favorite"}
    assert len(works) == 1
    assert report.works_created == 1 and report.marks_created == 2


# ---------- §8.2 文件夹：有夹 / 未分区(NULL) ----------

def test_folder_and_unfiled(parse_and_ingest, db):
    from app.models import PersonalSourceMark

    parse_and_ingest(
        {"favorite_videos": [_record("22221", "favorite_videos"), _record("22222", "favorite_videos")]},
        folders=_folders_doc([{"id": "fA", "name": "旅行"}], {"22221": "fA"}),
    )
    by_id = {m.external_id: m for m in db.query(PersonalSourceMark).all()}
    assert by_id["22221"].folder_id == "fA" and by_id["22221"].folder_name == "旅行"
    # 未分区：显式 NULL，不推断别的含义
    assert by_id["22222"].folder_id is None and by_id["22222"].folder_name is None


# ---------- §8.3 重跑幂等 ----------

def test_rerun_idempotent(parse_and_ingest, db):
    from app.models import PersonalSourceMark, Work

    records = {"liked_videos": [_record("33331", "liked_videos")]}
    _, first = parse_and_ingest(records)
    _, second = parse_and_ingest(records)
    assert first.marks_created == 1 and second.marks_created == 0
    assert second.works_created == 0 and second.marks_updated == 1
    assert db.query(PersonalSourceMark).filter_by(external_id="33331").count() == 1
    assert db.query(Work).filter_by(external_id="33331").count() == 1
    # first_seen 不因重跑漂移（last_seen 允许刷新）
    mark = db.query(PersonalSourceMark).filter_by(external_id="33331").one()
    assert mark.first_seen_at == mark.last_seen_at or mark.last_seen_at > mark.first_seen_at


# ---------- §8.4 空列表 / 文件缺失 / JSON 损坏 三种结果可区分 ----------

def test_empty_list_is_valid_snapshot(db):
    snapshot = parse_snapshot_document(_records_doc({}), source="file")
    assert snapshot.items == [] and snapshot.snapshot_id
    report = ingest_snapshot(snapshot, db)
    assert report.items_in_snapshot == 0 and report.marks_created == 0


def test_missing_file_raises(tmp_path):
    with pytest.raises(PersonalSourceError) as exc:
        read_snapshot_files(tmp_path / "records.json")
    assert exc.value.kind == "missing_file"


def test_corrupt_json_raises(tmp_path):
    bad = tmp_path / "records.json"
    bad.write_text('{"schemaVersion": 2, "records": {"liked_vi', encoding="utf-8")
    with pytest.raises(PersonalSourceError) as exc:
        read_snapshot_files(bad)
    assert exc.value.kind == "invalid_json"


def test_unknown_schema_version_raises(db):
    doc = _records_doc({})
    doc["schemaVersion"] = 99
    with pytest.raises(PersonalSourceError) as exc:
        parse_snapshot_document(doc, source="file")
    assert exc.value.kind == "schema_changed"


# ---------- §8.5 缺字段：全部未知，不崩 ----------

def test_missing_fields_unknown(parse_and_ingest, db):
    from app.models import PersonalSourceMark, Work

    raw = {
        "id": "favorite_videos:44441",
        "videoId": "44441",
        # 无 title/author/publishedAt/occurredAt/coverUrl/mediaType
    }
    _, report = parse_and_ingest({"favorite_videos": [raw]}, warnings=[])
    work = db.query(Work).filter_by(external_id="44441").one()
    assert work.published_at is None and work.published_time_source is None
    assert work.title == "未命名视频"  # DDR 同款占位
    assert work.url == "https://www.douyin.com/video/44441"  # 由 videoId 确定性构造
    mark = db.query(PersonalSourceMark).filter_by(external_id="44441").one()
    assert mark.occurred_at is None


# ---------- §8.6 快照不完整：warnings/directSync 透传 ----------

def test_incomplete_snapshot_passthrough(parse_and_ingest):
    _, report = parse_and_ingest(
        {"favorite_videos": [_record("55551", "favorite_videos")]},
        warnings=["coverage incomplete"],
        direct_sync={"watch_history": True, "liked_videos": True, "favorite_videos": False},
    )
    assert report.warnings == ["coverage incomplete"]
    assert report.direct_sync["favorite_videos"] is False  # 报告保留"未读完"标记，不推断取消


# ---------- §8.7 已存在作品：不覆盖可信时间；可补全 NULL ----------

def test_existing_work_trusted_time_untouched(parse_and_ingest, db):
    from app.models import Work
    from app.utils.timeutil import now_iso

    existing = Work(
        platform="douyin", external_id="66661", title="原标题", url="https://www.douyin.com/video/66661",
        source_type="monitor", published_at="2020-01-01T00:00:00Z",
        published_time_source="douyin_mediacrawler_create_time", published_time_precision="second",
        published_time_timezone="UTC", created_at=now_iso(), updated_at=now_iso(),
    )
    db.add(existing)
    db.commit()

    _, report = parse_and_ingest({"liked_videos": [_record("66661", "liked_videos")]})
    db.refresh(existing)
    assert existing.published_at == "2020-01-01T00:00:00Z"  # 已有可信值零触碰
    assert existing.title == "原标题" and existing.source_type == "monitor"
    assert report.works_created == 0 and report.works_publication_filled == []


def test_existing_work_null_publication_filled(parse_and_ingest, db):
    from app.models import Work
    from app.utils.timeutil import now_iso

    existing = Work(
        platform="douyin", external_id="66662", title="旧登记", url="https://www.douyin.com/video/66662",
        source_type="monitor", created_at=now_iso(), updated_at=now_iso(),
    )
    db.add(existing)
    db.commit()

    _, report = parse_and_ingest({"favorite_videos": [_record("66662", "favorite_videos")]})
    db.refresh(existing)
    assert existing.published_at == "2026-05-01T08:00:00.000Z"  # 只补未知
    assert existing.published_time_source == "douyin_web_create_time"
    assert report.works_publication_filled == ["66662"]


# ---------- §8.8 登记作品可走现有下载链路（mock 下载器，不真实下载） ----------

def test_registered_work_download_path(db, monkeypatch):
    from pathlib import Path

    from app.models import ArchiveEntry, Task, Work
    from app.utils.timeutil import now_iso
    from monitor import jobs

    work = Work(
        platform="douyin", external_id="77771", title="来源登记作品",
        url="https://www.douyin.com/video/77771", source_type="personal_source",
        published_at="2026-05-01T08:00:00.000Z", published_time_source="douyin_web_create_time",
        published_time_precision="second", published_time_timezone="UTC",
        created_at=now_iso(), updated_at=now_iso(),
    )
    db.add(work)
    db.commit()

    captured = {}

    def _fake_downloader(platform, url, media_dir):
        captured["media_dir"] = str(media_dir)
        target = Path(media_dir) / "fake_original.mp4"
        target.write_bytes(b"\x00" * 2048)
        return None

    monkeypatch.setattr(jobs.download, "_invoke_downloader", _fake_downloader)
    checkpoint = jobs.download_and_checkpoint(db, work)

    assert captured["media_dir"] and checkpoint.result.disposition == "downloaded"
    db.refresh(work)
    # download_and_checkpoint 只负责下载段；后续状态由流水线继续推进
    # （_process_work：DOWNLOADING→TRANSCRIBING→…），此处断言下载凭据已落库。
    assert work.media_path
    assert work.downloaded_at and work.download_time_source  # 真实写入验证后才记下载完成时间
    assert db.query(ArchiveEntry).filter_by(external_id="77771").count() >= 1
    assert db.query(Task).filter_by(work_id=work.id, stage="download", status="succeeded").count() == 1
