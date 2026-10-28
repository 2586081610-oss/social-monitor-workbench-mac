"""The Mac fallback must recover only failed Douyin downloads without clobbering media."""
from pathlib import Path
import pytest

from monitor.pipeline import download


@pytest.mark.parametrize('failure', ['empty', 'error'])
def test_fallback_recovers_douyin_without_changing_existing_file(monkeypatch, tmp_path, failure):
    monkeypatch.setattr(download.sys, 'platform', 'darwin')
    old = tmp_path / 'old.mp4'
    old.write_bytes(b'old-media')
    def original(platform, url, directory):
        if failure == 'error':
            raise download.PipelineOfflineError('downloader failed')
    def browser(url, directory):
        (directory / 'new.mp4').write_bytes(b'new-video-and-audio')
    monkeypatch.setattr(download, '_invoke_downloader', original)
    monkeypatch.setattr(download, '_download_douyin_browser', browser, raising=False)
    result = download.download_with_result('douyin', 'https://www.douyin.com/video/12345678', tmp_path)
    assert result.disposition == 'downloaded' and result.completed_at
    assert [f.path.name for f in result.files] == ['new.mp4']
    assert old.read_bytes() == b'old-media'


def test_successful_original_never_invokes_browser(monkeypatch, tmp_path):
    monkeypatch.setattr(download.sys, 'platform', 'darwin')
    monkeypatch.setattr(download, '_invoke_downloader', lambda platform, url, directory: (directory / 'one.mp4').write_bytes(b'original'))
    def browser(*args):
        pytest.fail('successful existing downloader must remain primary')
    monkeypatch.setattr(download, '_download_douyin_browser', browser, raising=False)
    assert download.download_with_result('douyin', 'https://www.douyin.com/video/12345678', tmp_path).files[0].path.name == 'one.mp4'


@pytest.mark.parametrize('platform,operating_system', [('bilibili', 'darwin'), ('douyin', 'win32')])
def test_no_fallback_for_other_platform_or_windows(monkeypatch, tmp_path, platform, operating_system):
    monkeypatch.setattr(download.sys, 'platform', operating_system)
    monkeypatch.setattr(download, '_invoke_downloader', lambda *args: None)
    monkeypatch.setattr(download, '_download_douyin_browser', lambda *args: pytest.fail('unexpected fallback'), raising=False)
    with pytest.raises(download.PipelineOfflineError):
        download.download_with_result(platform, 'https://www.douyin.com/video/12345678', tmp_path)


def test_failed_fallback_never_creates_success_receipt(monkeypatch, tmp_path):
    monkeypatch.setattr(download.sys, 'platform', 'darwin')
    monkeypatch.setattr(download, '_invoke_downloader', lambda *args: None)
    monkeypatch.setattr(download, '_download_douyin_browser', lambda *args: (_ for _ in ()).throw(download.PipelineOfflineError('blocked')), raising=False)
    with pytest.raises(download.PipelineOfflineError):
        download.download_with_result('douyin', 'https://www.douyin.com/video/12345678', tmp_path)


def test_select_tracks_requires_matching_work_and_official_cdn():
    from monitor.pipeline.douyin_browser import select_tracks
    video_url = 'https://v11-weba.douyinvod.com/sig/ttl/video/tos/cn/object/videoid/media-video-avc1/?token=one'
    audio_url = 'https://v11-weba.douyinvod.com/sig/ttl/video/tos/cn/object/audioid/media-audio-und-mp4a/?token=two'
    detail = {'aweme_id': '12345678', 'video': {'sources': [video_url, audio_url]}}
    # Same file identity with refreshed signing prefix and query is allowed.
    candidates = [('video', video_url.replace('/sig/ttl/', '/other/sign/'), 100),
                  ('audio', audio_url, 50)]
    assert set(select_tracks('12345678', detail, candidates)) == {'audio', 'video'}
    assert select_tracks('99999999', detail, candidates) == {}
    evil = video_url.replace('v11-weba.douyinvod.com', 'evil.example')
    assert select_tracks('12345678', detail, [('video', evil, 100), ('audio', audio_url, 50)]) == {}
    assert select_tracks('12345678', detail, [('video', video_url.replace('videoid', 'other'), 100), ('audio', audio_url, 50)]) == {}


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'https://douyin.com.evil.test/video/12345678',
                                 'https://evil.test/video/12345678', 'https://u@www.douyin.com/video/12345678',
                                 'http://www.douyin.com/video/12345678'])
def test_reject_non_douyin_entry_urls(url):
    from monitor.pipeline.douyin_browser import valid_entry_url
    assert not valid_entry_url(url)


def test_failed_primary_partial_file_never_becomes_download_receipt(monkeypatch, tmp_path):
    monkeypatch.setattr(download.sys, 'platform', 'darwin')
    def primary(platform, url, directory):
        (directory / 'partial.mp4').write_bytes(b'incomplete')
        raise download.PipelineOfflineError('downloader failed')
    def browser(url, directory):
        (directory / 'complete.mp4').write_bytes(b'complete')
    monkeypatch.setattr(download, '_invoke_downloader', primary)
    monkeypatch.setattr(download, '_download_douyin_browser', browser)
    receipt = download.download_with_result('douyin', 'https://www.douyin.com/video/12345678', tmp_path)
    assert [item.path.name for item in receipt.files] == ['complete.mp4']


def test_browser_process_timeout_cleans_only_its_private_scratch(monkeypatch, tmp_path):
    import subprocess
    untouched = tmp_path / 'keep.txt'
    untouched.write_text('keep')
    def timed_out(command, **kwargs):
        scratch = Path(command[-1])
        (scratch / 'unfinished.mp4').write_bytes(b'partial')
        raise subprocess.TimeoutExpired(command, 1)
    monkeypatch.setattr(download.subprocess, 'run', timed_out)
    with pytest.raises(download.PipelineOfflineError):
        download._download_douyin_browser('https://www.douyin.com/video/12345678', tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ['keep.txt']


def test_personal_source_sync_disables_browser_fallback(monkeypatch, tmp_path):
    """个人来源（F2/DDR）自动下载不得打开或播放作品网页兜底：失败如实报错。"""
    monkeypatch.setattr(download.sys, 'platform', 'darwin')
    monkeypatch.setattr(download, '_invoke_downloader', lambda *args: None)  # 主下载器无输出
    monkeypatch.setattr(
        download, '_download_douyin_browser',
        lambda *args: pytest.fail('personal-source works must not use the browser fallback'),
        raising=False,
    )
    with pytest.raises(download.PipelineOfflineError):
        download.download_with_result(
            'douyin', 'https://www.douyin.com/video/12345678', tmp_path,
            allow_browser_fallback=False,
        )

