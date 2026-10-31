"""抖音本人点赞/收藏只读来源适配器（DouyinDataReview 本地 JSON / 回环 API）。

设计依据：`preflight/favorites-ingest-design.md`；取证：`preflight/douyindatareview-evidence-2026-10-28.md`。

硬约定：
- 只读。不调用任何会触发读取/互动/聊天的接口（不用 /v1/sync、/v1/pair*）。
- 失败 ≠ 空列表：文件缺失、JSON 损坏、schemaVersion 不识别都抛 PersonalSourceError，
  绝不返回空结果冒充"没有内容"。
- 喜欢/收藏在 DDR 中是"按作品身份的当前状态快照"，一般没有平台侧动作时间：
  occurredAt 仅在 occurredAtSource=platform_action 时可信，否则记未知。
- publishedAt 是作品发布时间（DDR 由 aweme create_time 的 toISOString() 生成，
  恒为 UTC ISO 串），不得当作点赞/收藏发生时间。
- 本模块不 import app.*，保持可独立测试。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SNAPSHOT_SCHEMA_VERSION = 2
FOLDERS_SCHEMA_VERSION = 1
MARKS = ("like", "favorite")
# DDR records.json 的列表键 → 统一 source_mark
_RECORD_TYPE_TO_MARK = {"liked_videos": "like", "favorite_videos": "favorite"}
_DEFAULT_DATA_DIR = Path.home() / "Library" / "Application Support" / "内容数据工作台" / "collector" / ".local-data"
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

# 凭据/标识脱敏（兜底层；错误文案本身按白名单构造，不依赖这里罗列秘密键）
_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(ms_token|mstoken|a_bogus|verify_fp|verifyfp|s_v_web_id|webid|ttwid|odin_tt|"
    r"passport_csrf_token|sessionid|sessionid_ss|sid_tt|sid_guard|uid_tt|uiinput|"
    r"cookie|authorization|token|signature|sign|auth|key|credential)"
    r"(\s*[=:]\s*)(\"?)[^&'\"\s,;)}\]]*"
)
# Cookie/Authorization 头整值（含多个分号分隔的键值对）
_COOKIE_HEADER_RE = re.compile(r"(?i)\bcookie\s*:\s*[^\r\n]+")
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}")
# URL 查询串整体（携带 msToken/a_bogus 等签名参数，不逐键罗列）
_URL_QUERY_RE = re.compile(r"\?[^'\"\s<>]+")
# 本人 sec_uid 与较长数字标识（抖音作品/账号 ID 约 19 位数字，日志不出现）
_SEC_UID_RE = re.compile(r"\bMS4wLjABAA[A-Za-z0-9_-]{8,}")
_LONG_NUMERIC_ID_RE = re.compile(r"(?i)\b(folder)[:#]?\s*\d{10,}")
_LONG_NUMBER_RE = re.compile(r"\b\d{15,}\b")


def redact_sensitive_text(text: str, *, max_length: int = 300) -> str:
    """脱敏错误消息/日志：抹去凭据值、URL 查询串与本人/收藏夹标识，超长截断。

    适用于异常 str()、上游输出、poll_logs detail——凭据、账号标识不得落入日志与 UI。
    注意：错误文案的主防线是白名单式构造（只含 kind/类别/数量），本函数是兜底。
    """
    if not isinstance(text, str):
        text = str(text)
    redacted = _COOKIE_HEADER_RE.sub("Cookie: <redacted>", text)
    redacted = _BEARER_RE.sub(r"\1 <redacted>", redacted)
    redacted = _URL_QUERY_RE.sub("?<redacted>", redacted)
    redacted = _SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", redacted)
    redacted = _SEC_UID_RE.sub("<sec_uid>", redacted)
    redacted = _LONG_NUMERIC_ID_RE.sub(r"\1<id>", redacted)
    redacted = _LONG_NUMBER_RE.sub("<long-id>", redacted)
    if len(redacted) > max_length:
        redacted = redacted[:max_length] + "…<截断>"
    return redacted


class PersonalSourceError(RuntimeError):
    """显式失败（缺文件/坏 JSON/版本不识别/接口不可用）。kind 供报告与日志区分。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class PersonalItem:
    """一条本人来源事实：(external_id, source_mark) 为最小事实单元。"""

    external_id: str
    source_mark: str  # like | favorite
    title: str | None = None
    author: str | None = None
    author_id: str | None = None
    url: str | None = None
    cover_url: str | None = None
    media_type: str | None = None
    published_at: str | None = None  # 仅完整 UTC ISO 串（DDR toISOString()）
    occurred_at: str | None = None  # 仅 occurredAtSource=platform_action 时保留
    occurred_at_source: str | None = None


@dataclass
class PersonalSnapshot:
    items: list[PersonalItem] = field(default_factory=list)
    skipped_invalid: list[dict] = field(default_factory=list)  # 无法解析身份的记录（显式上报，不静默丢）
    folders: dict[str, str] = field(default_factory=dict)  # folder_id -> name（favorite 夹）
    folder_assignments: dict[str, str] = field(default_factory=dict)  # videoKey -> folder_id（DDR 单夹）
    # F2 平台多夹归属：external_id -> [folder_id...]（一作可多夹，与单夹语义并存不混用）
    folder_memberships: dict[str, list[str]] = field(default_factory=dict)
    # 夹级不完整明细：["folders"]（夹列表分页未走完）或 ["folder:<id>"]（夹内作品未走完）
    folder_incomplete: list[str] = field(default_factory=list)
    # 夹数据形态：single=DDR 一作一夹（写 marks.folder_id）；multi=F2 平台多夹
    #（写 personal_source_folder_items）；None=无夹数据（旧快照按 single 兼容处理）
    folder_mode: str | None = None
    folders_available: bool = False
    warnings: list[str] = field(default_factory=list)
    direct_sync: dict = field(default_factory=dict)  # 各列表完整读取标记
    snapshot_updated_at: str | None = None
    snapshot_id: str = ""
    source: str = "file"  # file | api | f2
    source_path: str | None = None
    # F2 提交协议：取数结果回传的续传状态（仅哈希身份/游标/完成标记）。
    # 业务数据持久提交后才允许据此推进/清除游标；None 表示无可确认状态。
    resume_state: dict | None = None
    # F2 回执续传状态异常：missing=应有而缺失（未声明禁用）、invalid_type=键存在但非对象。
    # 非 None 时 ingest 必须如实上报并保留旧游标，绝不静默跳过确认。
    resume_anomaly: str | None = None


def _clean_str(value) -> str | None:
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    return None


def _valid_timestamp(value) -> bool:
    if not isinstance(value, str) or not _ISO_RE.fullmatch(value):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).year > 1970
    except ValueError:
        return False


def _trusted_timestamp(record: dict) -> tuple[str | None, str | None]:
    """喜欢/收藏的动作时间：仅 platform_action 可信（B2 取证 §4）。"""
    occurred = record.get("occurredAt") if isinstance(record.get("occurredAt"), str) else None
    source = record.get("occurredAtSource") if isinstance(record.get("occurredAtSource"), str) else None
    if source == "platform_action" and _valid_timestamp(occurred):
        return occurred, source
    return None, source


def _normalize_record(record: dict, mark: str) -> tuple[PersonalItem | None, dict | None]:
    if not isinstance(record, dict):
        return None, {"reason": "not_an_object", "raw_kind": type(record).__name__}
    external_id = _clean_str(record.get("videoId"))
    if not external_id:
        raw_id = _clean_str(record.get("id")) or ""
        # 兜底：DDR 记录 id 形如 "favorite_videos:<aweme_id>"
        if raw_id.startswith(("liked_videos:", "favorite_videos:")):
            external_id = raw_id.split(":", 1)[1]
    if not external_id:
        return None, {"reason": "missing_identity", "raw_id": raw_id[:80]}
    published = _clean_str(record.get("publishedAt"))
    if published and not _valid_timestamp(published):
        published = None  # 非完整 ISO 一律按未知处理，不猜测精度/时区
    occurred, occurred_source = _trusted_timestamp(record)
    media_type = _clean_str(record.get("mediaType"))
    item = PersonalItem(
        external_id=external_id,
        source_mark=mark,
        title=_clean_str(record.get("title")),
        author=_clean_str(record.get("author")),
        author_id=_clean_str(record.get("authorId")),
        url=_clean_str(record.get("url")) or f"https://www.douyin.com/video/{external_id}",
        cover_url=_clean_str(record.get("coverUrl")),
        media_type=media_type if media_type in {"video", "image"} else None,
        published_at=published,
        occurred_at=occurred,
        occurred_at_source=occurred_source,
    )
    return item, None


def parse_snapshot_document(doc, *, source: str, source_path: str | None = None) -> PersonalSnapshot:
    """校验并归一化 records.json 形状的文档（文件与回环 API 共用）。"""
    if not isinstance(doc, dict):
        raise PersonalSourceError("invalid_json", "records 文档不是 JSON 对象")
    version = doc.get("schemaVersion")
    if version != SNAPSHOT_SCHEMA_VERSION:
        raise PersonalSourceError(
            "schema_changed",
            f"records schemaVersion={version!r}（本适配器锚定 {SNAPSHOT_SCHEMA_VERSION}），需人工核对后再接入",
        )
    records = doc.get("records")
    if not isinstance(records, dict):
        raise PersonalSourceError("schema_changed", "records 缺少 records 对象")

    snapshot = PersonalSnapshot(source=source, source_path=source_path)
    snapshot.snapshot_updated_at = _clean_str(doc.get("updatedAt"))
    warnings = doc.get("warnings")
    if isinstance(warnings, list):
        snapshot.warnings = [w for w in warnings if isinstance(w, str)]
    direct_sync = doc.get("directSync")
    if isinstance(direct_sync, dict):
        snapshot.direct_sync = {k: bool(v) for k, v in direct_sync.items() if isinstance(v, (bool, str))}

    for record_type, mark in _RECORD_TYPE_TO_MARK.items():
        entries = records.get(record_type)
        if entries is None:
            raise PersonalSourceError("schema_changed", f"records.{record_type} 缺失")
        if not isinstance(entries, list):
            raise PersonalSourceError("schema_changed", f"records.{record_type} 不是列表")
        for raw in entries:
            item, invalid = _normalize_record(raw, mark)
            if item is None:
                snapshot.skipped_invalid.append({"list": record_type, **(invalid or {})})
            else:
                snapshot.items.append(item)

    digest = hashlib.sha256(
        json.dumps(
            [[i.external_id, i.source_mark, i.published_at or "", i.occurred_at or ""] for i in snapshot.items],
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    snapshot.snapshot_id = digest[:16]
    return snapshot


def parse_folders_document(doc) -> tuple[dict[str, str], dict[str, str]]:
    """校验 folders.json 形状的文档，返回 (folder_id->name, videoKey->folder_id)。

    只取 favorite 栏目；DDR 为单夹语义（一作一夹），未分区不产生条目。
    """
    if not isinstance(doc, dict):
        raise PersonalSourceError("invalid_json", "folders 文档不是 JSON 对象")
    if doc.get("schemaVersion") != FOLDERS_SCHEMA_VERSION:
        raise PersonalSourceError(
            "schema_changed",
            f"folders schemaVersion={doc.get('schemaVersion')!r}（锚定 {FOLDERS_SCHEMA_VERSION}）",
        )
    lists = doc.get("lists")
    favorite = lists.get("favorite_videos") if isinstance(lists, dict) else None
    if not isinstance(favorite, dict):
        raise PersonalSourceError("schema_changed", "folders.lists.favorite_videos 缺失")
    folder_rows, items = favorite.get("folders"), favorite.get("items")
    if not isinstance(folder_rows, list) or not isinstance(items, dict):
        raise PersonalSourceError("schema_changed", "folders 的 folders/items 类型不正确")
    folders: dict[str, str] = {}
    for folder in folder_rows:
        if not isinstance(folder, dict) or not isinstance(folder.get("id"), str) or not isinstance(folder.get("name"), str):
            raise PersonalSourceError("schema_changed", "folders 含无效文件夹记录")
        folders[folder["id"]] = folder["name"]
    assignments: dict[str, str] = {}
    for key, folder_id in items.items():
        if not isinstance(key, str) or not isinstance(folder_id, str) or folder_id not in folders:
            raise PersonalSourceError("schema_changed", "folders 含无效或悬空成员关系")
        assignments[key] = folder_id
    return folders, assignments


def read_snapshot_files(records_path: Path, folders_path: Path | None = None) -> PersonalSnapshot:
    """通道 A：从本地 JSON 文件读取。records 缺失/损坏 → 显式失败；folders 缺失 → 可容忍（如实标注）。"""
    records_path = Path(records_path)
    if not records_path.is_file():
        raise PersonalSourceError("missing_file", f"records.json 不存在: {records_path}")
    try:
        doc = json.loads(records_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PersonalSourceError("invalid_json", f"records.json 读取/解析失败: {exc}") from None
    snapshot = parse_snapshot_document(doc, source="file", source_path=str(records_path))

    if folders_path is not None and Path(folders_path).is_file():
        try:
            folders, assignments = parse_folders_document(
                json.loads(Path(folders_path).read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise PersonalSourceError("invalid_json", f"folders.json 读取/解析失败: {exc}") from None
        snapshot.folders, snapshot.folder_assignments = folders, assignments
        snapshot.folders_available = True
        snapshot.folder_mode = "single"
    return snapshot


def read_snapshot_api(base_url: str, timeout: float = 10.0) -> PersonalSnapshot:
    """通道 B：应用运行时只读 GET /v1/records 与 /v1/folders（回环 127.0.0.1:4765）。

    不调用 /v1/sync、/v1/pair 等任何会触发读取或写操作的端点。
    """
    base = base_url.rstrip("/")
    snapshot = _api_get(base, "/v1/records", timeout)
    parsed = parse_snapshot_document(snapshot, source="api", source_path=base)
    folders_doc = _api_get(base, "/v1/folders", timeout)
    folders, assignments = parse_folders_document(folders_doc)
    parsed.folders, parsed.folder_assignments = folders, assignments
    parsed.folders_available = True
    parsed.folder_mode = "single"
    return parsed


def _api_get(base: str, path: str, timeout: float):
    url = f"{base}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PersonalSourceError("api_error", f"{path} 返回 HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise PersonalSourceError("api_unavailable", f"回环 API 不可用（应用未运行？）{path}: {exc}") from None


def resolve_data_dirs(base_dir: str | os.PathLike | None = None) -> list[Path]:
    """返回候选账号数据目录（含 records.json 的目录）。多账号时调用方必须显式选择。"""
    base = Path(base_dir) if base_dir else Path(os.getenv("DDR_DATA_DIR", "")) if os.getenv("DDR_DATA_DIR") else _DEFAULT_DATA_DIR
    accounts = base / "accounts"
    if accounts.is_dir():
        found = sorted(p for p in accounts.iterdir() if (p / "records.json").is_file())
        if found:
            return found
    return [base] if (base / "records.json").is_file() else []


def snapshot_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
