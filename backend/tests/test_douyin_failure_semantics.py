"""Transport/platform failures must not become successful empty creator polls."""
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base
from app.models import CookieHealth, Creator, PollLog, Work
from monitor.platforms.douyin_mediacrawler import DouyinMediaCrawlerAdapter


@pytest.mark.parametrize('reply', [
    subprocess.TimeoutExpired('fixture', 150),
    subprocess.CompletedProcess([], 1, '', 'private upstream details'),
    subprocess.CompletedProcess([], 0, '', ''),
    subprocess.CompletedProcess([], 0, 'not json', ''),
    subprocess.CompletedProcess([], 0, '{"error":"private upstream details"}', ''),
    subprocess.CompletedProcess([], 0, '{}', ''),
    subprocess.CompletedProcess([], 0, '[null]', ''),
])
def test_adapter_failure_raises_instead_of_empty(monkeypatch, tmp_path, reply):
    adapter = DouyinMediaCrawlerAdapter(tmp_path)
    monkeypatch.setattr(adapter, '_venv_python', lambda: Path('fixture-python'))
    def run(*args, **kwargs):
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(RuntimeError) as caught:
        adapter.poll_creator('fixture-id')
    assert 'private upstream details' not in str(caught.value)


def test_genuine_empty_list_succeeds(monkeypatch, tmp_path):
    adapter = DouyinMediaCrawlerAdapter(tmp_path)
    monkeypatch.setattr(adapter, '_venv_python', lambda: Path('fixture-python'))
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess([], 0, '[]', ''))
    assert adapter.poll_creator('fixture-id') == []


@pytest.mark.parametrize('mixed', [False, True])
def test_poll_records_failure_and_preserves_existing_work(monkeypatch, mixed):
    from monitor import jobs
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        db.add(Creator(platform='douyin', name='fixture', external_id='fixture-id', profile_url='https://example.invalid'))
        if mixed:
            db.add(Creator(platform='douyin', name='good', external_id='good-id', profile_url='https://example.invalid'))
        db.add(Work(platform='douyin', external_id='old-work', title='keep', url='https://example.invalid', status='NEW'))
        db.commit()
    monkeypatch.setattr(jobs, 'SessionLocal', sessions)
    monkeypatch.setattr(DouyinMediaCrawlerAdapter, 'available', lambda self: True)
    monkeypatch.setattr(DouyinMediaCrawlerAdapter, '_venv_python', lambda self: Path('fixture-python'))
    monkeypatch.setattr(subprocess, 'run', lambda args, **kw: (
        subprocess.CompletedProcess([], 0, '[]', '') if args[2] == 'good-id'
        else subprocess.CompletedProcess([], 1, '', '')))
    jobs.poll_douyin()
    with sessions() as db:
        logs = db.query(PollLog).all()
        assert logs and all(log.status != 'ok' for log in logs)
        assert db.query(CookieHealth).filter_by(platform='douyin').one().status == 'FAIL'
        assert [(work.external_id, work.title, work.status) for work in db.query(Work)] == [('old-work', 'keep', 'NEW')]
        assert db.query(Creator).filter_by(external_id='fixture-id').one().last_poll_at is None
    engine.dispose()


def _load_fetch(monkeypatch, tmp_path):
    monkeypatch.setenv('MEDIACRAWLER_DIR', str(tmp_path))
    monkeypatch.chdir(tmp_path)
    path = Path(__file__).resolve().parents[2] / 'monitor/platforms/dy_mediacrawler_fetch.py'
    spec = importlib.util.spec_from_file_location('fixture_fetch', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('payload', [None, {}, {'status_code': 8, 'aweme_list': []},
                                      {'status_code': 0}, {'status_code': 0, 'aweme_list': None}])
def test_upstream_error_not_normalized_to_empty(monkeypatch, tmp_path, payload):
    fetch = _load_fetch(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError):
        fetch._validated_aweme_list(payload)


def test_upstream_valid_empty(monkeypatch, tmp_path):
    fetch = _load_fetch(monkeypatch, tmp_path)
    assert fetch._validated_aweme_list({'status_code': 0, 'aweme_list': []}) == []
