"""CLI previews must never create/migrate the configured database or data roots."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('existing', ['missing', 'legacy', 'current'])
def test_dry_run_preserves_database_and_roots(tmp_path, existing):
    target = tmp_path / 'target'
    target.mkdir()
    database = target / 'preview.db'
    if existing == 'legacy':
        with sqlite3.connect(database) as conn:
            conn.execute('CREATE TABLE sentinel (value TEXT)')
            conn.execute("INSERT INTO sentinel VALUES ('keep')")
    fixture = tmp_path / 'ddr'
    fixture.mkdir()
    (fixture / 'records.json').write_text(json.dumps({
        'schemaVersion': 2, 'records': {'liked_videos': [{
            'id': 'liked_videos:900001', 'videoId': '900001',
            'url': 'https://www.douyin.com/video/900001', 'title': 'fixture',
        }], 'favorite_videos': [], 'watch_history': []},
    }))
    env = dict(os.environ, DATABASE_PATH=str(database), MEDIA_ROOT=str(target / 'media'),
               OBSIDIAN_ROOT=str(target / 'notes'), WORKBUDDY_DIR=str(target / 'workbuddy'),
               ARCHIVE_ROOTS=json.dumps({'fitness': str(target / 'fitness')}))
    command = [sys.executable, '-m', 'monitor.personal_source_ingest',
               '--source', 'file', '--base-dir', str(fixture)]
    if existing == 'current':
        initial = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True)
        assert initial.returncode == 0, initial.stderr + initial.stdout
    before = {str(p.relative_to(target)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
              for p in target.rglob('*') if p.is_file()}
    before_dirs = sorted(str(p.relative_to(target)) for p in target.rglob('*') if p.is_dir())
    result = subprocess.run([sys.executable, '-m', 'monitor.personal_source_ingest',
                             '--source', 'file', '--base-dir', str(fixture), '--dry-run'],
                            cwd=ROOT, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report['dry_run'] is True
    assert report['works_created'] == (0 if existing == 'current' else 1)
    if existing == 'current':
        assert report['marks_updated'] == 1
    after = {str(p.relative_to(target)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
             for p in target.rglob('*') if p.is_file()}
    assert after == before
    assert sorted(str(p.relative_to(target)) for p in target.rglob('*') if p.is_dir()) == before_dirs


def test_live_wal_preview_refuses_without_touching_source(tmp_path):
    database = tmp_path / 'live.db'
    fixture = tmp_path / 'ddr'
    fixture.mkdir()
    (fixture / 'records.json').write_text(json.dumps({
        'schemaVersion': 2, 'records': {'liked_videos': [], 'favorite_videos': []}}))
    connection = sqlite3.connect(database)
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE sentinel (value TEXT)')
        connection.commit()
        before = {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                  for p in tmp_path.iterdir() if p.is_file()}
        result = subprocess.run([sys.executable, '-m', 'monitor.personal_source_ingest',
                                 '--source', 'file', '--base-dir', str(fixture), '--dry-run'],
                                cwd=ROOT, env=dict(os.environ, DATABASE_PATH=str(database)),
                                text=True, capture_output=True)
        assert result.returncode == 1
        assert json.loads(result.stdout)['kind'] == 'database_busy'
        after = {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                 for p in tmp_path.iterdir() if p.is_file()}
        assert before == after
    finally:
        connection.close()
