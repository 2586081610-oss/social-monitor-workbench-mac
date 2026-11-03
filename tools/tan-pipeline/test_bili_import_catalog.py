"""bili_import_catalog 双平台选片测试：纯函数 + 合成数据，不碰真实库与真实 selection 文件。

运行：/Users/zzboy/Documents/ChatGPT/总控\\ 2/socialmonitor-mac/workbench/.venv/bin/python -m pytest scripts/test_bili_import_catalog.py -q
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import bili_import_catalog as mod


# ---------- _top_union：点赞 Top N ∪ 收藏 Top N 去重 ----------

def test_top_union_merges_both_charts():
    entries = [
        {"key": "a", "like": 100, "favorite": 10},
        {"key": "b", "like": 90, "favorite": 50},
        {"key": "c", "like": 80, "favorite": 40},
    ]
    union = mod._top_union(entries, 2)
    # 收藏榜前2: b(50), c(40)；点赞榜前2: a(100), b(90) → 并集 a,b,c
    assert set(union) == {"a", "b", "c"}
    assert union["b"]["reason"] == "收藏榜第1 + 点赞榜第2"
    assert union["a"]["reason"] == "点赞榜第1"
    assert union["c"]["reason"] == "收藏榜第2"


def test_top_union_top1_dedup():
    entries = [{"key": "a", "like": 1, "favorite": 1}, {"key": "b", "like": 2, "favorite": 2}]
    union = mod._top_union(entries, 1)
    assert set(union) == {"b"}  # 双榜第1同为 b，去重后只 1 条


# ---------- 抖音选片：合成 pages 数据 + 假 work_id 解析 ----------

def _aweme(aid, *, digg, collect, images=None, duration_ms=0, desc=""):
    a = {
        "aweme_id": aid,
        "desc": desc or f"作品{aid}",
        "create_time": 1770000000,
        "statistics": {"digg_count": digg, "collect_count": collect},
    }
    if images:
        a["images"] = [{"url_list": [f"https://cdn/{aid}/{i}.jpg"]} for i in range(images)]
    else:
        a["video"] = {"duration": duration_ms}
    return a


def _write_pages(tmp_path, awemes):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.json").write_text(json.dumps({"aweme_list": awemes}, ensure_ascii=False))
    return pages


def _fake_db(mapping, monkeypatch):
    monkeypatch.setattr(
        mod.work_repo, "get_by_platform_external",
        lambda db, platform, ext: MagicMock(id=mapping[(platform, ext)]) if (platform, ext) in mapping else None,
    )
    return MagicMock()


def test_douyin_selection_excludes_images_by_default(tmp_path, monkeypatch):
    pages = _write_pages(tmp_path, [
        _aweme("v1", digg=100, collect=10, duration_ms=65000),
        _aweme("v2", digg=90, collect=90),
        _aweme("img1", digg=99999, collect=99999, images=3),  # 数据再高也不参选
    ])
    db = _fake_db({("douyin", "v1"): 11, ("douyin", "v2"): 12}, monkeypatch)

    part = mod.select_top_douyin(db, top=2, pages_dir=pages)

    assert [i["aweme_id"] for i in part["selected"]] == ["v2", "v1"]
    assert all(i["content_type"] == "video" for i in part["selected"])
    assert [s["aweme_id"] for s in part["skipped_images"]] == ["img1"]
    assert part["skipped_images"][0]["images_count"] == 3
    v1 = next(i for i in part["selected"] if i["aweme_id"] == "v1")
    assert v1["duration_sec"] == 65 and v1["work_id"] == 11 and v1["platform"] == "douyin"


def test_douyin_selection_include_images_marks_content_type(tmp_path, monkeypatch):
    pages = _write_pages(tmp_path, [
        _aweme("v1", digg=10, collect=10),
        _aweme("img1", digg=100, collect=100, images=2),
    ])
    db = _fake_db({("douyin", "img1"): 21}, monkeypatch)

    part = mod.select_top_douyin(db, top=1, include_images=True, pages_dir=pages)

    assert [i["aweme_id"] for i in part["selected"]] == ["img1"]
    assert part["selected"][0]["content_type"] == "image"
    assert part["missing"] == []  # img1 有 work_id


def test_douyin_selection_missing_work_id_listed(tmp_path, monkeypatch):
    pages = _write_pages(tmp_path, [_aweme("ghost", digg=1, collect=1)])
    db = _fake_db({}, monkeypatch)  # 库里没有该作品

    part = mod.select_top_douyin(db, top=1, pages_dir=pages)

    assert part["selected"][0]["work_id"] is None
    assert [m["aweme_id"] for m in part["missing"]] == ["ghost"]


# ---------- 合并与备份 ----------

def test_merge_keeps_legacy_bilibili_entries_and_tags_platform():
    existing = {"selected": [{"work_id": 16686, "bvid": "BV1x", "title": "旧B站", "reason": "收藏榜第1"}]}
    parts = {"douyin": {"selected": [{"work_id": 99, "platform": "douyin", "aweme_id": "d1",
                                      "title": "抖音", "reason": "点赞榜第1"}],
                        "missing": [],
                        "skipped_images": [{"aweme_id": "im1", "title": "图", "favorite": 1,
                                            "like": 2, "images_count": 2, "note": "n"}]}}

    out = mod.merge_selection(existing, parts, top=10)

    assert [(i.get("platform"), i.get("work_id")) for i in out["selected"]] == [("bilibili", 16686), ("douyin", 99)]
    assert out["selected"][0]["bvid"] == "BV1x"  # 旧字段原样保留
    assert out["skipped_images_douyin"][0]["aweme_id"] == "im1"


def test_merge_replaces_only_recomputed_platform():
    existing = {"selected": [
        {"work_id": 1, "platform": "bilibili", "bvid": "BVold"},
        {"work_id": 2, "platform": "douyin", "aweme_id": "old_d"},
    ]}
    parts = {"douyin": {"selected": [{"work_id": 3, "platform": "douyin", "aweme_id": "new_d",
                                      "title": "t", "reason": "r"}], "missing": []}}

    out = mod.merge_selection(existing, parts, top=10)

    assert [(i.get("platform"), i.get("work_id")) for i in out["selected"]] == [("bilibili", 1), ("douyin", 3)]


def test_backup_selection_timestamped_and_non_destructive(tmp_path):
    target = tmp_path / "pipeline_selection.json"
    target.write_text("{}")
    first = mod.backup_selection(target)
    second = mod.backup_selection(target)
    assert first is not None and second is not None
    assert first.name.startswith("pipeline_selection.json.bak-")
    assert first != second  # 同秒也不覆盖旧备份
    assert mod.backup_selection(tmp_path / "nope.json") is None


# ---------- main：dry-run 默认不写盘 ----------

def _stub_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "init_db", lambda: None)
    db = MagicMock()
    db.query.return_value.filter.return_value.count.return_value = 0

    class _FakeSession:
        def __enter__(self):
            return db

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(mod, "SessionLocal", _FakeSession)
    monkeypatch.setattr(mod, "select_top", lambda db, top=10: {"selected": [], "missing": []})
    monkeypatch.setattr(mod, "select_top_douyin", lambda db, top=10, include_images=False: {
        "selected": [{"work_id": 7, "platform": "douyin", "aweme_id": "d7", "title": "t",
                      "reason": "点赞榜第1", "favorite": 1, "like": 2, "duration_sec": 3,
                      "content_type": "video"}],
        "missing": [], "skipped_images": []})
    out = tmp_path / "pipeline_selection.json"
    monkeypatch.setattr(mod, "SELECTION_OUT", out)
    return out


def test_main_default_is_dry_run(monkeypatch, tmp_path, capsys):
    out = _stub_runtime(monkeypatch, tmp_path)
    mod.main([])
    assert not out.exists()
    assert "DRY-RUN" in capsys.readouterr().out


def test_main_write_requires_explicit_platform(monkeypatch, tmp_path):
    _stub_runtime(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        mod.main(["--write"])


def test_main_write_merges_and_backs_up(monkeypatch, tmp_path):
    out = _stub_runtime(monkeypatch, tmp_path)
    out.write_text(json.dumps({"selected": [{"work_id": 16686, "bvid": "BV1x", "title": "旧", "reason": "r"}]}))

    mod.main(["--platform", "douyin", "--top", "10", "--write"])

    written = json.loads(out.read_text())
    assert [(i.get("platform"), i.get("work_id")) for i in written["selected"]] == [("bilibili", 16686), ("douyin", 7)]
    backups = list(tmp_path.glob("pipeline_selection.json.bak-*"))
    assert len(backups) == 1  # 写前已备份
