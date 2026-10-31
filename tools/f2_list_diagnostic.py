"""F2 列表异常的最小脱敏诊断（**仅离线**）。

用途：把人工/受控采集到的*响应元数据*（不是正文、不是 URL）归纳成可审核的
判定摘要，用于区分“非 JSON / 空体 / HTTP 非 200 / JSON 缺字段 / status_code”
几类原因。本工具不导入任何网络库，永远不发请求；联网诊断须另行授权。

输入 JSON：{"samples": [ ... ]}，每个样本只允许以下非敏感字段：
    api: "like" | "collection" | "collects" | "profile" | "detail"（类别标签）
    status: int                      HTTP 状态码
    content_type: str                MIME 主类型（如 application/json）
    body_length: int                 响应体字节数
    first_char: str                  响应首字符类别（"{" "[" "<" 或空）
    keys: [str]                      顶层字段名（仅字段存在性，不含值）
    exception: str                   异常类名（如 APIResponseError）
    cause: str                       底层原因类名（如 JSONDecodeError）

禁止字段（防误收集敏感信息）：url/uri/query/body/text/content/headers/cookie/
token/sec_uid/aweme_id/user_id 等；出现即整体拒绝，不输出任何输入原文。

输出：逐样本摘要 + api 维度汇总，只含状态、类型分类、长度档、JSON 形状、
字段存在性布尔、异常类名与判定标签；不回显 URL、签名、凭据、标识或正文。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_FORBIDDEN_KEYS = {
    "url", "uri", "query", "querystring", "body", "text", "content", "raw",
    "headers", "header", "cookie", "cookies", "token", "ms_token", "msToken",
    "a_bogus", "signature", "sec_uid", "sec_user_id", "aweme_id", "user_id",
    "uid", "nickname", "author", "title", "cover", "play_addr",
}
_ALLOWED_KEYS = {"api", "status", "content_type", "body_length", "first_char",
                 "keys", "exception", "cause"}
_SAFE_ERROR_CLASSES = frozenset({
    "", "APIResponseError", "JSONDecodeError", "UnicodeDecodeError",
    "APIRateLimitError", "APIUnauthorizedError", "APIRetryExhaustedError",
    "APITimeoutError", "APIConnectionError", "APINotFoundError",
    "TimeoutError", "OSError", "HTTPStatusError", "TransportError",
    "RemoteProtocolError", "ReadTimeout", "ConnectTimeout",
})
_FIELD_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_APIS = {"like", "collection", "collects", "profile", "detail"}


class DiagnosticInputError(ValueError):
    """输入含禁止字段或形状不合法（错误信息只含字段名/类型，不含值）。"""


def _mime_class(content_type: str) -> str:
    value = content_type.strip().lower()
    if "json" in value:
        return "json"
    if "html" in value:
        return "html"
    if "text" in value:
        return "text"
    if "octet-stream" in value or "binary" in value:
        return "binary"
    return "other" if value else "unknown"


def _length_bucket(body_length: int) -> str:
    if body_length <= 0:
        return "empty"
    if body_length < 512:
        return "tiny"
    if body_length < 10 * 1024:
        return "small"
    if body_length < 1024 * 1024:
        return "medium"
    return "large"


def _json_shape(first_char: str) -> str:
    if first_char == "{":
        return "object"
    if first_char == "[":
        return "array"
    if first_char == "<":
        return "markup"
    return "other" if first_char else "unknown"


def _validate_sample(sample: object, index: int) -> dict:
    if not isinstance(sample, dict):
        raise DiagnosticInputError(f"sample[{index}] 不是对象")
    if set(sample) & _FORBIDDEN_KEYS:
        # 不回显任何输入键名/值：键名本身也可能携带敏感内容
        raise DiagnosticInputError(f"sample[{index}] 含禁止字段（字段名不回显）")
    if set(sample) - _ALLOWED_KEYS:
        raise DiagnosticInputError(f"sample[{index}] 含未知字段（字段名不回显）")
    api = sample.get("api")
    # 先做类型校验再成员判断：list/dict 不可哈希，直接 in 会抛 TypeError
    if not isinstance(api, str) or api not in _APIS:
        raise DiagnosticInputError(f"sample[{index}].api 不合法")
    status = sample.get("status")
    if isinstance(status, bool) or not isinstance(status, int) or not 0 <= status <= 999:
        raise DiagnosticInputError(f"sample[{index}].status 不合法")
    body_length = sample.get("body_length")
    if isinstance(body_length, bool) or not isinstance(body_length, int) or body_length < 0:
        raise DiagnosticInputError(f"sample[{index}].body_length 不合法")
    content_type = sample.get("content_type", "")
    if not isinstance(content_type, str) or len(content_type) > 128:
        raise DiagnosticInputError(f"sample[{index}].content_type 不合法")
    first_char = sample.get("first_char", "")
    if not isinstance(first_char, str) or first_char not in {"", "{", "[", "<"}:
        raise DiagnosticInputError(f"sample[{index}].first_char 不合法（只允许空、{{、[ 或 <）")
    keys = sample.get("keys", [])
    if not isinstance(keys, list) or any(
            not isinstance(key, str) or not _FIELD_RE.fullmatch(key) for key in keys):
        raise DiagnosticInputError(f"sample[{index}].keys 不合法")
    for field in ("exception", "cause"):
        value = sample.get(field, "")
        # 只接受明确的安全异常类名；未知值固定拒绝，绝不把输入值写入错误/输出
        if not isinstance(value, str) or value not in _SAFE_ERROR_CLASSES:
            raise DiagnosticInputError(f"sample[{index}].{field} 不在允许的异常类别")
    return {
        "api": api,
        "status": status,
        "content_type": content_type,
        "body_length": body_length,
        "first_char": first_char,
        "keys": keys,
        "exception": sample.get("exception", ""),
        "cause": sample.get("cause", ""),
    }


def _verdict(sample: dict, keys: set[str], shape: str, mime: str) -> str:
    api = sample["api"]
    if sample["status"] != 200:
        return "http_non_200"
    if sample["body_length"] == 0:
        return "empty_body"
    if shape == "markup" or mime == "html":
        return "non_json_markup"
    if sample["cause"] == "JSONDecodeError" or (
            sample["exception"] == "APIResponseError" and shape != "object"):
        return "json_decode_error"
    if shape != "object":
        return "non_json_body"
    if "status_code" not in keys:
        return "json_without_status_code"
    if api in {"collection", "like"} and "aweme_list" not in keys:
        return "json_missing_list_field"
    if api == "collects" and "collects_list" not in keys:
        return "json_missing_collects_list"
    if api == "profile" and "user" not in keys:
        return "json_missing_user_object"
    return "shape_ok"


def summarize(samples: list) -> dict:
    """返回脱敏摘要；任何输入原文（URL/正文/值）都不会出现在输出中。"""
    if not isinstance(samples, list) or not samples:
        raise DiagnosticInputError("samples 必须是非空列表")
    rows = []
    for index, raw in enumerate(samples):
        sample = _validate_sample(raw, index)
        keys = set(sample["keys"])
        shape = _json_shape(sample["first_char"])
        mime = _mime_class(sample["content_type"])
        rows.append({
            "api": sample["api"],
            "status": sample["status"],
            "content_type_class": mime,
            "length_bucket": _length_bucket(sample["body_length"]),
            "json_shape": shape,
            "keys_present": {
                "status_code": "status_code" in keys,
                "aweme_list": "aweme_list" in keys,
                "collects_list": "collects_list" in keys,
                "has_more": "has_more" in keys,
                "user": "user" in keys,
                "data": "data" in keys,
            },
            "exception": sample["exception"],
            "cause": sample["cause"],
            "verdict": _verdict(sample, keys, shape, mime),
        })
    by_api: dict[str, list[str]] = {}
    for row in rows:
        by_api.setdefault(row["api"], []).append(row["verdict"])
    return {"samples": rows, "by_api": by_api}


def validate_document(doc: object) -> list:
    """根对象严格只允许 {"samples": list}；任何额外字段整体拒绝且不回显键名。"""
    if not isinstance(doc, dict):
        raise DiagnosticInputError("根文档不是对象")
    if set(doc) != {"samples"}:
        raise DiagnosticInputError("根文档只允许 samples 字段（额外字段名不回显）")
    samples = doc["samples"]
    if not isinstance(samples, list):
        raise DiagnosticInputError("samples 必须是列表")
    return samples


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: f2_list_diagnostic.py <samples.json|->", file=sys.stderr)
        return 2
    source = args[0]
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        doc = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"输入读取失败：{type(exc).__name__}", file=sys.stderr)
        return 2
    try:
        samples = validate_document(doc)
        summary = summarize(samples)
    except DiagnosticInputError as exc:
        print(f"输入被拒绝：{exc}", file=sys.stderr)
        return 2
    except (TypeError, ValueError):
        # 兜底：任何漏掉的类型问题都固定安全拒绝，不打印 traceback/输入内容
        print("输入被拒绝：形状不合法", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
