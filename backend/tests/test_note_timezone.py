"""笔记日期口径：pending 包与笔记命名按北京时间（Asia/Shanghai）取日。

回归案例：work_id 16608，published_at=2026-09-05T22:13:21Z，
北京时间为 2026-09-06 06:13，旧实现按 UTC 取日错记为 2026-09-05。
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services import note_service as module
from app.services import summary_target_service
from app.utils.timeutil import to_date, to_date_local


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        summary_target_service,
        "_settings_path",
        lambda: tmp_path / "config" / "summary_settings.json",
    )


def _work(**overrides):
    base = dict(id=16608, platform='bilibili', external_id='BV16608', title='fixture',
                creator=None, group=None, url='https://example.invalid',
                published_at=None, created_at=None, media_path=None, transcript_path=None)
    base.update(overrides)
    return SimpleNamespace(**base)


# --- to_date_local 单元级 ---

def test_utc_late_evening_rolls_to_next_beijing_day():
    # 16608 案例：UTC 22:13 = 北京次日 06:13
    assert to_date_local('2026-09-05T22:13:21Z') == '2026-09-06'


def test_utc_early_morning_stays_same_beijing_day():
    # UTC 00:30 = 北京当日 08:30，日期不偏移
    assert to_date_local('2026-09-05T00:30:00Z') == '2026-09-05'


def test_naive_time_treated_as_utc():
    # 存储约定为 UTC：无 tzinfo 的 22:13 同样落到北京次日
    assert to_date_local('2026-09-05T22:13:21') == '2026-09-06'
    assert to_date_local(datetime(2026, 9, 5, 22, 13, 21)) == '2026-09-06'


def test_string_inputs_with_offset_and_plain_date():
    assert to_date_local('2026-09-05T22:13:21+00:00') == '2026-09-06'
    assert to_date_local('2026-09-05') == '2026-09-05'  # 北京 08:00，当日
    # 自带非 UTC 偏移的字符串按自身偏移换算
    assert to_date_local('2026-09-05T16:30:00+00:00') == '2026-09-06'


def test_datetime_object_input():
    dt = datetime(2026, 9, 5, 22, 13, 21, tzinfo=timezone.utc)
    assert to_date_local(dt) == '2026-09-06'


def test_unparseable_and_none_return_none():
    assert to_date_local(None) is None
    assert to_date_local('not-a-date') is None


def test_to_date_default_behavior_unchanged():
    """tz 缺省时保持原 UTC 取日行为，其它潜在调用方不受影响。"""
    assert to_date('2026-09-05T22:13:21Z') == '2026-09-05'
    assert to_date('2026-09-05T22:13:21Z', tz='Asia/Shanghai') == '2026-09-06'


# --- write_pending_manifest 端到端 ---

def test_pending_manifest_uses_beijing_dates(monkeypatch, tmp_path):
    monkeypatch.setattr(module.settings, 'pending_dir', tmp_path)
    work = _work(published_at='2026-09-05T22:13:21Z', created_at='2026-09-06T01:00:00Z')
    result = module.write_pending_manifest(work)
    content = json.loads((tmp_path / result.split('/')[-1]).read_text())
    assert content['publish_date'] == '2026-09-06'
    assert '2026-09-06' in content['expected_output']
    assert result.split('/')[-1].startswith('16608-2026-09-06-')


def test_pending_manifest_created_at_fallback_also_beijing(monkeypatch, tmp_path):
    """published_at 未知时，命名回退到 created_at，同样按北京时间取日。"""
    monkeypatch.setattr(module.settings, 'pending_dir', tmp_path)
    work = _work(published_at=None, created_at='2026-09-05T22:13:21Z')
    result = module.write_pending_manifest(work)
    content = json.loads((tmp_path / result.split('/')[-1]).read_text())
    assert content['publish_date'] == 'unknown'
    assert '2026-09-06' in content['expected_output']
