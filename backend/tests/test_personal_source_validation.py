"""Malformed source data must not manufacture time or silently clear folder facts."""
import pytest

from monitor.platforms import douyin_personal_source as source


def records(published=None, occurred=None):
    return {'schemaVersion': 2, 'records': {'liked_videos': [{
        'videoId': '901001', 'publishedAt': published,
        'occurredAt': occurred, 'occurredAtSource': 'platform_action',
    }], 'favorite_videos': []}}


@pytest.mark.parametrize('timestamp', ['2026-02-30T00:00:00Z', '2026-13-01T00:00:00Z',
                                        '2026-01-01T25:00:00Z'])
def test_impossible_timestamps_remain_unknown(timestamp):
    snapshot = source.parse_snapshot_document(records(timestamp, timestamp), source='file')
    assert snapshot.items[0].published_at is None
    assert snapshot.items[0].occurred_at is None


@pytest.mark.parametrize('folders', [{}, None, [], {'schemaVersion': 99}])
def test_api_rejects_malformed_folders(monkeypatch, folders):
    monkeypatch.setattr(source, '_api_get', lambda base, path, timeout: (
        records() if path == '/v1/records' else folders))
    with pytest.raises(source.PersonalSourceError):
        source.read_snapshot_api('http://127.0.0.1:4765')


@pytest.mark.parametrize('favorite', [
    {'folders': 'bad', 'items': {}}, {'folders': [], 'items': []},
    {'folders': [{'id': 'f', 'name': 'folder'}], 'items': {'901001': 'missing'}},
])
def test_malformed_folder_membership_is_not_empty(favorite):
    with pytest.raises(source.PersonalSourceError):
        source.parse_folders_document({'schemaVersion': 1, 'lists': {'favorite_videos': favorite}})


def test_id_only_record_uses_stable_identity_without_crashing():
    document = records()
    document['records']['liked_videos'] = [{'id': 'liked_videos:901002'}]
    snapshot = source.parse_snapshot_document(document, source='file')
    assert snapshot.items[0].external_id == '901002'
    assert snapshot.items[0].url == 'https://www.douyin.com/video/901002'


def test_unidentifiable_record_is_reported_without_crashing():
    document = records()
    document['records']['liked_videos'] = [{'title': 'missing id'}]
    snapshot = source.parse_snapshot_document(document, source='file')
    assert snapshot.items == []
    assert snapshot.skipped_invalid[0]['reason'] == 'missing_identity'
