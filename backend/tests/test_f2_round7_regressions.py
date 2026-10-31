"""第七轮复审回归：图集中断恢复协议、AVFoundation 视频校验、F2 候选重试。

全部使用合成媒体、临时目录、无网络；f2 与 HTTP 均为桩。
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from monitor.pipeline import douyin_images as images
from monitor.pipeline import douyin_video as video
from monitor.platforms import f2_fetch
from tests.test_f2_round6_regressions import mp4_bytes, png, real_mp4_bytes

ROOT = Path(__file__).resolve().parents[2]


def _run_child(program: str, *args: str, timeout: int = 20) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT), env.get("PYTHONPATH", "")) if part)
    return subprocess.run([sys.executable, "-c", program, *map(str, args)],
                          capture_output=True, text=True, timeout=timeout, env=env)


# ---------- 1. 图集提交/恢复协议 ----------

def test_crash_during_backup_copy_must_not_corrupt_intact_old_original(tmp_path):
    """备份复制中崩溃：原件未被替换；恢复不得用截断备份覆盖完好原件。"""
    folder = tmp_path / "images"
    folder.mkdir()
    old = png((12, 34, 56))
    original = folder / "douyin-image-1.png"
    original.write_bytes(old)
    stage = folder / ".staging-fixture"
    stage.mkdir()
    staged = stage / original.name
    staged.write_bytes(png((70, 80, 90)))
    program = r"""
import os, sys
from pathlib import Path
from monitor.pipeline import douyin_images as images
from monitor.pipeline.download_result import MediaFile
folder, staged = map(Path, sys.argv[1:])
def crash_mid_copy(source, target, *args, **kwargs):
    Path(target).write_bytes(Path(source).read_bytes()[:16])
    os._exit(37)
images.shutil.copy2 = crash_mid_copy
payload = staged.read_bytes()
images._commit_staged_album(folder, [(staged, MediaFile(staged, len(payload), 'image'), 'https://example.test/img')], 'fixture')
"""
    child = _run_child(program, folder, staged)
    assert child.returncode == 37, child.stderr
    assert original.read_bytes() == old  # 备份未完成，替换尚未发生
    assert list(folder.glob(".backup-*"))
    images._restore_interrupted_backups(folder)
    assert original.read_bytes() == old, "recovery replaced the intact original with a truncated backup"
    assert images._restore_interrupted_backups(folder) is True  # 重复恢复幂等
    assert original.read_bytes() == old


def test_crash_after_manifest_write_must_leave_coherent_album(tmp_path):
    """manifest 已写入后崩溃：恢复只清备份，不得把旧图配回新 manifest。"""
    folder = tmp_path / "images"
    folder.mkdir()
    original = folder / "douyin-image-1.png"
    old = png((12, 34, 56))
    new = png((70, 80, 90))
    original.write_bytes(old)
    images._write_manifest(folder / images.MANIFEST_NAME, "fixture",
                           [{"file": original.name, "source_url": "https://example.test/old"}])
    stage = folder / ".staging-fixture"
    stage.mkdir()
    staged = stage / original.name
    staged.write_bytes(new)
    program = r"""
import os, sys
from pathlib import Path
from monitor.pipeline import douyin_images as images
from monitor.pipeline.download_result import MediaFile
folder, staged = map(Path, sys.argv[1:])
real_rmtree = images.shutil.rmtree
def crash_before_cleanup(path, *args, **kwargs):
    if Path(path).name.startswith('.backup-'):
        os._exit(37)
    return real_rmtree(path, *args, **kwargs)
images.shutil.rmtree = crash_before_cleanup
payload = staged.read_bytes()
images._commit_staged_album(folder, [(staged, MediaFile(staged, len(payload), 'image'), 'https://example.test/new')], 'fixture')
"""
    child = _run_child(program, folder, staged)
    assert child.returncode == 37, child.stderr
    assert original.read_bytes() == new
    images._restore_interrupted_backups(folder)
    manifest = json.loads((folder / images.MANIFEST_NAME).read_text())
    url = manifest["images"][0]["source_url"]
    assert (original.read_bytes(), url) in [(old, "https://example.test/old"),
                                            (new, "https://example.test/new")], \
        "recovery paired old image bytes with the new manifest URL"
    assert not list(folder.glob(".backup-*"))


def test_failed_commit_restores_multi_images_and_removes_new_files(tmp_path, monkeypatch):
    """多图 + 新增文件：第三张替换失败 → 旧图全部恢复，新增文件移除，旧 manifest 不动。"""
    folder = tmp_path / "images"
    folder.mkdir()
    old_first, old_second = png((1, 2, 3)), png((4, 5, 6))
    first, second = folder / "douyin-image-1.png", folder / "douyin-image-2.png"
    first.write_bytes(old_first)
    second.write_bytes(old_second)
    old_manifest = json.dumps({"images": [
        {"file": first.name, "source_url": "https://example.test/o1"},
        {"file": second.name, "source_url": "https://example.test/o2"}]})
    (folder / images.MANIFEST_NAME).write_text(old_manifest, encoding="utf-8")
    stage = folder / ".staging-fixture"
    stage.mkdir()
    staged = []
    for index, color in enumerate([(7, 7, 7), (8, 8, 8), (9, 9, 9)], start=1):
        name = f"douyin-image-{index}.png"
        path = stage / name
        path.write_bytes(png(color))
        staged.append((path, None, f"https://example.test/n{index}"))
    real_replace = images.os.replace

    def fail_third(source, target):
        if Path(source).name == "douyin-image-3.png":
            raise OSError("simulated failure on new file")
        return real_replace(source, target)

    monkeypatch.setattr(images.os, "replace", fail_third)
    with pytest.raises(OSError, match="simulated failure"):
        images._commit_staged_album(folder, staged, "fixture")
    assert first.read_bytes() == old_first
    assert second.read_bytes() == old_second
    assert not (folder / "douyin-image-3.png").exists()
    assert (folder / images.MANIFEST_NAME).read_text(encoding="utf-8") == old_manifest
    assert not list(folder.glob(".backup-*"))


def test_rollback_failure_keeps_backup_and_next_recovery_retries(tmp_path, monkeypatch):
    """回滚复制失败时保留 journal 备份；下一次恢复重试成功后才清理。"""
    folder = tmp_path / "images"
    folder.mkdir()
    original = folder / "douyin-image-1.png"
    old, new = png((1, 2, 3)), png((4, 5, 6))
    original.write_bytes(old)
    images._write_manifest(folder / images.MANIFEST_NAME, "fixture",
                           [{"file": original.name, "source_url": "https://example.test/old"}])
    backup_dir = folder / ".backup-999999"
    backup_dir.mkdir()
    (backup_dir / original.name).write_bytes(old)
    (backup_dir / "journal.json").write_text(json.dumps({
        "pid": 999999,
        "aweme_id": "fixture",
        "manifest_sha256": images._manifest_digest(folder / images.MANIFEST_NAME),
        "restore": [original.name],
        "created": [],
    }), encoding="utf-8")
    original.write_bytes(new)  # 模拟替换已发生但 manifest 仍是旧内容

    real_atomic_copy = images._atomic_copy
    fail = {"on": True}

    def maybe_fail(source, target):
        if fail["on"]:
            raise OSError("simulated copy failure")
        return real_atomic_copy(source, target)

    monkeypatch.setattr(images, "_atomic_copy", maybe_fail)
    images._restore_interrupted_backups(folder)
    assert original.read_bytes() == new
    assert backup_dir.exists(), "backup removed although rollback failed"

    fail["on"] = False
    images._restore_interrupted_backups(folder)
    assert original.read_bytes() == old
    assert backup_dir.exists()  # 已回滚备份保留到下一次成功提交
    assert json.loads((backup_dir / "journal.json").read_text())["rolled_back"] is True


def test_unreadable_manifest_keeps_backup_and_blocks_new_commit(tmp_path, monkeypatch):
    """manifest 读取失败 → 状态未知：保留唯一备份，并拒绝开始新提交/新下载。"""
    folder = tmp_path / "album"
    folder.mkdir()
    original = folder / "douyin-image-1.png"
    old, replacement = png((1, 2, 3)), png((4, 5, 6))
    original.write_bytes(replacement)
    manifest = images._write_manifest(folder / images.MANIFEST_NAME, "review", [
        {"file": original.name, "source_url": "https://example.test/old"}])
    backup = folder / ".backup-999999"
    backup.mkdir()
    (backup / original.name).write_bytes(old)
    (backup / "journal.json").write_text(json.dumps({
        "pid": 999999, "aweme_id": "review",
        "manifest_sha256": images._manifest_state(manifest)[1],
        "restore": [original.name], "created": []}), encoding="utf-8")
    actual_read = Path.read_bytes

    def unreadable_manifest(path):
        if path == manifest:
            raise PermissionError("synthetic read failure")
        return actual_read(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable_manifest)
    assert images._restore_interrupted_backups(folder) is False
    assert backup.exists(), "unreadable manifest caused the only original backup to be deleted"
    assert original.read_bytes() == replacement  # 状态未知，不做任何文件改动
    stage = folder / ".stage-blocked"
    stage.mkdir()
    staged = stage / original.name
    staged.write_bytes(png((9, 9, 9)))
    with pytest.raises(OSError, match="未完成的恢复"):
        images._commit_staged_album(folder, [(staged, None, "https://example.test/new")], "review")
    with pytest.raises(images.DouyinImageError, match="未完成的恢复"):
        images.download_douyin_images_with_result(
            SimpleNamespace(external_id="review", media_urls=None), folder)
    monkeypatch.setattr(Path, "read_bytes", actual_read)
    assert images._restore_interrupted_backups(folder) is True
    assert original.read_bytes() == old
    assert backup.exists() and json.loads((backup / "journal.json").read_text())["rolled_back"] is True


# ---------- 2. AVFoundation 视频校验 ----------
def test_structured_but_unplayable_mp4_must_not_be_recorded(tmp_path, monkeypatch):
    """Codex 复现：合法 box + vide 标记 + 全零 mdat，AVFoundation 读不出视频轨 → 拒绝。"""
    class FakeResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self, chunk_size):
            yield mp4_bytes()

    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(video, "_detail_video_url",
                        lambda aweme_id: ("https://example.test/video", {"external_id": aweme_id}))
    with pytest.raises(video.DouyinVideoError):
        video.download_f2_video_with_result(SimpleNamespace(external_id="review123"), tmp_path)
    assert not list(tmp_path.glob("douyin-video-*.mp4"))


def test_playable_mp4_is_accepted_end_to_end(tmp_path, monkeypatch):
    payload = real_mp4_bytes(min_bytes=64 * 1024)

    class FakeResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self, chunk_size):
            yield payload

    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(video, "_detail_video_url",
                        lambda aweme_id: (["https://example.test/good.mp4"], {"external_id": aweme_id}))
    result = video.download_f2_video_with_result(SimpleNamespace(external_id="review-good"), tmp_path)
    assert result.disposition == "downloaded"
    assert result.files[0].size_bytes == len(payload)


def test_historical_unplayable_registration_is_not_reused(tmp_path, monkeypatch):
    """旧登记文件若无法通过媒体验证，复用被拒绝并重新下载。"""
    work = SimpleNamespace(external_id="review-history")
    final = tmp_path / f"douyin-video-{work.external_id}.mp4"
    bad = mp4_bytes()
    final.write_bytes(bad)
    record = {"path": str(final), "size_bytes": len(bad),
              "sha256": video._sha256_file(final), "media_type": "video"}
    good = real_mp4_bytes(min_bytes=64 * 1024)

    class FakeResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self, chunk_size):
            yield good

    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(video, "_detail_video_url",
                        lambda aweme_id: (["https://example.test/good.mp4"], {"external_id": aweme_id}))
    result = video.download_f2_video_with_result(work, tmp_path, registered_files=[record])
    assert result.disposition == "downloaded"
    assert final.read_bytes() == good


def test_video_probe_preserves_neighbors_and_uses_private_temp(tmp_path):
    """复用校验的探测输出不得覆盖/删除源文件旁的既有同名文件。"""
    payload = real_mp4_bytes(min_bytes=64 * 1024)
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    existing = media_dir / "existing.mp4"
    existing.write_bytes(payload)
    neighbor = media_dir / "existing.mp4.probe.mp4"
    sentinel = b"synthetic neighboring file; must stay untouched"
    neighbor.write_bytes(sentinel)
    before = {path.name: path.read_bytes() for path in media_dir.iterdir()}
    record = {"path": str(existing), "size_bytes": len(payload),
              "sha256": hashlib.sha256(payload).hexdigest(), "media_type": "video"}
    assert video._registered_video([record], media_dir) is not None
    after = {path.name: path.read_bytes() for path in media_dir.iterdir()}
    assert after == before, "video validation changed files beside the source"


def test_video_probe_failure_leaves_no_trace_beside_source(tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    bad = media_dir / "bad.mp4"
    bad.write_bytes(mp4_bytes())
    neighbor = media_dir / "bad.mp4.probe.mp4"
    neighbor.write_bytes(b"sentinel")
    before = {path.name: path.read_bytes() for path in media_dir.iterdir()}
    record = {"path": str(bad), "size_bytes": bad.stat().st_size,
              "sha256": hashlib.sha256(bad.read_bytes()).hexdigest()}
    assert video._registered_video([record], media_dir) is None  # 校验失败不得复用
    assert {path.name: path.read_bytes() for path in media_dir.iterdir()} == before


def test_video_probe_concurrent_validations_are_isolated(tmp_path):
    payload = real_mp4_bytes(min_bytes=64 * 1024)
    video_file = tmp_path / "video.mp4"
    video_file.write_bytes(payload)
    neighbor = tmp_path / "video.mp4.probe.mp4"
    neighbor.write_bytes(b"sentinel")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: video._validate_mp4_container(video_file), range(4)))
    assert results == [None, None, None, None]
    assert neighbor.read_bytes() == b"sentinel"
    assert video_file.read_bytes() == payload


# ---------- 3. 多个候选直链 ----------

def test_first_unavailable_candidate_tries_next_candidate(tmp_path, monkeypatch):
    first = "https://example.test/unavailable.mp4"
    second = "https://example.test/available.mp4"
    metadata = {"play_addr": {"url_list": [first, second]}}
    monkeypatch.setattr(video, "_detail_video_url",
                        lambda aweme_id: (f2_fetch._select_video_url(metadata),
                                          {"external_id": aweme_id}))
    monkeypatch.setattr(video, "_validate_mp4_container", lambda path: None)
    attempts = []

    class FakeResponse:
        def __init__(self, url):
            attempts.append(url)
            self.status_code = 403 if url == first else 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self, chunk_size):
            yield b"\x00\x00\x00\x18ftypmp42" + b"v" * 65536

    monkeypatch.setattr(httpx, "stream", lambda method, url, **kwargs: FakeResponse(url))
    result = video.download_f2_video_with_result(SimpleNamespace(external_id="review123"), tmp_path)
    assert result.disposition == "downloaded"
    assert attempts == [first, second]


def test_all_candidates_failing_reports_safe_message(tmp_path, monkeypatch):
    first = "https://example.test/a.mp4"
    second = "https://example.test/b.mp4"
    monkeypatch.setattr(video, "_detail_video_url",
                        lambda aweme_id: ([first, second], {"external_id": aweme_id}))
    attempts = []

    class FakeResponse:
        def __init__(self, url):
            attempts.append(url)
            self.status_code = 403

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self, chunk_size):
            yield b""

    monkeypatch.setattr(httpx, "stream", lambda method, url, **kwargs: FakeResponse(url))
    with pytest.raises(video.DouyinVideoError) as excinfo:
        video.download_f2_video_with_result(SimpleNamespace(external_id="review-all-fail"), tmp_path)
    assert attempts == [first, second]
    assert str(excinfo.value) == video._ALL_CANDIDATES_FAILED
    assert first not in str(excinfo.value) and second not in str(excinfo.value)


def test_single_candidate_failure_keeps_specific_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "_detail_video_url",
                        lambda aweme_id: (["https://example.test/single.mp4"], {"external_id": aweme_id}))

    class FakeResponse:
        status_code = 403

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self, chunk_size):
            yield b""

    monkeypatch.setattr(httpx, "stream", lambda method, url, **kwargs: FakeResponse())
    with pytest.raises(video.DouyinVideoError, match="HTTP 403"):
        video.download_f2_video_with_result(SimpleNamespace(external_id="review-single"), tmp_path)
