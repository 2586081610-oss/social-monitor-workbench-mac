"""Download one publicly playable Douyin video using an isolated browser context.

Only media URLs tied to the selected work's detail response are accepted. The
browser has a fresh profile and does not access a user's login credentials.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

_MAX_TRACK_BYTES = 250_000_000
_ALLOWED_ENTRY_HOSTS = {'www.douyin.com', 'douyin.com', 'v.douyin.com', 'v.iesdouyin.com'}


def valid_entry_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return (p.scheme == 'https' and p.hostname in _ALLOWED_ENTRY_HOSTS
                and p.username is None and p.password is None and p.port in (None, 443))
    except (ValueError, TypeError):
        return False


def _cdn_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return (p.scheme == 'https' and bool(p.hostname)
                and p.hostname.endswith('.douyinvod.com')
                and p.username is None and p.password is None and p.port in (None, 443))
    except (ValueError, TypeError):
        return False


def _file_identity(url: str) -> str:
    return '/'.join(urlparse(url).path.strip('/').split('/')[-4:])


def _urls(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _urls(child)
    elif isinstance(value, list):
        for child in value:
            yield from _urls(child)
    elif isinstance(value, str) and _cdn_url(value):
        yield value


def select_tracks(work_id: str, detail: dict, candidates: list[tuple[str, str, int]]) -> dict:
    if not isinstance(detail, dict) or str(detail.get('aweme_id')) != work_id:
        return {}
    declared = {_file_identity(url) for url in _urls(detail.get('video', {}))}
    selected = {}
    for kind, url, size in candidates:
        if kind not in {'audio', 'video'} or not _cdn_url(url):
            continue
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= _MAX_TRACK_BYTES:
            continue
        if _file_identity(url) in declared:
            selected.setdefault(kind, (url, size))
    return selected if set(selected) == {'audio', 'video'} else {}


def download_public_video(url: str, output_dir: Path) -> None:
    if not valid_entry_url(url):
        raise RuntimeError('invalid_douyin_url')
    from playwright.sync_api import sync_playwright

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.douyin-browser-', dir=output_dir) as scratch:
        scratch_path = Path(scratch)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-proxy-server'])
            context = browser.new_context()
            page = context.new_page()
            details: dict[str, dict] = {}
            candidates: list[tuple[str, str, int]] = []

            def allow_navigation(route):
                request = route.request
                if request.is_navigation_request() and not valid_entry_url(request.url):
                    route.abort()
                else:
                    route.continue_()

            def observe(response):
                try:
                    parsed = urlparse(response.url)
                    if parsed.hostname in {'www.douyin.com', 'douyin.com'} and parsed.path == '/aweme/v1/web/aweme/detail/' and response.status == 200:
                        data = response.json()
                        item = data.get('aweme_detail') if isinstance(data, dict) else None
                        if isinstance(item, dict) and item.get('aweme_id'):
                            details[str(item['aweme_id'])] = item
                    elif _cdn_url(response.url) and response.status == 206:
                        kind = 'audio' if '/media-audio-' in parsed.path else 'video' if '/media-video-' in parsed.path else None
                        match = re.fullmatch(r'bytes \d+-\d+/(\d+)', response.headers.get('content-range', ''))
                        if kind and match:
                            candidates.append((kind, response.url, int(match.group(1))))
                except Exception:
                    # Invalid upstream responses cannot be used as evidence.
                    return

            context.route('**/*', allow_navigation)
            page.on('response', observe)
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=35000)
                parsed_page = urlparse(page.url)
                match = re.fullmatch(r'/video/(\d+)', parsed_page.path.rstrip('/'))
                if not valid_entry_url(page.url) or not match:
                    raise RuntimeError('unsupported_douyin_video')
                work_id = match.group(1)
                tracks = {}
                for _ in range(60):
                    tracks = select_tracks(work_id, details.get(work_id, {}), candidates)
                    if tracks:
                        break
                    page.wait_for_timeout(500)
                if not tracks:
                    raise RuntimeError('public_video_streams_unavailable')
                for kind, (media_url, size) in tracks.items():
                    response = context.request.get(
                        media_url,
                        headers={'Range': f'bytes=0-{size-1}', 'Referer': 'https://www.douyin.com/'},
                        timeout=60000,
                        max_redirects=0,
                    )
                    if response.status not in (200, 206):
                        raise RuntimeError('media_request_failed')
                    if response.status == 206 and response.headers.get('content-range') != f'bytes 0-{size-1}/{size}':
                        raise RuntimeError('media_range_mismatch')
                    payload = response.body()
                    if len(payload) != size:
                        raise RuntimeError('media_incomplete')
                    (scratch_path / f'{kind}.mp4').write_bytes(payload)
                    del payload
            finally:
                context.close()
                browser.close()

        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        merged = scratch_path / 'merged.mp4'
        result = subprocess.run(
            [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(scratch_path / 'video.mp4'),
             '-i', str(scratch_path / 'audio.mp4'), '-c', 'copy', '-movflags', '+faststart', str(merged)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90, check=False,
        )
        if result.returncode or not merged.is_file() or merged.stat().st_size <= 0:
            raise RuntimeError('media_merge_failed')
        verify = subprocess.run(
            [ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', str(merged), '-map', '0:v:0',
             '-map', '0:a:0', '-frames:v', '1', '-frames:a', '1', '-f', 'null', '-'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False,
        )
        if verify.returncode:
            raise RuntimeError('media_verification_failed')
        os.replace(merged, output_dir / f'douyin-{work_id}-{uuid.uuid4().hex[:12]}.mp4')


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    try:
        download_public_video(sys.argv[1], Path(sys.argv[2]))
    except Exception as exc:
        # Never emit signed media URLs, cookies, paths or upstream response bodies.
        print(json.dumps({'status': 'failed', 'kind': type(exc).__name__}))
        return 1
    print(json.dumps({'status': 'ok'}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
