"""Fitness notes validate their real source references before they become visible."""
import json
from pathlib import Path

import pytest

pytest.importorskip("jsonschema")
pytest.importorskip("yaml")

from monitor import note_v3
from monitor.note_dispatcher import DispatcherConfig


def test_package_reuses_media_and_only_publishes_valid_pair(tmp_path, monkeypatch):
    media = tmp_path / "media"
    images = media / "post" / "images"
    images.mkdir(parents=True)
    (images / "03.jpg").write_bytes(b"fixture")
    # A missing first image must not shift the second image's original ordinal.
    (images / "douyin_images_manifest.json").write_text(json.dumps({
        "images": [{"file": "missing.jpg"}, {"file": "03.jpg"}],
    }))
    package = {
        "work_id": 42, "platform": "douyin", "blogger": "作者", "title": "训练图文",
        "url": "https://example.com/42", "frames_dir": "post/frames",
        "provenance": {"external_id": "123"},
    }
    output = tmp_path / "review"
    config = DispatcherConfig("fixture", ("fixture",), {}, 5, 1)
    queue_calls = []
    origin = {"type": "post", "start_ms": None, "end_ms": None,
              "transcript_segment_ids": [], "frame_ids": [], "image_indices": [2]}
    draft = {
        "metadata": {
            "title": "训练图文",
            "classification": {"primary_type": "图文收藏", "topics": [], "modules": []},
            "relations": {"related_media_ids": [], "related_note_ids": [], "knowledge_topic_ids": []},
            "processing": {"transcript_status": "unavailable", "frame_status": "done"},
        },
        "claims": [{"claim_id": "c1", "media_id": "douyin:123", "statement": "图示坐姿",
                    "claim_type": "technique", "origin": origin, "source_evidence_ids": ["v1"],
                    "verification": {"level": "V0", "status": "source_confirmed",
                                     "external_checked": False, "external_status": "not_checked", "evidence_ids": []},
                    "knowledge_topic_ids": [], "needs_review": False}],
        "evidence": [{"id": "v1", "detail": "图2 可见坐姿", "channel": "image", "origin": origin,
                      "citation": None, "search": None, "limits": None, "strength": None}],
        "training_sequence": [], "training_plan": None, "sports_nutrition": None,
        "corrections": [], "knowledge_links": [], "review_items": [],
    }

    def execute(_config, _prompt, stage: Path):
        data = note_v3.read_json(stage / "inputs.json")
        assert set(data["assets"]) == {"image_2"}
        assert data["assets"]["image_2"]["image_index"] == 2
        queue_calls.append(stage)
        link = "unknown.jpg" if len(queue_calls) == 1 else "assets/image_2.jpg"
        (stage / "body.md").write_text(f"# 训练图文\n\n图2示范坐姿[^v1]。\n\n"
                                       f"![坐姿]({link})\n\n[^v1]: 图2可见。\n")
        (stage / "note.json").write_text(json.dumps(draft))
        return 0, ""

    monkeypatch.setattr(note_v3, "run_executor", execute)
    with pytest.raises(ValueError, match="图片链接不在素材清单"):
        note_v3.generate(package, media, output, config, "standard")
    assert not list(output.rglob("note.*"))
    path, reused = note_v3.generate(package, media, output, config, "standard")
    assert not reused
    assert (path / "assets/image_2.jpg").read_bytes() == (images / "03.jpg").read_bytes()
    assert note_v3.generate(package, media, output, config, "standard") == (path, True)
    assert len(queue_calls) == 2
    note = note_v3.read_json(path / "note.json")
    assert note["metadata"]["processing"]["note_status"] == "draft"

    # A nutrition entry can cite an official document that qualifies the
    # advice, while still requiring its original source.
    note["metadata"]["classification"]["modules"] = ["运动营养"]
    note["sports_nutrition"] = [{
        "item": "剂量", "amount": None, "unit": None, "conditions": None,
        "author_basis": None, "limitations": "适用条件待核", "evidence_ids": ["v1", "d1"],
    }]
    note["evidence"].append({
        "id": "d1", "detail": "适用条件", "channel": "research", "origin": None, "search": None,
        "citation": {"author": "示例机构", "year": 2024, "title": "示例指南", "venue": "示例机构",
                     "url": "https://example.com/guide", "doi": None},
        "strength": None, "limits": "适用条件有限",
    })
    note["claims"][0]["verification"].update(
        external_checked=True, external_status="partly_supported", evidence_ids=["d1"],
    )
    body = "# 训练图文\n\n原图[^v1]，适用条件[^d1]。\n\n[^v1]: 原图\n[^d1]: 适用条件\n"
    inputs = note_v3.inventory(package, media, "standard")
    note_v3.validate(note, body, inputs)
    note["sports_nutrition"][0]["evidence_ids"] = ["d1"]
    with pytest.raises(ValueError, match="营养条目必须有原片出处"):
        note_v3.validate(note, body, inputs)
