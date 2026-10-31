"""AI 总结交付目标测试：默认工具配置、多目标清单投递、provenance 留痕。

背景（2026-10-31 用户需求）：总结任务包不再硬编码走 WorkBuddy——支持
workbuddy/codex/trae/zcode 目标，前端可改默认工具；清单带数据来源留痕，
完成时写 *.provenance.json 回执。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import summary_target_service
from app.services.note_service import write_pending_manifest


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """每个测试用独立配置文件，绝不读写真实 config/summary_settings.json。"""
    monkeypatch.setattr(
        summary_target_service,
        "_settings_path",
        lambda: tmp_path / "config" / "summary_settings.json",
    )


class _FakeWork:
    def __init__(self) -> None:
        self.id = 42
        self.platform = "bilibili"
        self.external_id = "BV1TEST"
        self.url = "https://www.bilibili.com/video/BV1TEST"
        self.title = "测试作品标题"
        self.published_at = None
        self.created_at = None
        self.media_path = None
        self.transcript_path = None
        self.creator = None
        self.group = None


def test_defaults_when_config_missing(tmp_path):
    """配置文件缺失时返回内置默认：workbuddy 为默认，四个内置目标齐全。"""
    data = summary_target_service.load_summary_settings()
    assert data["default_target"] == "workbuddy"
    assert set(data["targets"]) == {"workbuddy", "codex", "trae", "zcode"}
    # 读路径无副作用：不落盘
    assert not (tmp_path / "config" / "summary_settings.json").exists()


def test_set_default_persists_and_validates(tmp_path):
    summary_target_service.set_default_target("codex")
    assert summary_target_service.get_default_target() == "codex"
    saved = json.loads((tmp_path / "config" / "summary_settings.json").read_text())
    assert saved["default_target"] == "codex"


def test_set_default_rejects_unknown():
    from app.core.errors import AppError

    with pytest.raises(AppError):
        summary_target_service.set_default_target("not-a-target")


def test_resolve_workbuddy_keeps_legacy_contract(tmp_path, monkeypatch):
    """workbuddy 目标维持原契约：pending 在 workbuddy/pending，输出在 Obsidian。"""
    monkeypatch.setattr(summary_target_service.settings, "pending_dir", tmp_path / "workbuddy" / "pending")
    monkeypatch.setattr(summary_target_service.settings, "obsidian_root", tmp_path / "Obsidian")
    resolved = summary_target_service.resolve_target("workbuddy")
    assert resolved["kind"] == "workbuddy"
    assert resolved["pending_dir"] == tmp_path / "workbuddy" / "pending"


def test_manifest_goes_to_default_target_dir(tmp_path, monkeypatch):
    """默认工具改为 codex 后，清单应落在 codex 的 pending 目录且带 target/provenance。"""
    monkeypatch.setattr(summary_target_service.settings, "workspace_root", tmp_path)
    summary_target_service.set_default_target("codex")

    manifest_rel = write_pending_manifest(_FakeWork())
    manifest_path = tmp_path / manifest_rel
    assert manifest_path.is_file()
    assert "summary_targets/codex/pending" in manifest_rel
    manifest = json.loads(manifest_path.read_text())
    assert manifest["target"] == "codex"
    assert manifest["provenance"]["source_url"] == "https://www.bilibili.com/video/BV1TEST"
    assert manifest["expected_output"].startswith("summary_targets/codex/output/")


def test_manifest_workbuddy_default_unchanged(tmp_path, monkeypatch):
    """默认 workbuddy 时行为与旧契约一致（向后兼容）。"""
    monkeypatch.setattr(summary_target_service.settings, "workspace_root", tmp_path)
    monkeypatch.setattr(summary_target_service.settings, "pending_dir", tmp_path / "workbuddy" / "pending")
    manifest_rel = write_pending_manifest(_FakeWork())
    assert manifest_rel.startswith("workbuddy/pending/")
    manifest = json.loads((tmp_path / manifest_rel).read_text())
    assert manifest["expected_output"].startswith("Obsidian/")
    assert manifest["target"] == "workbuddy"


def test_explicit_target_overrides_default(tmp_path, monkeypatch):
    monkeypatch.setattr(summary_target_service.settings, "workspace_root", tmp_path)
    manifest_rel = write_pending_manifest(_FakeWork(), target="zcode")
    assert "summary_targets/zcode/pending" in manifest_rel


def test_unknown_target_rejected(tmp_path, monkeypatch):
    from app.core.errors import AppError

    monkeypatch.setattr(summary_target_service.settings, "workspace_root", tmp_path)
    with pytest.raises(AppError):
        write_pending_manifest(_FakeWork(), target="not-a-target")


def test_provenance_hashes_real_files(tmp_path):
    """provenance 对存在的素材文件计算 sha256；不存在的留 None 不编造。"""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 1024)
    prov = summary_target_service.build_provenance(
        _FakeWork(), video_path=video, transcript_path=tmp_path / "nope.json", frames_count=0
    )
    roles = {m["role"]: m for m in prov["media_files"]}
    assert roles["video"]["sha256"] is not None
    assert roles["transcript"]["sha256"] is None
    assert prov["frames_count"] == 0
