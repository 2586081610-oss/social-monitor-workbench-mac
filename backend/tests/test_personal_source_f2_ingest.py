"""F2 快照入库（ingest_snapshot）：marks / 多夹归属 / works 登记语义与幂等。

全部使用合成快照与隔离数据库；不触碰真实账号。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

from monitor.platforms.douyin_f2_source import parse_f2_result  # noqa: E402
from monitor.personal_source_ingest import ingest_snapshot  # noqa: E402
from app.models import PersonalSourceFolderItem, PersonalSourceMark, Work  # noqa: E402


def _work(aweme_id: str, **overrides) -> dict:
    base = {
        "external_id": aweme_id,
        "title": f"作品{aweme_id[-4:]}",
        "author": "测试作者",
        "url": f"https://www.douyin.com/video/{aweme_id}",
        "media_type": "video",
        "published_at": "2026-05-01T08:00:00Z",
    }
    base.update(overrides)
    return base


def _f2_result() -> dict:
    return {
        "ok": True,
        "warnings": [],
        "like": {"complete": True, "works": [_work("7000001")]},
        "collection": {"complete": True, "works": [_work("7000001"), _work("7000002")]},
        "collects": {
            "complete": True,
            "folders": [{"folder_id": "f1", "name": "夹A"}, {"folder_id": "f2", "name": "夹B"}],
            "folder_items": {
                "f1": {"complete": True, "works": [_work("8000001"), _work("7000001")]},
                "f2": {"complete": True, "works": [_work("8000001")]},
            },
        },
    }


def test_f2_ingest_registers_marks_folder_facts_and_works(db):
    snapshot = parse_f2_result(_f2_result())
    report = ingest_snapshot(snapshot, db)
    db.commit()

    # 点赞 + 平铺收藏两条事实；夹内-only 作品不断言收藏状态（按本用例 ID 过滤，
    # 测试库跨文件复用，其他用例可能已写入数据）
    ids = {"7000001", "7000002", "8000001"}
    marks = db.query(PersonalSourceMark).filter(PersonalSourceMark.external_id.in_(ids)).all()
    assert sorted((m.external_id, m.source_mark) for m in marks) == [
        ("7000001", "favorite"), ("7000001", "like"), ("7000002", "favorite"),
    ]
    assert all(m.source_channel == "f2" for m in marks)
    assert not any(m.source_mark == "favorite_folder" for m in marks)

    # 一作多夹归属：8000001 → f1+f2；7000001 → f1；DDR 单夹字段不受 F2 影响
    rows = db.query(PersonalSourceFolderItem).filter(PersonalSourceFolderItem.external_id.in_(ids)).all()
    assert sorted((r.external_id, r.folder_id) for r in rows) == [
        ("7000001", "f1"), ("8000001", "f1"), ("8000001", "f2"),
    ]
    assert all(r.folder_name in {"夹A", "夹B"} for r in rows)

    # 夹内-only 作品登记为 personal_source 作品（可被流水线发现/下载）
    works = {w.external_id: w for w in db.query(Work).filter(Work.external_id.in_(ids)).all()}
    assert set(works) == {"7000001", "7000002", "8000001"}
    assert all(w.source_type == "personal_source" for w in works.values())
    assert works["8000001"].published_at == "2026-05-01T08:00:00Z"
    assert works["8000001"].published_time_source == "douyin_web_create_time"

    # mark.work_id 与归属 work_id 都指向同一作品
    by_external = {w.external_id: w.id for w in works.values()}
    like_mark = next(m for m in marks if m.source_mark == "like")
    assert like_mark.work_id == by_external["7000001"]
    assert all(r.work_id == by_external[r.external_id] for r in rows)

    assert report.marks_created == 3
    assert report.works_created == 3
    assert report.folder_items_created == 3


def test_f2_ingest_folder_failure_keeps_existing_facts_and_imports_other_scopes(db):
    """单夹失败：已有点赞/收藏/夹归属保留，新独立范围与其他夹结果照常导入，报告 partial。"""
    ingest_snapshot(parse_f2_result(_f2_result()), db)
    db.commit()

    failed = {
        "ok": True,
        "warnings": ["收藏夹#2 读取失败（platform_error），保留其他范围与其他夹已完成的结果"],
        "like": {"complete": True, "works": [_work("7000003")]},
        "collection": {"complete": True, "works": [_work("7000004")]},
        "collects": {
            "complete": False,
            "folders_complete": True,
            "folders": [{"folder_id": "f1", "name": "夹A"}, {"folder_id": "f2", "name": "夹B"}],
            "folder_items": {
                "f1": {"complete": True, "works": [_work("8000002")]},
                # 被隔离的夹：明确 failed，且不带 works（不冒充合法空夹）
                "f2": {"complete": False, "failed": True, "kind": "platform_error"},
            },
            "folder_incomplete": ["folder#2"],
            "folder_failures": [{"folder": "folder#2", "kind": "platform_error"}],
        },
    }
    report = ingest_snapshot(parse_f2_result(failed), db)
    db.commit()

    assert "collections" in report.incomplete_lists
    assert "folder#2" in report.incomplete_lists
    # 新独立范围照常导入
    new_ids = {"7000003", "7000004"}
    marks = db.query(PersonalSourceMark).filter(
        PersonalSourceMark.external_id.in_(new_ids)).all()
    assert sorted((m.external_id, m.source_mark) for m in marks) == [
        ("7000003", "like"), ("7000004", "favorite"),
    ]
    # 既有夹归属全部保留（f2 失败只标记不完整，不删除/不推断取消）
    rows = db.query(PersonalSourceFolderItem).filter(
        PersonalSourceFolderItem.external_id.in_({"7000001", "8000001", "8000002"})).all()
    assert sorted((r.external_id, r.folder_id) for r in rows) == [
        ("7000001", "f1"), ("8000001", "f1"), ("8000001", "f2"), ("8000002", "f1"),
    ]
    # 既有 works 未被删除
    existing = {w.external_id for w in db.query(Work).filter(
        Work.external_id.in_({"7000001", "7000002", "8000001"})).all()}
    assert existing == {"7000001", "7000002", "8000001"}

    # 重试恢复：下一次完整快照不再报不完整，既有夹归属仍在
    recovered = ingest_snapshot(parse_f2_result(_f2_result()), db)
    db.commit()
    assert recovered.incomplete_lists == []
    rows = db.query(PersonalSourceFolderItem).filter(
        PersonalSourceFolderItem.external_id.in_({"7000001", "8000001"})).all()
    assert sorted((r.external_id, r.folder_id) for r in rows) == [
        ("7000001", "f1"), ("8000001", "f1"), ("8000001", "f2"),
    ]


def test_f2_ingest_budget_partial_keeps_completed_scopes_and_marks_incomplete(db):
    """预算到期：已完成点赞/夹照常导入，未完成范围/夹明确不完整，既有数据保留。"""
    ingest_snapshot(parse_f2_result(_f2_result()), db)
    db.commit()

    partial = {
        "ok": True,
        "partial": True,
        "budget_exhausted": True,
        "warnings": ["整体时间预算到期：已保留已完成结果，其余范围/夹标记不完整"],
        "like": {"complete": True, "works": [_work("7000005")]},
        "collection": {"complete": False, "budget_exhausted": True, "works": []},
        "collects": {
            "pages": 1, "folders_complete": True, "budget_exhausted": True,
            "folders": [{"folder_id": "f1", "name": "夹A"}, {"folder_id": "f2", "name": "夹B"}],
            "folder_items": {
                "f1": {"complete": True, "works": [_work("8000003")]},
                "f2": {"complete": False, "budget_exhausted": True},
            },
            "folder_incomplete": ["folder#2"],
            "folder_failures": [{"folder": "folder#2", "kind": "budget_exhausted"}],
            "complete": False,
        },
    }
    report = ingest_snapshot(parse_f2_result(partial), db)
    db.commit()

    assert report.direct_sync["like"] is True
    assert report.direct_sync["collection"] is False
    assert set(report.incomplete_lists) >= {"collection", "collections", "folder#2"}
    # 已完成的点赞与夹内作品照常入库
    assert db.query(PersonalSourceMark).filter_by(
        external_id="7000005", source_mark="like").one_or_none() is not None
    assert db.query(PersonalSourceFolderItem).filter_by(
        external_id="8000003", folder_id="f1").one_or_none() is not None
    # 既有夹归属（含未完成的 f2）不被删除
    assert db.query(PersonalSourceFolderItem).filter_by(
        external_id="8000001", folder_id="f2").one_or_none() is not None

    recovered = ingest_snapshot(parse_f2_result(_f2_result()), db)
    db.commit()
    assert recovered.incomplete_lists == []


def test_f2_ingest_mixed_folder_order_partial_preserves_existing_memberships(db):
    """错序夹 + 失败 + 超时：三个夹都明确不完整，既有归属保留，重试后恢复。"""
    ingest_snapshot(parse_f2_result(_f2_result()), db)
    db.commit()

    partial = {
        "ok": True, "partial": True, "budget_exhausted": True, "warnings": [],
        "collects": {
            "pages": 1, "folders_complete": True, "complete": False,
            "folders": [{"folder_id": "A", "name": "A"},
                        {"folder_id": "M", "name": "M"},
                        {"folder_id": "Z", "name": "Z"}],
            "folder_items": {
                "A": {"complete": False, "failed": True, "kind": "platform_error"},
                "M": {"complete": False, "budget_exhausted": True},
                "Z": {"complete": False, "budget_exhausted": True},
            },
            "folder_incomplete": ["folder#1", "folder#2", "folder#3"],
            "folder_failures": [
                {"folder": "folder#1", "kind": "platform_error"},
                {"folder": "folder#2", "kind": "budget_exhausted"},
                {"folder": "folder#3", "kind": "budget_exhausted"},
            ],
        },
    }
    report = ingest_snapshot(parse_f2_result(partial, scopes=("collections",)), db)
    db.commit()
    assert set(report.incomplete_lists) >= {
        "collections", "folder#1", "folder#2", "folder#3"}
    # 既有 f1/f2 归属不因本轮夹失败/未完成而删除
    rows = db.query(PersonalSourceFolderItem).filter(
        PersonalSourceFolderItem.external_id.in_({"7000001", "8000001"})).all()
    assert sorted((r.external_id, r.folder_id) for r in rows) == [
        ("7000001", "f1"), ("8000001", "f1"), ("8000001", "f2"),
    ]
    recovered = ingest_snapshot(parse_f2_result(_f2_result(), scopes=("collections",)), db)
    db.commit()
    assert recovered.incomplete_lists == []


def test_f2_ingest_rerun_is_idempotent_and_missing_membership_not_deleted(db):
    """重跑零新增；快照缺项不推断取消归属（归属行保留、仅 last_seen 更新）。"""
    snapshot = parse_f2_result(_f2_result())
    ingest_snapshot(snapshot, db)
    db.commit()

    reduced = _f2_result()
    # 第二次快照里 8000001 不再出现在 f2（平台翻页缺失/用户移动），不得删除归属
    reduced["collects"]["folder_items"]["f2"]["works"] = []
    reduced["collects"]["folder_items"].pop("f2")
    snapshot2 = parse_f2_result(reduced)
    report2 = ingest_snapshot(snapshot2, db)
    db.commit()

    assert report2.marks_created == 0 and report2.works_created == 0
    assert report2.folder_items_created == 0
    rows = db.query(PersonalSourceFolderItem).filter(
        PersonalSourceFolderItem.external_id.in_({"7000001", "8000001"}),
    ).all()
    assert sorted((r.external_id, r.folder_id) for r in rows) == [
        ("7000001", "f1"), ("8000001", "f1"), ("8000001", "f2"),
    ]
    # last_seen 更新到第二次快照（updated 语义），且 snapshot_id 跟随最新
    updated = next(r for r in rows if r.external_id == "8000001" and r.folder_id == "f2")
    # 缺席的归属行完全不动：snapshot_id 仍是第一次快照（不刷新、不删除）
    assert updated.snapshot_id == snapshot.snapshot_id


def test_f2_ingest_fills_publication_only_when_work_exists_with_unknown_date(db):
    """已存在作品零触碰，仅允许补全空缺发布时间；已有可信值不覆盖。"""
    # 独立 ID，避免与本文件其他用例（同库复用）冲突；两个作品都先于同步存在
    result = _f2_result()
    result["like"]["works"] = [_work("7100001")]
    result["collection"]["works"] = [_work("7100001"), _work("7100002")]
    result["collects"]["folder_items"] = {}
    snapshot = parse_f2_result(result)

    db.add(Work(platform="douyin", external_id="7100001", title="原标题", url="old-url",
                source_type="monitor", content_type="video",
                published_at="2020-01-01T00:00:00Z", published_time_source="monitor",
                published_time_precision="second", published_time_timezone="UTC"))
    # 完全未知发布时间的既有作品 → 允许补全
    db.add(Work(platform="douyin", external_id="7100002", title="无时间", url="u2",
                source_type="monitor", content_type="video"))
    db.flush()

    report = ingest_snapshot(snapshot, db)
    db.commit()

    work = db.query(Work).filter_by(platform="douyin", external_id="7100001").one()
    assert work.title == "原标题" and work.url == "old-url"  # 零触碰
    assert work.published_at == "2020-01-01T00:00:00Z"  # 已有可信时间不覆盖
    assert "7100001" not in report.works_publication_filled

    work2 = db.query(Work).filter_by(platform="douyin", external_id="7100002").one()
    assert work2.published_at == "2026-05-01T08:00:00Z"
    assert work2.published_time_source == "douyin_web_create_time"
    assert "7100002" in report.works_publication_filled
