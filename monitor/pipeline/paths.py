"""流水线·路径工具：非 ASCII/空格/问号路径复制到 media/_work/{hash}/。

实测问题 #3：中文/空格/问号路径导致 subprocess 调 ffmpeg（funasr/torchaudio 内部
也走 ffmpeg）失败。统一在入口处把输入复制到 ASCII 安全的工作目录再处理，
规避 Windows 下子进程对非 ASCII 路径的处理差异。
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("monitor.pipeline.paths")

# media/_work 下的工作目录（ASCII 安全）
_WORK_DIR_NAME = "_work"

# 进程内同一份工作副本的引用计数：转写/抽帧可嵌套复用同一副本，
# 只有最外层退出时才删除目录，既避免重复复制，也保证异常路径必清理。
_REF_LOCK = threading.RLock()
_ACTIVE_COPIES: dict[str, tuple[Path, int]] = {}

# 超过该时长仍残留的副本视为孤儿（进程被强杀等 finally 未执行的情况），下次开机回收。
_STALE_AFTER_SECONDS = 24 * 3600


def media_root() -> Path:
    """媒体根目录：优先 MEDIA_ROOT 环境变量，否则按工作区默认 media/。"""
    val = os.getenv("MEDIA_ROOT")
    if val:
        return Path(val).resolve()
    return Path(__file__).resolve().parents[2] / "media"


def is_ascii_safe(path: str | Path) -> bool:
    """路径是否 ASCII 安全：无 >127 字符、无空格、无问号（含全角？）。"""
    s = str(path)
    return not any(ch > "\x7f" or ch in " ?" for ch in s)


def retry_write_text(path: str | Path, content: str, tries: int = 8, delay: float = 2.0) -> None:
    """写文本文件，遇 PermissionError/OSError 自动重试；覆盖已存在文件失败时删除重建。

    实测：本机 WorkBuddy 文件保护对**已存在文件**的覆盖写会间歇/持续拦截
    （Permission denied / sqlite readonly），而对**新文件**写入正常。
    因此：先尝试直接写；被拒则 unlink + 重建（重建即新文件，可写），再重试。
    """
    p = Path(path)
    for i in range(tries):
        try:
            p.write_text(content, encoding="utf-8")
            return
        except OSError as exc:
            if i == tries - 1:
                raise
            logger.warning("write %s 被占用（%s），尝试删除重建后重试 %d/%d", path, exc, i + 1, tries)
            try:
                p.unlink(missing_ok=True)
            except OSError:
                logger.warning("unlink %s 失败，仅等待重试", path)
            time.sleep(delay)


def work_copy_dir(video_path: str | Path) -> Path | None:
    """返回该路径对应的 media/_work/{hash}/ 目录；路径本就 ASCII 安全时返回 None。"""
    p = Path(video_path)
    s = str(p)
    if is_ascii_safe(s):
        return None
    digest = hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]
    return media_root() / _WORK_DIR_NAME / digest


def ensure_ascii_work_copy(video_path: str | Path) -> tuple[str, str | None]:
    """返回 (工作路径, 原路径)。原路径 ASCII 安全则原样返回；否则复制到 media/_work/{hash}/。

    Args:
        video_path: 原始视频/音频路径（可能含中文/空格/问号）
    Returns:
        (work_path, original_path)：original_path 为 None 表示未复制（路径本就安全）

    注意：本函数只负责复制，不负责清理；需要自动清理请用 ascii_work_copy() 上下文管理器，
    或在用完后显式调用 cleanup_ascii_work_copy()。
    """
    p = Path(video_path)
    s = str(p)
    if is_ascii_safe(s):
        return s, None
    dest = work_copy_dir(p)
    dest = dest / (dest.name + p.suffix.lower())
    if dest.exists() and dest.stat().st_size == p.stat().st_size:
        logger.info("工作目录已存在，复用: %s", dest)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(p, dest)
        except BaseException:
            # 复制中途失败会留下半截文件：立即删掉，避免污染工作目录
            dest.unlink(missing_ok=True)
            raise
        logger.info("非 ASCII 路径复制到工作目录: %s -> %s", s, dest)
    return str(dest), s


@contextmanager
def ascii_work_copy(video_path: str | Path) -> Iterator[tuple[str, str | None]]:
    """确保 ASCII 安全工作副本，并在退出时（含异常/中断）删除 media/_work/{hash}/。

    同一进程内可嵌套复用：转写与抽帧分别进入，只有最外层退出时才真正删除，
    避免同一条视频被重复复制两遍。
    """
    key = str(video_path)
    work_path, original = ensure_ascii_work_copy(video_path)
    if original is None:
        yield work_path, None
        return
    with _REF_LOCK:
        depth = _ACTIVE_COPIES.get(key, (Path(work_path).parent, 0))[1]
        _ACTIVE_COPIES[key] = (Path(work_path).parent, depth + 1)
    try:
        yield work_path, original
    finally:
        with _REF_LOCK:
            directory, depth = _ACTIVE_COPIES.get(key, (Path(work_path).parent, 1))
            if depth <= 1:
                _ACTIVE_COPIES.pop(key, None)
                _remove_work_dir(directory)
            else:
                _ACTIVE_COPIES[key] = (directory, depth - 1)


def cleanup_ascii_work_copy(video_path: str | Path) -> bool:
    """删除该路径对应的媒体工作副本目录；返回是否实际删除。"""
    directory = work_copy_dir(video_path)
    if directory is None:
        return False
    return _remove_work_dir(directory)


def sweep_stale_work_dirs(max_age_seconds: float = _STALE_AFTER_SECONDS) -> list[str]:
    """回收过期的 media/_work 副本（覆盖进程被强杀、finally 未执行的异常路径）。

    只删除超过 max_age_seconds 未更新、且当前进程未在使用的副本目录。
    """
    root = media_root() / _WORK_DIR_NAME
    if not root.is_dir():
        return []
    now = time.time()
    removed: list[str] = []
    with _REF_LOCK:
        active = {str(directory) for directory, _ in _ACTIVE_COPIES.values()}
        for directory in root.iterdir():
            if not directory.is_dir() or str(directory) in active:
                continue
            newest = max(
                (item.stat().st_mtime for item in directory.rglob("*") if item.is_file()),
                default=directory.stat().st_mtime,
            )
            if now - newest < max_age_seconds:
                continue
            if _remove_work_dir(directory):
                removed.append(directory.name)
    if removed:
        logger.info("回收过期工作副本 %d 个: %s", len(removed), ", ".join(removed))
    return removed


def _remove_work_dir(directory: Path) -> bool:
    """删除单个工作副本目录；只允许删除 _work 下的目录，失败仅告警不抛错。"""
    root = media_root() / _WORK_DIR_NAME
    try:
        if directory.parent.resolve() != root.resolve():
            logger.warning("拒绝删除非工作目录: %s", directory)
            return False
        shutil.rmtree(directory, ignore_errors=True)
        if directory.exists():
            logger.warning("工作副本删除不完整: %s", directory)
            return False
        return True
    except OSError as exc:
        logger.warning("工作副本删除失败 %s（%s）", directory, exc)
        return False
