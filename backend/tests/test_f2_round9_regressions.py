"""第九轮复审回归：journal 结构校验与图集级并发互斥。

全部合成数据、临时目录、无网络；并发子进程只持有本地锁文件。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from monitor.pipeline import douyin_images as images
from tests.test_f2_round6_regressions import png

ROOT = Path(__file__).resolve().parents[2]


def _album_with_backup(folder: Path, journal_text: str, name: str = "douyin-image-1.png"):
    folder.mkdir()
    original = folder / name
    old, partial_new = png((1, 2, 3)), png((4, 5, 6))
    original.write_bytes(partial_new)
    manifest = images._write_manifest(folder / images.MANIFEST_NAME, "synthetic", [
        {"file": name, "source_url": "https://example.test/old"}])
    backup = folder / ".backup-999999-abcd1234"
    backup.mkdir()
    (backup / name).write_bytes(old)
    (backup / "journal.json").write_bytes(journal_text.encode("utf-8"))
    return backup, original, old, manifest


@pytest.mark.parametrize("journal_text", [
    "{}",
    '{"pid": 999999}',  # 缺 manifest_sha256/restore/created
    '{"pid": "999999", "manifest_sha256": null, "restore": [], "created": []}',  # pid 类型错误
    '{"pid": 999999, "manifest_sha256": "not-a-sha", "restore": [], "created": []}',
    '{"pid": 999999, "manifest_sha256": null, "restore": "douyin-image-1.png", "created": []}',
    '{"pid": 999999, "manifest_sha256": null, "restore": ["../escape.png"], "created": []}',
    '{"pid": 999999, "manifest_sha256": null, "restore": [], "created": ["sub/dir.png"]}',
    '{"pid": 999999, "manifest_sha256": null, "restore": [], "created": [], "rolled_back": "yes"}',
    "{not json",
])
def test_malformed_journal_is_not_treated_as_committed(tmp_path, journal_text):
    """结构不完整/类型错误/越界文件名/损坏 JSON 都不得推断阶段或丢弃备份。"""
    folder = tmp_path / "album"
    backup, original, old, _manifest = _album_with_backup(folder, journal_text)
    assert images._restore_interrupted_backups(folder) is False
    assert backup.exists(), "malformed journal caused backup deletion"
    assert (backup / original.name).read_bytes() == old
    assert original.read_bytes() == png((4, 5, 6))  # 状态不明，不做文件改动


@pytest.mark.parametrize("missing", ["pid", "manifest_sha256", "restore", "created"])
def test_each_missing_required_field_blocks_recovery_download_and_commit(tmp_path, missing):
    """任一必需字段缺失（与显式 null 不同）→ 保留备份、阻塞下载与提交。"""
    folder = tmp_path / "album"
    folder.mkdir()
    original = folder / "douyin-image-1.png"
    old = png((1, 2, 3))
    original.write_bytes(png((4, 5, 6)))
    manifest = images._write_manifest(folder / images.MANIFEST_NAME, "synthetic", [
        {"file": original.name, "source_url": "https://example.test/old"}])
    backup = folder / ".backup-999999-abcd1234"
    backup.mkdir()
    (backup / original.name).write_bytes(old)
    journal = {"pid": 999999, "manifest_sha256": images._manifest_state(manifest)[1],
               "restore": [original.name], "created": []}
    journal.pop(missing)
    (backup / "journal.json").write_text(json.dumps(journal), encoding="utf-8")

    assert images._restore_interrupted_backups(folder) is False
    assert backup.exists() and (backup / original.name).read_bytes() == old
    with pytest.raises(images.DouyinImageError, match="未完成的恢复"):
        images.download_douyin_images_with_result(
            SimpleNamespace(external_id="review", media_urls=None), folder)
    stage = folder / ".stage-blocked"
    stage.mkdir()
    staged = stage / original.name
    staged.write_bytes(png((9, 9, 9)))
    with pytest.raises(OSError, match="未完成的恢复"):
        images._commit_staged_album(folder, [(staged, None, "https://example.test/n")], "review")


def test_explicit_null_journal_allows_full_download_entry(tmp_path, monkeypatch):
    """显式 null = 提交前无 manifest：回滚后下载入口可正常提交，旧回滚备份被清理。"""
    folder = tmp_path / "album"
    folder.mkdir()
    name = "douyin-image-1.png"
    (folder / name).write_bytes(png((10, 11, 12)))
    backup = folder / ".backup-999999-abcd1234"
    backup.mkdir()
    old = png((7, 8, 9))
    (backup / name).write_bytes(old)
    (backup / "journal.json").write_text(json.dumps({
        "pid": 999999, "manifest_sha256": None,
        "restore": [name], "created": []}), encoding="utf-8")
    assert images._restore_interrupted_backups(folder) is True
    assert (folder / name).read_bytes() == old
    monkeypatch.setattr(images, "_detail_images",
                        lambda aweme_id: (["https://example.test/new"], {"external_id": aweme_id}))
    monkeypatch.setattr(images, "fetch_url_bytes", lambda url: png((20, 21, 22)))
    manifest, result = images.download_douyin_images_with_result(
        SimpleNamespace(external_id="review", media_urls=None), folder)
    assert result.disposition == "downloaded"
    assert (folder / name).read_bytes() == png((20, 21, 22))
    assert json.loads(manifest.read_text())["images"][0]["source_url"] == "https://example.test/new"
    assert not backup.exists()  # 成功提交后清理已回滚备份


def test_valid_legacy_journal_is_recovered(tmp_path):
    """旧版有效记录（无 aweme_id/rolled_back）仍需正常回滚。"""
    folder = tmp_path / "album"
    folder.mkdir()
    original = folder / "douyin-image-1.png"
    old, partial_new = png((1, 2, 3)), png((4, 5, 6))
    original.write_bytes(partial_new)
    manifest = images._write_manifest(folder / images.MANIFEST_NAME, "synthetic", [
        {"file": original.name, "source_url": "https://example.test/old"}])
    backup = folder / ".backup-999999"
    backup.mkdir()
    (backup / original.name).write_bytes(old)
    (backup / "journal.json").write_text(json.dumps({
        "pid": 999999, "manifest_sha256": images._manifest_state(manifest)[1],
        "restore": [original.name], "created": [],
    }), encoding="utf-8")
    assert images._restore_interrupted_backups(folder) is True
    assert original.read_bytes() == old
    assert backup.exists() and json.loads((backup / "journal.json").read_text())["rolled_back"] is True


def _stage_two(folder: Path, tag: str, first_name: str, second_name: str):
    stage = folder / f".stage-{tag}"
    stage.mkdir()
    one, two = stage / first_name, stage / second_name
    one.write_bytes(png((10, 10, 10)))
    two.write_bytes(png((11, 11, 11)))
    return one, two


def test_commit_is_rejected_while_another_thread_holds_album_lock(tmp_path):
    folder = tmp_path / "album"
    folder.mkdir()
    first, second = "douyin-image-1.png", "douyin-image-2.png"
    (one, two) = _stage_two(folder, "a", first, second)
    holding, release = threading.Event(), threading.Event()

    def hold():
        with images._album_lock(folder):
            holding.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=hold)
    thread.start()
    assert holding.wait(timeout=5)
    try:
        with pytest.raises(images.AlbumBusyError):
            images._commit_staged_album(
                folder, [(one, None, "https://example.test/1"),
                         (two, None, "https://example.test/2")], "synthetic")
        with pytest.raises(images.AlbumBusyError):
            images.download_douyin_images_with_result(
                SimpleNamespace(external_id="x", media_urls=None), folder)
        assert not (folder / images.MANIFEST_NAME).exists()
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    # 锁释放后同一调用可以完成
    assert images._commit_staged_album(
        folder, [(one, None, "https://example.test/1"),
                 (two, None, "https://example.test/2")], "synthetic").exists()


def test_commit_is_rejected_while_another_process_holds_album_lock(tmp_path):
    folder = tmp_path / "album"
    folder.mkdir()
    original = folder / "douyin-image-1.png"
    original.write_bytes(png((1, 2, 3)))
    one, two = _stage_two(folder, "p", "douyin-image-1.png", "douyin-image-2.png")
    program = r"""
import sys, time
from pathlib import Path
from monitor.pipeline import douyin_images as images
folder = Path(sys.argv[1])
with images._album_lock(folder):
    print('locked', flush=True)
    time.sleep(3)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(part for part in (str(ROOT), env.get("PYTHONPATH", "")) if part)
    child = subprocess.Popen([sys.executable, "-c", program, str(folder)],
                             stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(images.AlbumBusyError):
            images._commit_staged_album(
                folder, [(one, None, "https://example.test/1"),
                         (two, None, "https://example.test/2")], "synthetic")
        assert original.read_bytes() == png((1, 2, 3))
        assert not (folder / images.MANIFEST_NAME).exists()
    finally:
        child.terminate()
        child.wait(timeout=10)
