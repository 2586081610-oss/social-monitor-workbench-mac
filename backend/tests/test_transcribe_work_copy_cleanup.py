"""转写临时工作副本清理：成功、失败、中断路径都不得在 media/_work 留下副本。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

from monitor.pipeline import paths, transcribe  # noqa: E402


def _non_ascii_video(tmp_path: Path) -> Path:
    """中文+空格路径：必然触发 media/_work 复制分支。"""
    video = tmp_path / "中文 测试.mp4"
    video.write_bytes(b"fake-video-bytes")
    return video


def test_transcribe_engine_failure_still_cleans_work_copy(tmp_path: Path, monkeypatch) -> None:
    """转写引擎抛错时，工作副本必须被清理，且出错瞬间副本确实存在过。"""
    video = _non_ascii_video(tmp_path)
    work_dir = paths.work_copy_dir(video)
    assert work_dir is not None
    seen: list[bool] = []

    def boom():
        seen.append(work_dir.exists())
        raise RuntimeError("engine load failed")

    monkeypatch.setattr(transcribe, "_get_engine", boom)

    with pytest.raises(RuntimeError):
        transcribe.transcribe(str(video), tmp_path / "transcript")

    assert seen == [True], "转写前应已创建工作副本"
    assert not work_dir.exists(), "转写失败后工作副本必须清理"


def test_transcribe_interrupt_still_cleans_work_copy(tmp_path: Path, monkeypatch) -> None:
    """中断（KeyboardInterrupt）同样走 finally，副本不得残留。"""
    video = _non_ascii_video(tmp_path)
    work_dir = paths.work_copy_dir(video)
    assert work_dir is not None

    def interrupted(**_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(transcribe, "_get_engine", lambda: SimpleNamespace(generate=interrupted))

    with pytest.raises(KeyboardInterrupt):
        transcribe.transcribe(str(video), tmp_path / "transcript")

    assert not work_dir.exists(), "中断路径也必须清理工作副本"


def test_transcribe_success_cleans_work_copy(tmp_path: Path, monkeypatch) -> None:
    """成功路径同样不留副本，转写稿正常落盘。"""
    video = _non_ascii_video(tmp_path)
    work_dir = paths.work_copy_dir(video)
    assert work_dir is not None

    class Engine:
        def generate(self, **kwargs):
            return [{"text": "测试文本", "timestamp": [[0, 1000]]}]

    monkeypatch.setattr(transcribe, "_get_engine", lambda: Engine())

    out = Path(transcribe.transcribe(str(video), tmp_path / "transcript"))

    assert out.exists(), "转写稿应正常落盘"
    assert not work_dir.exists(), "转写完成后工作副本必须清理"


def test_nested_work_copy_shared_and_cleaned_once(tmp_path: Path) -> None:
    """转写与抽帧嵌套复用同一副本，最外层退出时才删除（避免重复复制两遍）。"""
    video = _non_ascii_video(tmp_path)
    work_dir = paths.work_copy_dir(video)
    assert work_dir is not None

    with paths.ascii_work_copy(str(video)) as (outer_path, _):
        assert Path(outer_path).exists()
        with paths.ascii_work_copy(str(video)) as (inner_path, _):
            assert inner_path == outer_path
            assert Path(inner_path).exists()
        assert Path(inner_path).exists(), "内层退出不应删除仍在使用的副本"
    assert not work_dir.exists(), "最外层退出后必须删除副本"


def test_stale_work_dir_is_reclaimed(tmp_path: Path, monkeypatch) -> None:
    """进程被强杀等导致 finally 未执行的孤儿副本，由过期回收兜底清理。"""
    video = _non_ascii_video(tmp_path)
    work_path, _ = paths.ensure_ascii_work_copy(video)
    work_dir = paths.work_copy_dir(video)
    assert work_dir is not None and work_dir.exists()

    reclaimed = paths.sweep_stale_work_dirs(max_age_seconds=0)
    assert work_dir.name in reclaimed
    assert not work_dir.exists()
    assert Path(work_path).exists() is False
