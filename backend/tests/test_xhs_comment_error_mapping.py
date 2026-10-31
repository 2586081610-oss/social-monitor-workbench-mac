"""真实采集错误分类测试：重点覆盖 tenacity 包装与 461 无 Verifytype 头的真实形态。

真实场景（2026-10-31 实测）：平台返回 461 + JSON 体（code 300031）且无 Verifytype 响应头，
MediaCrawler 在取 response.headers["Verifytype"] 时抛 KeyError，tenacity 重试 3 次后包成
RetryError——分类必须解包后按风控处理，而不是落到兜底的 page_interrupted。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.xhs_comment_collect import _map_api_error, _unwrap_retry_error  # noqa: E402


class _FakeFuture:
    def __init__(self, exc: BaseException | None) -> None:
        self._exc = exc

    def exception(self):  # noqa: ANN201 - 鸭子类型模拟 concurrent.futures.Future
        return self._exc


class _FakeRetryError(Exception):
    """模拟 tenacity.RetryError 的包装形态（.last_attempt/.last 指向 Future）。"""

    def __init__(self, exc: BaseException, attr: str = "last_attempt") -> None:
        super().__init__(f"FakeRetryError[{exc!r}]")
        if attr == "last_attempt":
            self.last_attempt = _FakeFuture(exc)
        else:
            self.last = _FakeFuture(exc)


def test_verifytype_keyerror_wrapped_in_retry_error_maps_to_rate_limited():
    """461 无 Verifytype 头 → KeyError('Verifytype') → RetryError，应归为风控。"""
    err = _FakeRetryError(KeyError("Verifytype"))
    assert _map_api_error(err) == "rate_limited"


def test_retry_error_with_last_attribute_also_unwrapped():
    err = _FakeRetryError(KeyError("Verifytype"), attr="last")
    assert _map_api_error(err) == "rate_limited"


def test_plain_keyerror_verifytype_maps_to_rate_limited():
    assert _map_api_error(KeyError("Verifytype")) == "rate_limited"


def test_note_unavailable_message_maps_by_code_and_text():
    class _DataFetchError(Exception):
        pass

    err = _DataFetchError("当前笔记暂时无法浏览")
    assert _map_api_error(err) == "note_unavailable"
    assert _map_api_error(_DataFetchError("request failed code 300031")) == "note_unavailable"


def test_wrapped_cause_via_raise_from_is_unwrapped():
    class _DataFetchError(Exception):
        pass

    try:
        try:
            raise _DataFetchError("login expired 300012")
        except _DataFetchError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as err:
        assert _map_api_error(err) == "login_required"


def test_unwrap_returns_original_when_no_inner_exception():
    err = RuntimeError("plain failure")
    assert _unwrap_retry_error(err) is err


def test_page_interrupted_remains_fallback_for_unknown_errors():
    assert _map_api_error(RuntimeError("something else")) == "page_interrupted"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (KeyError("Verifytype"), "rate_limited"),
        (RuntimeError("HTTP 461 blocked"), "rate_limited"),
        (TimeoutError("read timed out"), "network_timeout"),
    ],
)
def test_known_shapes(exc: Exception, expected: str) -> None:
    assert _map_api_error(_FakeRetryError(exc)) == expected
