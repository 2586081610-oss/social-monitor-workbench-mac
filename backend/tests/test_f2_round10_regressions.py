"""第十轮复审回归：manifest 发布后中断必须留下完整旧版或完整新版。

仅合成图片、临时目录、无网络。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from monitor.pipeline import douyin_images as images
from tests.test_f2_round6_regressions import png


def _interrupt_after_manifest_replace(monkeypatch, manifest: Path):
    real_replace = images.os.replace

    def replace_then_interrupt(source, target):
        real_replace(source, target)
        if Path(target) == manifest:
            raise KeyboardInterrupt("synthetic interruption after manifest rename")

    monkeypatch.setattr(images.os, "replace", replace_then_interrupt)
    return real_replace


def _interrupt_before_manifest_replace(monkeypatch, manifest: Path):
    real_replace = images.os.replace

    def fail_manifest_replace(source, target):
        if Path(target) == manifest:
            raise KeyboardInterrupt("synthetic interruption before manifest rename")
        return real_replace(source, target)

    monkeypatch.setattr(images.os, "replace", fail_manifest_replace)
    return real_replace


def test_interrupt_after_manifest_replace_keeps_complete_new_album(tmp_path, monkeypatch):
    """旧图集 + 多图 + 新增文件：发布后中断 → 完整新版本，恢复不再改动。"""
    folder = tmp_path / "album"
    folder.mkdir()
    names = ["douyin-image-1.png", "douyin-image-2.png"]
    old_bytes = [png((1, 1, 1)), png((2, 2, 2))]
    for name, data in zip(names, old_bytes):
        (folder / name).write_bytes(data)
    manifest = images._write_manifest(folder / images.MANIFEST_NAME, "synthetic", [
        {"file": names[0], "source_url": "https://example.test/old1"},
        {"file": names[1], "source_url": "https://example.test/old2"}])
    stage = folder / ".stage"
    stage.mkdir()
    staged = []
    for index, data in enumerate([png((10, 10, 10)), png((11, 11, 11)), png((12, 12, 12))], start=1):
        path = stage / f"douyin-image-{index}.png"
        path.write_bytes(data)
        staged.append((path, None, f"https://example.test/new{index}"))

    _interrupt_after_manifest_replace(monkeypatch, manifest)
    with pytest.raises(KeyboardInterrupt):
        images._commit_staged_album(folder, staged, "synthetic")
    monkeypatch.undo()

    assert images._restore_interrupted_backups(folder) is True
    assert (folder / names[0]).read_bytes() == png((10, 10, 10))
    assert (folder / names[1]).read_bytes() == png((11, 11, 11))
    assert (folder / "douyin-image-3.png").read_bytes() == png((12, 12, 12))
    saved = json.loads(manifest.read_text())["images"]
    assert [row["source_url"] for row in saved] == [
        "https://example.test/new1", "https://example.test/new2", "https://example.test/new3"]
    assert not list(folder.glob(".backup-*"))


def test_interrupt_after_first_manifest_replace_keeps_new_album(tmp_path, monkeypatch):
    """首次提交（无旧 manifest）：发布后中断 → 新图片与新清单完整保留。"""
    folder = tmp_path / "album"
    folder.mkdir()
    manifest = folder / images.MANIFEST_NAME
    stage = folder / ".stage"
    stage.mkdir()
    staged = []
    for index, data in enumerate([png((20, 20, 20)), png((21, 21, 21))], start=1):
        path = stage / f"douyin-image-{index}.png"
        path.write_bytes(data)
        staged.append((path, None, f"https://example.test/new{index}"))
    _interrupt_after_manifest_replace(monkeypatch, manifest)
    with pytest.raises(KeyboardInterrupt):
        images._commit_staged_album(folder, staged, "synthetic")
    monkeypatch.undo()
    assert images._restore_interrupted_backups(folder) is True
    assert (folder / "douyin-image-1.png").exists()
    assert (folder / "douyin-image-2.png").exists()
    assert manifest.exists()
    assert [row["file"] for row in json.loads(manifest.read_text())["images"]] == [
        "douyin-image-1.png", "douyin-image-2.png"]


def test_interrupt_before_manifest_replace_keeps_complete_old_album(tmp_path, monkeypatch):
    """发布前中断：图片回滚旧字节，旧 manifest 原样，无残留事务。"""
    folder = tmp_path / "album"
    folder.mkdir()
    names = ["douyin-image-1.png", "douyin-image-2.png"]
    old_bytes = [png((1, 1, 1)), png((2, 2, 2))]
    for name, data in zip(names, old_bytes):
        (folder / name).write_bytes(data)
    manifest = images._write_manifest(folder / images.MANIFEST_NAME, "synthetic", [
        {"file": names[0], "source_url": "https://example.test/old1"},
        {"file": names[1], "source_url": "https://example.test/old2"}])
    old_manifest = manifest.read_bytes()
    stage = folder / ".stage"
    stage.mkdir()
    staged = []
    for index, data in enumerate([png((10, 10, 10)), png((11, 11, 11))], start=1):
        path = stage / f"douyin-image-{index}.png"
        path.write_bytes(data)
        staged.append((path, None, f"https://example.test/new{index}"))

    _interrupt_before_manifest_replace(monkeypatch, manifest)
    with pytest.raises(KeyboardInterrupt):
        images._commit_staged_album(folder, staged, "synthetic")
    monkeypatch.undo()
    assert images._restore_interrupted_backups(folder) is True
    assert (folder / names[0]).read_bytes() == old_bytes[0]
    assert (folder / names[1]).read_bytes() == old_bytes[1]
    assert manifest.read_bytes() == old_manifest
    assert not list(folder.glob(".backup-*"))


def test_recovery_restores_old_manifest_from_backup_copy(tmp_path):
    """崩溃现场：新图已替换、旧 manifest 丢失 → 用备份副本恢复完整旧版本。"""
    folder = tmp_path / "album"
    folder.mkdir()
    name = "douyin-image-1.png"
    old_manifest = json.dumps({"aweme_id": "synthetic", "saved_at": "2026-10-29T00:00:00Z",
                               "images": [{"file": name,
                                           "source_url": "https://example.test/old"}]}, indent=1)
    backup = folder / ".backup-999999-abcd1234"
    backup.mkdir()
    (backup / name).write_bytes(png((1, 2, 3)))
    (backup / "manifest.before").write_text(old_manifest, encoding="utf-8")
    journal = {"pid": 999999,
               "manifest_sha256": hashlib.sha256(old_manifest.encode("utf-8")).hexdigest(),
               "restore": [name], "created": []}
    (folder / name).write_bytes(png((4, 5, 6)))
    (backup / "journal.json").write_text(json.dumps(journal), encoding="utf-8")

    assert images._restore_interrupted_backups(folder) is True
    assert (folder / name).read_bytes() == png((1, 2, 3))
    assert (folder / images.MANIFEST_NAME).read_text(encoding="utf-8") == old_manifest
    assert backup.exists() and json.loads((backup / "journal.json").read_text())["rolled_back"] is True
