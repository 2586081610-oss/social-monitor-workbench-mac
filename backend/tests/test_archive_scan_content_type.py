"""本地归档扫描按实际文件判型（video / image）的回归测试。"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.services.archive_scan_service import (
    _content_type_for_media_files,
    scan_directory,
)

_DIRECTORY = "测试分组/测试博主/2024-01-01_抖音_测试作品"


@pytest.mark.parametrize(
    ("media_files", "expected"),
    [
        (["01.jpg", "02.png", "03.webp"], "image"),
        (["01.JPG", "02.HEIC", "03.BMP"], "image"),
        (["video.mp4"], "video"),
        (["video.MOV", "video.mkv", "video.webm"], "video"),
        (["video.mp4", "01.jpg", "cover.png"], "video"),
        ([], "video"),
        # 只有音频或未知扩展名时无法判型，保守回退 video。
        (["audio.mp3", "audio.m4a"], "video"),
        (["unknown.bin"], "video"),
    ],
)
def test_content_type_follows_actual_media_files(media_files, expected):
    assert _content_type_for_media_files(media_files) == expected


@pytest.mark.parametrize(
    ("media_files", "expected"),
    [
        (["01.jpg", "02.png"], "image"),
        (["video.mp4", "video.mov"], "video"),
        (["video.mp4", "01.jpg"], "video"),
    ],
)
def test_scan_directory_persists_detected_content_type(tmp_path, monkeypatch, media_files, expected):
    media_root = tmp_path / "media"
    target = media_root / _DIRECTORY
    target.mkdir(parents=True)
    for name in media_files:
        (target / name).write_bytes(b"archive-media")
    monkeypatch.setattr(settings, "archive_root_paths", {"media": media_root})

    result = scan_directory(
        "media",
        _DIRECTORY,
        [],
        existing=None,
        work_by_identity={},
        work_by_path={},
    )

    assert result.fields is not None
    assert result.fields["metadata_json"]["content_type"] == expected


def test_empty_media_directory_is_not_indexed(tmp_path, monkeypatch):
    media_root = tmp_path / "media"
    (media_root / _DIRECTORY).mkdir(parents=True)
    monkeypatch.setattr(settings, "archive_root_paths", {"media": media_root})

    result = scan_directory(
        "media",
        _DIRECTORY,
        [],
        existing=None,
        work_by_identity={},
        work_by_path={},
    )

    assert result.fields is None
    assert result.error_code == "media_directory_empty"
