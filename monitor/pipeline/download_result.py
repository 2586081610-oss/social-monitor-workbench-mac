"""Verified original-media receipts shared by download and archive callers.

Adapters determine whether bytes were newly written or registered files reused.
The receipt enforces the corresponding time and file-list invariants; constructing
one alone is not proof that an adapter successfully downloaded a work.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal


DownloadDisposition = Literal["downloaded", "reused", "not_required"]


@dataclass(frozen=True)
class MediaFile:
    path: Path
    size_bytes: int
    media_type: str | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes <= 0:
            raise ValueError("original media must contain bytes")
        if self.media_type is not None and not isinstance(self.media_type, str):
            raise TypeError("media_type must be a string or None")
        if self.sha256 is not None and not isinstance(self.sha256, str):
            raise TypeError("sha256 must be a string or None")


@dataclass(frozen=True)
class DownloadResult:
    disposition: DownloadDisposition
    files: tuple[MediaFile, ...]
    completed_at: str | None = None
    # 实际落盘视频分辨率（如 "1920x1080"）；ffprobe/ffmpeg 探测失败或非视频为 None。
    # 用于发现"选了高清档却被平台降档"（yutto -q 有静默降档兜底）。
    video_resolution: str | None = None

    def __post_init__(self) -> None:
        if self.disposition not in {"downloaded", "reused", "not_required"}:
            raise ValueError("unsupported download disposition")
        files = tuple(self.files)
        if any(not isinstance(item, MediaFile) for item in files):
            raise TypeError("download result files must be MediaFile values")
        object.__setattr__(self, "files", files)
        if self.disposition == "not_required":
            if self.files or self.completed_at is not None:
                raise ValueError("text-only works cannot carry a media download receipt")
            return
        if not self.files:
            raise ValueError("downloaded or reused media requires verified files")
        if self.disposition == "reused":
            if self.completed_at is not None:
                raise ValueError("registered media reuse cannot renew download time")
            return
        if not isinstance(self.completed_at, str):
            raise ValueError("new downloads require a completion timestamp")
        if self.video_resolution is not None and not isinstance(self.video_resolution, str):
            raise TypeError("video_resolution must be a string or None")
        completed = datetime.fromisoformat(self.completed_at.replace("Z", "+00:00"))
        if completed.tzinfo is None or completed.utcoffset() is None:
            raise ValueError("download completion time must specify a timezone")
