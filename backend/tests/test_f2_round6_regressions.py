"""第六轮复审回归（全部合成数据、临时目录、无网络、无真实平台）。

覆盖：
1. 损坏图片（只有魔数/截断）不得产生下载成功凭据；
2. 图集提交中途失败/manifest 写入失败不得损坏既有原件与登记；
3. 11 张以上图集复用按 manifest 顺序映射（文件名数字序陷阱）；
4. 伪 MP4（无 moov/无视频轨/截断）不得产生下载成功凭据；
5. 详情视频直链必须采用固定版 F2 的选优契约（bit_rate-only 与含 fallback）；
6. 手动链接分析的错误原文/路径不得进入作品、任务与日志。
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import subprocess
import sys
import types
import zlib
from types import SimpleNamespace

import pytest

from app.models import Task, Work
from monitor import jobs
from monitor.pipeline import douyin_images as images
from monitor.pipeline import douyin_video as video
from monitor.platforms import f2_fetch


def png(rgb=(10, 20, 30)) -> bytes:
    """合成 2x2 完整 PNG（IHDR/IDAT/IEND 齐全，可被 sips 完整解码）。"""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    row = b"\x00" + bytes(rgb) * 2
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row + row))
            + chunk(b"IEND", b""))


def mp4_bytes(mdat_size: int = 64 * 1024, handler: bytes = b"vide") -> bytes:
    """合成结构完整的 MP4（仅容器结构，无真实视频帧；不用于可播放性正例）。"""
    def box(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload) + 8) + kind + payload

    hdlr = box(b"hdlr", b"\x00" * 8 + handler + b"\x00" * 12 + b"\x00")
    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")
    moov = box(b"moov", box(b"trak", box(b"mdia", hdlr)))
    return ftyp + moov + box(b"mdat", b"\x00" * mdat_size)


# 由 AVFoundation 生成的 0.3 秒 64x64 H.264 MP4（合成样本，本机生成后以 base64 固化）
REAL_MP4_B64 = "AAAAHGZ0eXBtcDQyAAAAAWlzb21tcDQxbXA0MgAAAAFtZGF0AAAAAAAAAKgAAAA1BgUtR1ZK3FxMQz+U78URPNFDqAEAAAMAAQMAAAMAAQIAAeYACwAAAwAAAwAACYgMA5ErAYAAAAAxJbggH4AuSqwRNmYXSACJwyG5akafRwrPDoFqVCtjHBP+QvRWhyAAGk1PzfAEsEedgAAAABEh4QhfAoAvQrFXFN4ACQ7CtgAAABEBqIGK/1jQw/VufW+ACvdnuAAAAvFtb292AAAAbG12aGQAAAAA5wiDcecIg3EAAAJYAAAAtAABAAABAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAACfXRyYWsAAABcdGtoZAAAAAHnCINx5wiDcQAAAAEAAAAAAAAAtAAAAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAAAAAAQAAAAEAAAAAAACRlZHRzAAAAHGVsc3QAAAAAAAAAAQAAALQAAAA8AAEAAAAAAfVtZGlhAAAAIG1kaGQAAAAA5wiDcecIg3EAAAJYAAAAtFXEAAAAAAAxaGRscgAAAAAAAAAAdmlkZQAAAAAAAAAAAAAAAENvcmUgTWVkaWEgVmlkZW8AAAABnG1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAVxzdGJsAAAAoXN0c2QAAAAAAAAAAQAAAJFhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAEAAQABIAAAASAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGP//AAAAJ2F2Y0MBZAAL/+EADCdkAAusVlDDeBBhFAEABCjuPLD9+PgAAAAACmZpZWwBAAAAAApjaHJtAAAAAAAYc3R0cwAAAAAAAAABAAAAAwAAADwAAAAoY3R0cwAAAAAAAAADAAAAAQAAADwAAAABAAAAeAAAAAEAAAAAAAAAFHN0c3MAAAAAAAAAAQAAAAEAAAAPc2R0cAAAAAAgEBgAAAAcc3RzYwAAAAAAAAABAAAAAQAAAAMAAAABAAAAIHN0c3oAAAAAAAAAAAAAAAMAAABuAAAAFQAAABUAAAAUc3RjbwAAAAAAAAABAAAALA=="


def real_mp4_bytes(min_bytes: int = 0) -> bytes:
    """可被 AVFoundation 读取的真实 H.264 合成视频；尾部补 free box 达到最小体积。"""
    data = base64.b64decode(REAL_MP4_B64)
    if min_bytes and len(data) < min_bytes:
        data += struct.pack(">I", min_bytes - len(data) + 8) + b"free" + \
            b"\x00" * (min_bytes - len(data))
    return data


# ---------- 1. 损坏图片不得成为成功凭据 ----------

def test_corrupt_image_must_not_generate_success_checkpoint(db, monkeypatch):
    """PNG 魔数后接随机字节（无 IHDR/像素）不得登记为原图。"""
    work = Work(platform="douyin", external_id="9600000000000000001", title="synthetic",
                url="https://example.test/work", source_type="personal_source",
                content_type="image", status="NEW")
    db.add(work)
    db.commit()
    monkeypatch.setattr(images, "_detail_images",
                        lambda aweme_id: (["https://example.test/image"], {"external_id": aweme_id}))
    monkeypatch.setattr(images, "fetch_url_bytes", lambda url: b"\x89PNG\r\n\x1a\n" + b"junk" * 12)
    monkeypatch.setattr(jobs, "_sync_checkpoint_after_commit", lambda *args: None)
    with pytest.raises(jobs.PipelineOfflineError):
        jobs.download_and_checkpoint(db, work)
    db.rollback()
    assert db.query(Task).filter_by(work_id=work.id, stage="download", status="succeeded").count() == 0


def test_truncated_valid_image_is_rejected(tmp_path, monkeypatch):
    """真 PNG 截断（缺 IEND/IDAT 尾部）也不得当作原图。"""
    folder = tmp_path / "images"
    folder.mkdir()
    monkeypatch.setattr(images, "_detail_images",
                        lambda aweme_id: (["https://example.test/t"], {"external_id": aweme_id}))
    monkeypatch.setattr(images, "fetch_url_bytes", lambda url: png()[:40])
    with pytest.raises(images.DouyinImageError):
        images.download_douyin_images_with_result(SimpleNamespace(external_id="9600010", media_urls=None), folder)
    assert not list(folder.glob("douyin-image-*"))
    assert not list(folder.glob(".staging-*"))


# ---------- 2. 图集提交失败/中断保护 ----------

def _existing_album(folder):
    folder.mkdir(parents=True)
    first, second = folder / "douyin-image-1.png", folder / "douyin-image-2.png"
    old_first, old_second = png((12, 34, 56)), png((65, 43, 21))
    first.write_bytes(old_first)
    second.write_bytes(old_second)
    manifest_doc = {"images": [
        {"file": first.name, "source_url": "https://example.test/old1"},
        {"file": second.name, "source_url": "https://example.test/old2"},
    ]}
    (folder / images.MANIFEST_NAME).write_text(json.dumps(manifest_doc), encoding="utf-8")
    return first, second, old_first, old_second


def test_album_commit_failure_must_preserve_preexisting_originals(tmp_path, monkeypatch):
    """staging 全部成功后第二张替换失败：第一张也必须回滚，manifest 不动。"""
    folder = tmp_path / "images"
    first, second, old_first, old_second = _existing_album(folder)
    urls = ["https://example.test/new1", "https://example.test/new2"]
    monkeypatch.setattr(images, "_detail_images",
                        lambda aweme_id: (urls, {"external_id": aweme_id}))
    monkeypatch.setattr(images, "fetch_url_bytes",
                        lambda url: png((111, 1, 1)) if url.endswith("1") else png((1, 111, 1)))
    real_replace = images.os.replace

    def fail_second(source, target):
        if str(source).startswith(str(folder / ".staging-")) and \
                str(source).endswith(second.name):
            raise OSError("simulated disk failure on second file")
        return real_replace(source, target)

    monkeypatch.setattr(images.os, "replace", fail_second)
    with pytest.raises(OSError, match="simulated disk failure"):
        images.download_douyin_images_with_result(
            SimpleNamespace(external_id="96002", media_urls=None), folder)
    assert first.read_bytes() == old_first, "first original changed after interrupted album commit"
    assert second.read_bytes() == old_second
    manifest = json.loads((folder / images.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert [row["file"] for row in manifest["images"]] == [first.name, second.name]
    assert not list(folder.glob(".backup-*")) and not list(folder.glob(".staging-*"))


def test_manifest_write_failure_rolls_back_replaced_files(tmp_path, monkeypatch):
    """图片全部替换后 manifest 写入失败：图片回滚到旧字节，旧 manifest 保持。"""
    folder = tmp_path / "images"
    first, second, old_first, old_second = _existing_album(folder)
    old_manifest = (folder / images.MANIFEST_NAME).read_text(encoding="utf-8")
    monkeypatch.setattr(images, "_detail_images",
                        lambda aweme_id: (["https://example.test/new1", "https://example.test/new2"],
                                          {"external_id": aweme_id}))
    monkeypatch.setattr(images, "fetch_url_bytes", lambda url: png((7, 7, 7)))

    def boom(*args, **kwargs):
        raise OSError("simulated manifest write failure")

    monkeypatch.setattr(images, "_write_manifest", boom)
    with pytest.raises(OSError, match="simulated manifest write failure"):
        images.download_douyin_images_with_result(
            SimpleNamespace(external_id="96003", media_urls=None), folder)
    assert first.read_bytes() == old_first
    assert second.read_bytes() == old_second
    assert (folder / images.MANIFEST_NAME).read_text(encoding="utf-8") == old_manifest
    assert not list(folder.glob(".backup-*"))


def test_interrupted_backup_is_rolled_back_only_for_dead_owner(tmp_path):
    """进程崩溃遗留的 journal 备份在下一次运行时恢复旧字节；存活进程的备份不干预。"""
    folder = tmp_path / "images"
    folder.mkdir()
    final = folder / "douyin-image-1.png"
    final.write_bytes(png((200, 0, 0)))

    def make_backup(pid, payload):
        backup = folder / f".backup-{pid}"
        backup.mkdir()
        (backup / final.name).write_bytes(payload)
        (backup / "journal.json").write_text(json.dumps({
            "pid": pid, "aweme_id": "fixture", "manifest_sha256": None,
            "restore": [final.name], "created": [],
        }), encoding="utf-8")
        return backup

    dead = make_backup(2 ** 22, png((1, 2, 3)))
    assert images._restore_interrupted_backups(folder) is True
    assert final.read_bytes() == png((1, 2, 3))
    assert dead.exists()  # 已回滚备份保留到下一次成功提交
    assert json.loads((dead / "journal.json").read_text())["rolled_back"] is True

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        live = make_backup(other.pid, png((9, 9, 9)))
        assert images._restore_interrupted_backups(folder) is False  # 其他进程仍在提交 → 阻塞
        assert final.read_bytes() == png((1, 2, 3))  # 存活进程的提交不被回滚
        assert live.exists()
    finally:
        other.terminate()
        other.wait(timeout=10)


# ---------- 3. 11 张图集复用顺序 ----------

def test_reused_eleven_image_album_retains_numeric_order_and_mapping(tmp_path, monkeypatch):
    folder = tmp_path / "images"
    folder.mkdir()
    records, frames, urls = [], [], []
    for i in range(1, 12):
        url = f"https://example.test/picture/{i}"
        path = folder / f"douyin-image-{i}.png"
        payload = png((i, 2, 3))
        path.write_bytes(payload)
        records.append({"path": str(path), "size_bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest()})
        frames.append({"file": path.name, "source_url": url})
        urls.append(url)
    (folder / images.MANIFEST_NAME).write_text(json.dumps({"images": frames}), encoding="utf-8")
    monkeypatch.setattr(images, "_detail_images",
                        lambda aweme_id: (urls, {"external_id": aweme_id}))
    manifest, result = images.download_douyin_images_with_result(
        SimpleNamespace(external_id="96003", media_urls=None), folder, registered_files=records)
    assert result.disposition == "reused"
    assert [media.path.name for media in result.files] == [f"douyin-image-{i}.png" for i in range(1, 12)]
    saved = json.loads(manifest.read_text(encoding="utf-8"))["images"]
    assert [(row["file"], row["source_url"]) for row in saved] == [
        (f"douyin-image-{i}.png", urls[i - 1]) for i in range(1, 12)]


@pytest.mark.parametrize("mutate", ["missing_row", "wrong_mapping", "wrong_hash", "missing_file_key"])
def test_incomplete_or_mismatched_manifest_is_not_reused(tmp_path, mutate):
    folder = tmp_path / "images"
    folder.mkdir()
    urls = [f"https://example.test/p/{i}" for i in range(1, 4)]
    records = []
    for i in range(1, 4):
        path = folder / f"douyin-image-{i}.png"
        payload = png((i, 0, 0))
        path.write_bytes(payload)
        records.append({"path": str(path), "size_bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest()})
    frames = [{"file": f"douyin-image-{i}.png", "source_url": urls[i - 1]} for i in range(1, 4)]
    if mutate == "missing_row":
        frames = frames[:2]
    elif mutate == "wrong_mapping":
        frames[0]["source_url"], frames[1]["source_url"] = frames[1]["source_url"], frames[0]["source_url"]
    elif mutate == "wrong_hash":
        records[1]["sha256"] = "0" * 64
    elif mutate == "missing_file_key":
        del frames[0]["file"]
    (folder / images.MANIFEST_NAME).write_text(json.dumps({"images": frames}), encoding="utf-8")
    assert images._registered_matches(folder, urls, records) is None


# ---------- 4. 伪 MP4 不得成为成功凭据 ----------

def test_corrupt_mp4_must_not_generate_download_receipt(tmp_path, monkeypatch):
    import httpx

    payload = b"\x00\x00\x00\x18ftypmp42" + b"v" * (64 * 1024)

    class FakeResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_bytes(self, chunk_size):
            yield payload

    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(video, "_detail_video_url",
                        lambda aweme_id: ("https://example.test/video", {"external_id": aweme_id}))
    with pytest.raises(video.DouyinVideoError):
        video.download_f2_video_with_result(SimpleNamespace(external_id="96004"), tmp_path)
    assert not list(tmp_path.glob("douyin-video-*.mp4"))
    assert not list(tmp_path.glob(".staging-video-*"))


def test_mp4_container_requires_video_track_and_data(tmp_path):
    good = tmp_path / "good.mp4"
    good.write_bytes(real_mp4_bytes())
    video._validate_mp4_container(good)  # 真实 H.264 合成样本可被 AVFoundation 读取

    fake_structure = tmp_path / "fake.mp4"
    fake_structure.write_bytes(mp4_bytes())  # 只有容器骨架、无有效样本
    with pytest.raises(video.DouyinVideoError):
        video._validate_mp4_container(fake_structure)

    audio_only = tmp_path / "audio.mp4"
    audio_only.write_bytes(mp4_bytes(handler=b"soun"))
    with pytest.raises(video.DouyinVideoError, match="没有视频轨"):
        video._validate_mp4_container(audio_only)

    no_moov = tmp_path / "nomoov.mp4"
    no_moov.write_bytes(struct.pack(">I", 24) + b"ftyp" + b"isom" + b"\x00" * 12
                        + struct.pack(">I", 16) + b"mdat" + b"\x00" * 8)
    with pytest.raises(video.DouyinVideoError, match="缺少容器元数据"):
        video._validate_mp4_container(no_moov)

    truncated = tmp_path / "truncated.mp4"
    truncated.write_bytes(mp4_bytes()[:200])
    with pytest.raises(video.DouyinVideoError):
        video._validate_mp4_container(truncated)


# ---------- 5. 固定版 F2 视频选优 ----------

def _install_fake_f2(monkeypatch, detail, *, with_selection=True):
    """装入最小 f2 模块桩：detail 响应 + 固定版选优函数（与 Filter 同实现）。"""
    f2 = types.ModuleType("f2")
    f2.__path__ = []
    apps = types.ModuleType("f2.apps")
    apps.__path__ = []
    douyin = types.ModuleType("f2.apps.douyin")
    douyin.__path__ = []
    handler_mod = types.ModuleType("f2.apps.douyin.handler")

    class _Page:
        def __init__(self, raw):
            self._raw = raw

        def _to_raw(self):
            return self._raw

    class DouyinHandler:
        def __init__(self, kwargs):
            self.kwargs = kwargs

        async def fetch_one_video(self, aweme_id):
            return _Page({"status_code": 0, "aweme_detail": detail})

    handler_mod.DouyinHandler = DouyinHandler
    handler_mod.rich_console = SimpleNamespace(quiet=False)

    # 最小 crawler 桩：供详情期间替换为禁止跟随重定向的子类（本测试无网络）
    crawler_mod = types.ModuleType("f2.apps.douyin.crawler")

    class DouyinCrawler:
        headers: dict = {}

        def __init__(self, kwargs=None):
            self.kwargs = kwargs or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    crawler_mod.DouyinCrawler = DouyinCrawler
    handler_mod.DouyinCrawler = DouyinCrawler
    douyin.crawler = crawler_mod

    utils_mod = types.ModuleType("f2.apps.douyin.utils")
    if with_selection:
        def get_video_play_urls(video_data):
            rows = video_data.get("bit_rate") if isinstance(video_data.get("bit_rate"), list) else []
            best, best_key = [], None
            for row in rows:
                if not isinstance(row, dict):
                    continue
                addr = row.get("play_addr") if isinstance(row.get("play_addr"), dict) else {}
                urls = [u for u in addr.get("url_list") or [] if isinstance(u, str)]
                if not urls:
                    continue
                key = (int(addr.get("width") or 0) * int(addr.get("height") or 0),
                       int(row.get("bit_rate") or 0))
                if best_key is None or key > best_key:
                    best_key, best = key, urls
            if best:
                return best
            addr = video_data.get("play_addr") if isinstance(video_data.get("play_addr"), dict) else {}
            return [u for u in addr.get("url_list") or [] if isinstance(u, str)]

        utils_mod.get_video_play_urls = get_video_play_urls

    douyin.handler = handler_mod
    douyin.utils = utils_mod
    apps.douyin = douyin
    f2.apps = apps
    for name, module in (("f2", f2), ("f2.apps", apps),
                         ("f2.apps.douyin", douyin),
                         ("f2.apps.douyin.handler", handler_mod),
                         ("f2.apps.douyin.crawler", crawler_mod),
                         ("f2.apps.douyin.utils", utils_mod)):
        monkeypatch.setitem(sys.modules, name, module)


def _detail_args(tmp_path, aweme_id):
    cookie = tmp_path / "cookie.txt"
    cookie.write_text("sessionid=synthetic", encoding="utf-8")
    return SimpleNamespace(cookie_file=str(cookie), aweme_id=aweme_id,
                           timeout=0, max_retries=1, budget_seconds=10)


def _two_tier_detail():
    return {
        "aweme_id": "9600000000000000005",
        "author": {"nickname": "synthetic"},
        "video": {
            "bit_rate": [
                {"bit_rate": 1000000, "play_addr": {"width": 1920, "height": 1080,
                    "url_list": ["https://example.test/high.mp4"]}},
                {"bit_rate": 300000, "play_addr": {"width": 640, "height": 360,
                    "url_list": ["https://example.test/low.mp4"]}},
            ]
        }
    }


def test_detail_video_uses_pinned_f2_selection_with_bit_rate_only(tmp_path, monkeypatch):
    detail = _two_tier_detail()
    _install_fake_f2(monkeypatch, detail)
    result = f2_fetch.run_detail(_detail_args(tmp_path, detail["aweme_id"]))
    assert result["detail"]["video_url"] == "https://example.test/high.mp4"
    assert result["detail"]["video_urls"] == ["https://example.test/high.mp4"]


def test_detail_video_uses_pinned_f2_selection_with_play_addr_fallback(tmp_path, monkeypatch):
    detail = _two_tier_detail()
    detail["video"]["play_addr"] = {"url_list": ["https://example.test/fallback.mp4"]}
    _install_fake_f2(monkeypatch, detail)
    result = f2_fetch.run_detail(_detail_args(tmp_path, detail["aweme_id"]))
    assert result["detail"]["video_url"] == "https://example.test/high.mp4"
    assert result["detail"]["video_urls"] == ["https://example.test/high.mp4"]


def test_detail_video_selection_falls_back_locally_without_f2_selector():
    """F2 选优函数不可用（运行时缺该工具）时按同一规则本地选优，不退回裸 play_addr。"""
    detail = _two_tier_detail()
    detail["video"]["play_addr"] = {"url_list": ["https://example.test/fallback.mp4"]}
    assert f2_fetch._select_video_url(detail["video"]) == ["https://example.test/high.mp4"]


def test_detail_video_selection_skips_non_http_candidates():
    video_data = {"play_addr": {"url_list": ["file:///tmp/x.mp4", "https://example.test/ok.mp4"]}}
    assert f2_fetch._select_video_url(video_data) == ["https://example.test/ok.mp4"]
    assert f2_fetch._select_video_url({}) == []


# ---------- 6. 手动链接分析错误信息 ----------

@pytest.mark.parametrize("exc_kind", ["offline", "unexpected"])
def test_link_analysis_does_not_persist_or_log_sensitive_synthetic_error(
        db, monkeypatch, caplog, exc_kind):
    value = "SYNTHETIC_OPAQUE_PRIVATE_VALUE_ABC"
    work = Work(platform="douyin", external_id="96969696" + exc_kind, title="synthetic",
                url="https://example.test/work", source_type="link_analysis",
                content_type="video", status="NEW")
    db.add(work)
    db.commit()

    def fail(db_, work_, **kwargs):
        work_.status = "TRANSCRIBING"
        error = "输入文件不存在: /Users/synthetic/" + value
        if exc_kind == "offline":
            raise jobs.PipelineOfflineError(error)
        raise RuntimeError(error)

    monkeypatch.setattr(jobs, "_process_work", fail)
    with caplog.at_level("WARNING"):
        result = jobs.process_link_analysis(work.id)
    assert result["status"] == "FAILED"
    db.expire_all()
    saved = db.get(Work, work.id)
    tasks = db.query(Task).filter_by(work_id=work.id).all()
    assert value not in (saved.last_error or "")
    assert all(value not in (task.error or "") for task in tasks)
    assert "/Users/synthetic" not in (saved.last_error or "")
    assert value not in caplog.text
    assert "/Users/synthetic" not in caplog.text


def test_link_analysis_keeps_known_safe_message(db, monkeypatch):
    """已知安全文案（输入文件不存在固定文案）仍对用户可读。"""
    work = Work(platform="douyin", external_id="96960001", title="synthetic",
                url="https://example.test/work", source_type="link_analysis",
                content_type="video", status="NEW")
    db.add(work)
    db.commit()

    def fail(db_, work_, **kwargs):
        work_.status = "TRANSCRIBING"
        raise jobs.PipelineOfflineError("转写输入文件不存在（可能已被清理或下载不完整）")

    monkeypatch.setattr(jobs, "_process_work", fail)
    jobs.process_link_analysis(work.id)
    db.expire_all()
    assert db.get(Work, work.id).last_error == "转写输入文件不存在（可能已被清理或下载不完整）"
