"""Pending manifests must never turn creation time into a publication fact."""
import json
from types import SimpleNamespace

from app.services import note_service as module


def test_unknown_publication_not_filled_from_creation_date(monkeypatch, tmp_path):
    monkeypatch.setattr(module.settings, 'pending_dir', tmp_path)
    work = SimpleNamespace(id=71, platform='douyin', external_id='fixture', title='fixture',
                           creator=None, group=None, url='https://example.invalid',
                           published_at=None, created_at='2026-10-28T02:00:00Z',
                           media_path=None, transcript_path=None)
    result = module.write_pending_manifest(work)
    content = json.loads((tmp_path / result.split('/')[-1]).read_text())
    assert content['publish_date'] == 'unknown'
    # Existing note/manifest naming rules remain stable.
    assert '2026-10-28' in content['expected_output']


def test_known_publication_date_is_retained(monkeypatch, tmp_path):
    monkeypatch.setattr(module.settings, 'pending_dir', tmp_path)
    work = SimpleNamespace(id=72, platform='douyin', external_id='fixture', title='fixture',
                           creator=None, group=None, url='https://example.invalid',
                           published_at='2026-05-03T06:00:00Z', created_at='2026-10-28T02:00:00Z',
                           media_path=None, transcript_path=None)
    result = module.write_pending_manifest(work)
    assert json.loads((tmp_path / result.split('/')[-1]).read_text())['publish_date'] == '2026-05-03'
