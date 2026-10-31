"""离线脱敏诊断工具的合成回归：不联网、不回显输入原文。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools import f2_list_diagnostic as diag  # noqa: E402


def _sample(**kwargs):
    base = {"api": "collection", "status": 200, "content_type": "application/json",
            "body_length": 64, "first_char": "{", "keys": ["status_code", "aweme_list"]}
    base.update(kwargs)
    return base


def test_verdicts_distinguish_each_failure_class():
    samples = [
        _sample(api="collection", content_type="text/html; charset=utf-8",
                body_length=2048, first_char="<", keys=["status_code"]),
        _sample(api="collection", body_length=0, first_char="", keys=[]),
        _sample(api="collection", status=500, body_length=32, first_char="{"),
        _sample(api="collection", exception="APIResponseError", cause="JSONDecodeError",
                body_length=16, first_char="{", keys=[]),
        _sample(api="collection", keys=["status_code", "has_more"]),
        _sample(api="collects", keys=["status_code", "has_more"]),
        _sample(api="collects", keys=["status_code", "collects_list", "has_more"]),
        _sample(api="profile", keys=["status_code"]),
    ]
    summary = diag.summarize(samples)
    verdicts = [row["verdict"] for row in summary["samples"]]
    assert verdicts == [
        "non_json_markup", "empty_body", "http_non_200", "json_decode_error",
        "json_missing_list_field", "json_missing_collects_list", "shape_ok",
        "json_missing_user_object",
    ]
    assert summary["by_api"]["collection"] == [
        "non_json_markup", "empty_body", "http_non_200", "json_decode_error",
        "json_missing_list_field",
    ]


def test_summary_contains_only_non_sensitive_categories():
    summary = diag.summarize([
        _sample(content_type="text/html; charset=utf-8", body_length=2048,
                first_char="<"),
    ])
    text = json.dumps(summary, ensure_ascii=False)
    assert "charset" not in text and "text/html" not in text
    row = summary["samples"][0]
    assert set(row) == {"api", "status", "content_type_class", "length_bucket",
                        "json_shape", "keys_present", "exception", "cause", "verdict"}
    assert row["keys_present"] == {"status_code": True, "aweme_list": True,
                                   "collects_list": False, "has_more": False,
                                   "user": False, "data": False}


@pytest.mark.parametrize("bad", [
    {"url": "https://example.test/?msToken=SYNTH_SECRET"},
    {"body": "SYNTH_SECRET"},
    {"cookie": "sessionid=SYNTH_SECRET"},
    {"sec_uid": "SYNTH_SECRET"},
    {"headers": {"cookie": "SYNTH_SECRET"}},
])
def test_sensitive_metadata_is_rejected_without_echo(bad):
    sample = _sample(**bad)
    with pytest.raises(diag.DiagnosticInputError) as excinfo:
        diag.summarize([sample])
    message = str(excinfo.value)
    assert "SYNTH_SECRET" not in message
    assert next(iter(bad)) not in message  # 连字段名也不回显


@pytest.mark.parametrize("field", ["exception", "cause"])
def test_untrusted_error_class_is_rejected_without_echo(field):
    secret = "MS4wLjABAA_SYNTHETIC_IDENTIFIER_123"
    with pytest.raises(diag.DiagnosticInputError) as excinfo:
        diag.summarize([_sample(**{field: secret})])
    assert secret not in str(excinfo.value)
    assert field in str(excinfo.value)  # 字段名是本地固定标签，可保留


@pytest.mark.parametrize("field", ["exception", "cause"])
def test_safe_error_classes_are_accepted_and_summarized(field):
    summary = diag.summarize([_sample(**{field: "JSONDecodeError"})])
    assert summary["samples"][0][field] == "JSONDecodeError"


def test_unknown_sample_key_name_is_not_echoed():
    secret = "SYNTHETIC_SECRET_KEY_123"
    payload = _sample()
    payload[secret] = "unused"
    with pytest.raises(diag.DiagnosticInputError) as excinfo:
        diag.summarize([payload])
    assert secret not in str(excinfo.value)


@pytest.mark.parametrize("bad", [
    {"api": "unknown"},
    {"status": "200"},
    {"body_length": -1},
    {"first_char": "x"},
    {"keys": "status_code"},
    {"exception": "not_a_class"},
])
def test_invalid_metadata_shape_is_rejected(bad):
    with pytest.raises(diag.DiagnosticInputError):
        diag.summarize([_sample(**bad)])


def test_cli_reads_file_and_returns_sanitized_json(tmp_path, capsys):
    path = tmp_path / "samples.json"
    path.write_text(json.dumps({"samples": [_sample(keys=["status_code", "has_more"])]}),
                    encoding="utf-8")
    assert diag.main([str(path)]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["samples"][0]["verdict"] == "json_missing_list_field"
    assert "https://" not in out and "msToken" not in out


def test_cli_reads_stdin(capsys, monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"samples": [_sample(keys=["status_code", "aweme_list"])]})))
    assert diag.main(["-"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["samples"][0]["verdict"] == "shape_ok"


def test_cli_rejects_sensitive_file_without_echo(tmp_path, capsys):
    path = tmp_path / "samples.json"
    path.write_text(json.dumps({"samples": [
        _sample(url="https://example.test/?msToken=SYNTH_SECRET")]}), encoding="utf-8")
    assert diag.main([str(path)]) == 2
    err = capsys.readouterr().err
    assert "SYNTH_SECRET" not in err and "url" not in err


def test_cli_rejects_sensitive_top_level_without_echo(tmp_path, capsys):
    path = tmp_path / "input.json"
    path.write_text(json.dumps({"samples": [_sample()], "cookie": "SYNTHETIC_SECRET"}),
                    encoding="utf-8")
    out = capsys.readouterr()  # 先清空，避免其他输出干扰
    assert diag.main([str(path)]) == 2
    captured = capsys.readouterr()
    assert "SYNTHETIC_SECRET" not in captured.out + captured.err
    assert "cookie" not in captured.out + captured.err


@pytest.mark.parametrize("doc", [
    {"samples": [_sample()], "extra": "SYNTHETIC_SECRET"},
    {"samples": [_sample()], "cookie": "SYNTHETIC_SECRET"},
])
def test_document_allows_only_samples_key(doc):
    with pytest.raises(diag.DiagnosticInputError) as excinfo:
        diag.validate_document(doc)
    assert "SYNTHETIC_SECRET" not in str(excinfo.value)
    assert "extra" not in str(excinfo.value) and "cookie" not in str(excinfo.value)


@pytest.mark.parametrize("doc", [[], "text", {"samples": "not-a-list"}, {}])
def test_document_shape_errors_are_rejected(doc):
    with pytest.raises(diag.DiagnosticInputError):
        diag.validate_document(doc)


@pytest.mark.parametrize("field,bad", [
    ("api", ["SYNTHETIC_PRIVATE_VALUE"]), ("api", {"SYNTHETIC_PRIVATE_KEY": 1}),
    ("first_char", ["{"]), ("first_char", {"SYNTHETIC_PRIVATE_KEY": 1}),
    ("status", []), ("status", {"a": 1}),
    ("body_length", []), ("body_length", {"a": 1}),
    ("content_type", []), ("content_type", {"a": 1}),
    ("keys", {}), ("keys", ["status_code", []]),
    ("exception", []), ("exception", {"a": 1}),
    ("cause", []), ("cause", {"a": 1}),
])
def test_wrong_or_unhashable_field_types_are_rejected_without_crash(field, bad):
    """数组/对象等非法类型必须固定拒绝，不得抛出未捕获 TypeError。"""
    with pytest.raises(diag.DiagnosticInputError) as excinfo:
        diag.summarize([_sample(**{field: bad})])
    assert "SYNTHETIC_PRIVATE" not in str(excinfo.value)


@pytest.mark.parametrize("field", ["api", "first_char"])
@pytest.mark.parametrize("bad", [["SYNTHETIC_PRIVATE_VALUE"],
                                 {"SYNTHETIC_PRIVATE_KEY": 1}])
def test_cli_wrong_types_exit_2_without_traceback(field, bad, capsys, monkeypatch):
    import io
    sample = _sample(**{field: bad})
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"samples": [sample]})))
    assert diag.main(["-"]) == 2
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "SYNTHETIC_PRIVATE" not in text
    assert "Traceback" not in text


def test_cli_other_wrong_types_exit_2_without_traceback(capsys, monkeypatch):
    import io
    for field, bad in [("status", []), ("body_length", {}), ("content_type", []),
                       ("keys", {}), ("exception", []), ("cause", {})]:
        monkeypatch.setattr("sys.stdin", io.StringIO(
            json.dumps({"samples": [_sample(**{field: bad})]})))
        assert diag.main(["-"]) == 2
        captured = capsys.readouterr()
        assert "Traceback" not in captured.out + captured.err


def test_empty_samples_is_rejected():
    with pytest.raises(diag.DiagnosticInputError):
        diag.summarize([])
