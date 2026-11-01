"""小红书评论结果的结构化、合并与报告生成。

纯函数模块：不联网、不读私有配置，供采集子进程和后端测试复用。
保存的 ``comments.json`` 是唯一结构化来源；原始文本、点赞、时间、图片状态与
语音关联证据都保留在该结构中，报告由同一结构生成。
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

SCHEMA_VERSION = 1
CST = timezone(timedelta(hours=8))

# 采集完整性的阻断原因：出现任意一项都不能标记为完整
BLOCKING_REASONS: dict[str, str] = {
    "login_required": "登录态失效或未登录，平台拒绝返回评论",
    "rate_limited": "平台触发风控（461/验证码），需要稍后重试",
    "access_denied": "平台拒绝访问（HTTP 401/403/429）",
    "note_unavailable": "作品不存在、已删除或不可见",
    "network_timeout": "网络超时或连接中断",
    "page_interrupted": "翻页中断，未取到分页结束标志",
    "max_pages_reached": "达到本次页数上限，未走到分页结束",
    "max_comments_reached": "达到本次评论数量上限，未走到分页结束",
    "reply_incomplete": "部分回复分页未取完",
    "collector_error": "采集器异常退出",
    "fixture_missing": "模拟数据目录不存在或缺少 pages.json",
}

# 不阻断完整性、但需要在结果中如实展示的提示
WARNING_REASONS: dict[str, str] = {
    "note_meta_unavailable": "作品标题等基本信息未取得（不影响评论完整性）",
    "voice_extraction_failed": "语音转写提取失败",
    "voice_page_not_found": "未找到已打开的作品页，本次未提取语音转写",
    "voice_cdp_unavailable": "浏览器调试端口未就绪，本次未提取语音转写",
    "image_download_failed": "部分评论图片下载失败，可重新采集重试",
}

REASON_LABELS: dict[str, str] = {**BLOCKING_REASONS, **WARNING_REASONS}

NOTE_ID_RE = re.compile(r"^[0-9A-Za-z]{8,40}$")
_PICTURE_VALUE_KEYS = (
    "url_default",
    "urlDefault",
    "url_pre",
    "urlPre",
    "url",
    "urlDefaultWebp",
    "url_default_webp",
)
_PICTURE_LIST_KEYS = ("pictures", "picture", "image_list", "imageList", "images", "comment_pictures")


def note_id_valid(note_id: str) -> bool:
    return bool(NOTE_ID_RE.fullmatch(str(note_id or "")))


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def reason_label(code: str) -> str:
    return REASON_LABELS.get(code, code)


def to_int(value: Any) -> int:
    """把点赞/回复数（可能是 ``"10+"``、``"1.2万"`` 或数字）安全转为整数。"""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip().replace(",", "")
    match = re.match(r"(\d+(?:\.\d+)?)\s*([万wW亿]?)", text)
    if not match:
        return 0
    number = float(match.group(1))
    unit = match.group(2)
    if unit in {"万", "w", "W"}:
        number *= 10000
    elif unit == "亿":
        number *= 100000000
    return max(0, int(round(number)))


def _iso_from_ts(value: Any) -> tuple[str | None, str | None, Any]:
    """毫秒/秒时间戳 → 北京时间 ISO 字符串；保留原始值。"""
    raw = value
    try:
        if value is None or value == "":
            return None, None, raw
        if isinstance(value, str) and not value.strip().isdigit():
            return None, None, raw
        ts = float(value)
    except (TypeError, ValueError):
        return None, None, raw
    if ts > 10**12:
        ts /= 1000
    if ts <= 0:
        return None, None, raw
    try:
        return datetime.fromtimestamp(ts, CST).isoformat(timespec="seconds"), "second", raw
    except (OverflowError, OSError, ValueError):
        return None, None, raw


def _url_of_picture(item: Any) -> str | None:
    if isinstance(item, str):
        url = item.strip()
        return url if url.startswith(("http://", "https://")) else None
    if not isinstance(item, dict):
        return None
    for key in _PICTURE_VALUE_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value.strip()
    info = item.get("info_list") or item.get("infoList") or []
    urls = [u for u in (_url_of_picture(i) for i in info if isinstance(i, dict)) if u]
    if urls:
        return max(urls, key=len)
    return None


def picture_dedupe_key(url: str) -> str:
    """图片去重键：取 URL 路径（媒体 ID 在路径里）。

    小红书 CDN 会对同一张图在不同镜像域返回路径相同、域名不同的 URL；
    按整串去重会把同一张图当成多张，导致重复下载与相互覆盖。
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        return parts.path or url
    except ValueError:
        return url


def extract_picture_urls(raw: dict) -> list[str]:
    """从评论原始字段提取图片 URL（多图顺序保持不变、按路径去重）。"""
    urls: list[str] = []
    keys: set[str] = set()
    for key in _PICTURE_LIST_KEYS:
        value = raw.get(key)
        if value is None or value == "":
            continue
        items = value if isinstance(value, list) else [value]
        if isinstance(value, str):
            text = value.strip()
            if text.startswith(("http://", "https://")):
                items = [part for part in re.split(r"[,\s|]+", text) if part]
            else:
                items = [text]
        for item in items:
            url = _url_of_picture(item)
            dedupe_key = picture_dedupe_key(url or "")
            if url and dedupe_key and dedupe_key not in keys:
                urls.append(url)
                keys.add(dedupe_key)
    return urls


def _explicit_pinned(raw: dict) -> bool:
    for key in ("pinned", "is_pinned", "isPinned", "is_top", "isTop", "top"):
        if key not in raw:
            continue
        value = raw.get(key)
        if isinstance(value, bool):
            if value:
                return True
        elif isinstance(value, (int, float)) and value:
            return True
        elif isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"}:
            return True
    return False


# 楼中楼「回复 @某人：内容」前缀：小红书把被回复人放在子评论内容开头，
# 平台 API 不单独下发目标用户字段，只能从内容前缀解析。
_REPLY_AT_PREFIX = re.compile(r"^回复\s*@\s*([^\s：:]{1,50})")


def _reply_target_nickname(raw: dict, *, is_reply: bool) -> str | None:
    """解析子评论的被回复人昵称：API 显式字段优先，其次内容「回复 @」前缀。"""
    for key in ("target_user_nickname", "targetUserNickname", "reply_to_nickname"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:50]
    if not is_reply:
        return None
    match = _REPLY_AT_PREFIX.match(str(raw.get("content") or "").strip())
    return match.group(1) if match else None


def normalize_comment(
    raw: dict,
    *,
    parent_comment_id: str | None = None,
    platform_order: int | None = None,
) -> dict | None:
    """单条平台评论 → 统一结构；缺评论 ID 的行丢弃。"""
    comment_id = str(raw.get("comment_id") or raw.get("id") or "").strip()
    if not comment_id:
        return None
    user = raw.get("user_info") or raw.get("userInfo") or raw.get("user") or {}
    nickname = str(raw.get("nickname") or (user.get("nickname") if isinstance(user, dict) else "") or "")
    content = raw.get("content") or ""
    created_at, precision, created_raw = _iso_from_ts(
        raw.get("create_time", raw.get("createTime", raw.get("created_at")))
    )
    picture_urls = extract_picture_urls(raw)
    parent = str(
        raw.get("parent_comment_id")
        or raw.get("parentCommentId")
        or raw.get("target_comment_id")
        or raw.get("targetCommentId")
        or parent_comment_id
        or ""
    ).strip()
    if parent == comment_id:
        parent = str(parent_comment_id or "").strip()
    sub_count_raw = raw.get("sub_comment_count", raw.get("subCommentCount"))
    return {
        "comment_id": comment_id,
        "parent_comment_id": parent,
        "content": str(content),
        "content_status": "present" if str(content).strip() else "empty",
        "like_count": to_int(raw.get("like_count", raw.get("likeCount", raw.get("liked_count")))),
        "nickname": nickname,
        "reply_to_nickname": _reply_target_nickname(raw, is_reply=bool(parent)),
        "created_at": created_at,
        "created_at_raw": created_raw,
        "created_at_precision": precision,
        "sub_comment_count_raw": sub_count_raw,
        "sub_comment_count": to_int(sub_count_raw),
        "pictures": [
            {"url": url, "status": "pending", "local_path": None, "error": None, "attempts": 0}
            for url in picture_urls
        ],
        "voice": {
            "status": "not_extracted",
            "text": None,
            "source": None,
            "association": None,
            "evidence": None,
        },
        "picture_analysis": {"status": "not_analyzed"},
        "pinned": {"status": "none", "evidence": None},
        "platform_order": platform_order,
        "pinned_source": "platform_flag" if _explicit_pinned(raw) else None,
        "in_latest": True,
    }


def associate_voice(comments: list[dict], rows: list[dict]) -> tuple[int, list[dict]]:
    """把平台网页提取的语音转写合并到评论。

    仅当行内带评论 ID（精确）或「昵称唯一 + 点赞一致」时才关联；其余保持
    ``unassociated`` 并原样列出，绝不按顺序猜测。
    """
    attached = 0
    unassociated: list[dict] = []
    by_id = {c["comment_id"]: c for c in comments}
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        comment_id = str(row.get("comment_id") or "").strip()
        if comment_id and comment_id in by_id:
            _attach_voice(by_id[comment_id], row, text, "exact", "平台行内带评论 ID")
            attached += 1
            continue
        nickname = str(row.get("nickname") or row.get("nick") or "").strip()
        likes = row.get("like_count", row.get("likes"))
        candidates = [
            c
            for c in comments
            if nickname
            and c.get("nickname") == nickname
            and not c["parent_comment_id"]
            and (c.get("voice") or {}).get("status") != "transcript_available"
        ]
        if len(candidates) == 1 and (likes in (None, "") or to_int(likes) == candidates[0]["like_count"]):
            _attach_voice(candidates[0], row, text, "heuristic", f"昵称唯一且点赞 {candidates[0]['like_count']} 一致")
            attached += 1
            continue
        unassociated.append(
            {
                "text": text,
                "nickname": nickname or None,
                "like_count": to_int(likes) if likes not in (None, "") else None,
                "evidence": "无法可靠关联到具体评论（昵称/点赞不唯一），未自动挂载",
            }
        )
    return attached, unassociated


def _attach_voice(comment: dict, row: dict, text: str, association: str, evidence: str) -> None:
    comment["voice"] = {
        "status": "transcript_available",
        "text": text,
        "source": "xhs_web_dom",
        "association": association,
        "evidence": evidence,
        "raw_nickname": row.get("nickname") or row.get("nick"),
    }


def _merge_duplicate(prev: dict, cur: dict) -> dict:
    """同一评论 ID 的多条原始行合并：保留首次文本与顺序，补齐缺失字段和图片。"""
    keep = prev
    if not (keep.get("content") or "").strip() and (cur.get("content") or "").strip():
        keep["content"] = cur["content"]
        keep["content_status"] = cur["content_status"]
    keep["like_count"] = max(keep.get("like_count") or 0, cur.get("like_count") or 0)
    keep["sub_comment_count"] = max(keep.get("sub_comment_count") or 0, cur.get("sub_comment_count") or 0)
    if cur.get("sub_comment_count_raw") not in (None, "", 0, "0"):
        keep["sub_comment_count_raw"] = cur["sub_comment_count_raw"]
    seen = {picture_dedupe_key(p["url"]) for p in keep.get("pictures") or []}
    for picture in cur.get("pictures") or []:
        key = picture_dedupe_key(picture["url"])
        if key and key not in seen:
            keep.setdefault("pictures", []).append(picture)
            seen.add(key)
    for field in ("parent_comment_id", "created_at", "created_at_raw", "created_at_precision", "nickname"):
        if not keep.get(field) and cur.get(field):
            keep[field] = cur[field]
    if cur.get("pinned_source"):
        keep["pinned_source"] = cur["pinned_source"]
    if cur.get("platform_order") is not None and keep.get("platform_order") is None:
        keep["platform_order"] = cur["platform_order"]
    return keep


def dedupe_comments(comments: list[dict]) -> list[dict]:
    order: list[str] = []
    by_id: dict[str, dict] = {}
    for comment in comments:
        if not comment:
            continue
        cid = comment["comment_id"]
        if cid not in by_id:
            by_id[cid] = comment
            order.append(cid)
        else:
            by_id[cid] = _merge_duplicate(by_id[cid], comment)
    return [by_id[cid] for cid in order]


def _sort_comments(comments: list[dict]) -> list[dict]:
    mains = [c for c in comments if not c.get("parent_comment_id")]
    replies = [c for c in comments if c.get("parent_comment_id")]

    def key(item: dict):
        return (-int(item.get("like_count") or 0), item.get("created_at") or "9999", item["comment_id"])

    return sorted(mains, key=key) + sorted(replies, key=key)


def _apply_pinned(comments: list[dict]) -> str | None:
    mains = [c for c in comments if not c.get("parent_comment_id") and c.get("in_latest", True)]
    for comment in comments:
        comment["pinned"] = {"status": "none", "evidence": None}
    if not mains:
        return None
    explicit = [c for c in mains if c.get("pinned_source") == "platform_flag"]
    if explicit:
        chosen = explicit[0]
        chosen["pinned"] = {"status": "confirmed", "evidence": "平台返回明确置顶标识"}
        return chosen["comment_id"]
    max_like = max(int(c.get("like_count") or 0) for c in mains)
    ordered = sorted(mains, key=lambda c: (c.get("platform_order") is None, c.get("platform_order") or 0))
    first = ordered[0]
    if first.get("platform_order") == 0 and int(first.get("like_count") or 0) < max_like:
        first["pinned"] = {
            "status": "probable",
            "evidence": f"平台返回顺序第一条，点赞 {first.get('like_count') or 0} 低于最高 {max_like}，按顺序推断，未获平台明确标识",
        }
        return first["comment_id"]
    return None


def _root_of(comment: dict, by_id: dict[str, dict]) -> str:
    current = comment
    seen = {comment["comment_id"]}
    while current.get("parent_comment_id"):
        parent_id = current["parent_comment_id"]
        if parent_id in seen or parent_id not in by_id:
            break
        seen.add(parent_id)
        current = by_id[parent_id]
    return current["comment_id"]


def _reply_shortfall(comments: list[dict]) -> list[dict]:
    """平台显示回复数与本次实际关联数不一致时，如实标记为不完整。"""
    by_id = {c["comment_id"]: c for c in comments}
    fetched: dict[str, int] = {}
    for comment in comments:
        if not comment.get("parent_comment_id"):
            continue
        root = _root_of(comment, by_id)
        fetched[root] = fetched.get(root, 0) + 1
    shortfall = []
    for comment in comments:
        if comment.get("parent_comment_id") or not comment.get("in_latest", True):
            continue
        expected = int(comment.get("sub_comment_count") or 0)
        actual = fetched.get(comment["comment_id"], 0)
        if expected and actual < expected:
            shortfall.append(
                {
                    "comment_id": comment["comment_id"],
                    "expected": expected,
                    "fetched": actual,
                    "raw": comment.get("sub_comment_count_raw"),
                }
            )
    return shortfall


def _counts(comments: list[dict]) -> dict:
    mains = [c for c in comments if not c.get("parent_comment_id")]
    replies = [c for c in comments if c.get("parent_comment_id")]
    images = [p for c in comments for p in (c.get("pictures") or [])]
    voices = [
        c
        for c in comments
        if (c.get("voice") or {}).get("status") == "transcript_available"
    ]
    return {
        "total_comments": len(comments),
        "main_comments": len(mains),
        "replies": len(replies),
        "unique_users": len({c.get("nickname") or c["comment_id"] for c in comments}),
        "image_comments": len([c for c in comments if c.get("pictures")]),
        "images_total": len(images),
        "images_downloaded": len([p for p in images if p.get("status") == "downloaded"]),
        "images_failed": len([p for p in images if p.get("status") == "failed"]),
        "voice_transcripts": len(voices),
        "empty_content_comments": len([c for c in comments if c.get("content_status") == "empty"]),
        "missing_in_latest": len([c for c in comments if not c.get("in_latest", True)]),
    }


def build_result(
    *,
    note_id: str,
    source_url: str,
    comments: list[dict],
    note_meta: dict | None,
    collector: dict,
    pages_fetched: int,
    page_end_reached: bool,
    platform_total: int | None,
    sub_pagination_complete: bool,
    reasons: list[str],
    fatal_error: str | None = None,
    voice_run: dict | None = None,
    collected_at: str | None = None,
) -> dict:
    """生成规范结果：去重、排序、置顶判定、完整性判定与统计。"""
    unique = dedupe_comments(comments)
    unique = _sort_comments(unique)
    pinned_id = _apply_pinned(unique)
    blocking = sorted({r for r in reasons if r in BLOCKING_REASONS})
    warnings = sorted({r for r in reasons if r in WARNING_REASONS})
    reply_shortfall = _reply_shortfall(unique) if sub_pagination_complete else []
    if reply_shortfall and "reply_incomplete" not in blocking:
        blocking.append("reply_incomplete")
    if fatal_error or not unique:
        status = "failed"
        if fatal_error and fatal_error not in blocking:
            blocking.append("collector_error")
    elif page_end_reached and sub_pagination_complete and not blocking:
        status = "complete"
    else:
        status = "incomplete"
    adopted_voice = voice_run or {"requested": False, "status": "not_requested", "extracted": 0, "associated": 0, "unassociated": []}
    counts = _counts(unique)
    stamp = collected_at or now_iso()
    return {
        "schema_version": SCHEMA_VERSION,
        "note_id": note_id,
        "source_url": source_url,
        "collected_at": stamp,
        "first_collected_at": stamp,
        "collector": collector,
        "note_meta": note_meta or {"status": "unavailable", "title": None, "author": None, "platform_total": platform_total},
        "completeness": {
            "status": status,
            "pages_fetched": int(pages_fetched),
            "page_end_reached": bool(page_end_reached),
            "platform_total": platform_total,
            "main_fetched": counts["main_comments"],
            "replies_fetched": counts["replies"],
            "reasons": blocking,
            "reason_texts": [reason_label(r) for r in blocking],
            "warnings": warnings,
            "warning_texts": [reason_label(r) for r in warnings],
            "reply_shortfall": reply_shortfall,
            "fatal_error": fatal_error,
        },
        "order": {"main": "like_desc", "replies": "like_desc"},
        "pinned_comment_id": pinned_id,
        "counts": counts,
        "voice_run": adopted_voice,
        "comments": unique,
        "collection_history": [
            {"collected_at": stamp, "status": status}
        ],
    }


def _carry_forward(old: dict, new: dict) -> None:
    """重试/重新采集时保留旧结果中的人工或不完整成果。"""
    new["first_seen_at"] = old.get("first_seen_at") or old.get("first_collected_at")
    if not (new.get("content") or "").strip() and (old.get("content") or "").strip():
        new["content"] = old["content"]
        new["content_status"] = old.get("content_status", "present")
        new["content_carried_from_previous"] = True
    old_voice = old.get("voice") or {}
    if old_voice.get("status") == "transcript_available" and (new.get("voice") or {}).get("status") != "transcript_available":
        new["voice"] = copy.deepcopy(old_voice)
        new["voice"]["carried_from_previous"] = True
    old_images = {picture_dedupe_key(p.get("url") or ""): p for p in (old.get("pictures") or []) if p.get("url")}
    carried: list[dict] = []
    seen: set[str] = set()
    for picture in new.get("pictures") or []:
        key = picture_dedupe_key(picture.get("url") or "")
        seen.add(key)
        previous = old_images.get(key)
        if previous and picture.get("status") != "downloaded" and previous.get("status") == "downloaded":
            carried.append(copy.deepcopy(previous))
        else:
            carried.append(picture)
    for key, previous in old_images.items():
        if key and key not in seen and previous.get("status") == "downloaded":
            carried.append(copy.deepcopy(previous))
    new["pictures"] = carried
    old_analysis = old.get("picture_analysis") or {}
    if old_analysis.get("status") not in (None, "not_analyzed") and (new.get("picture_analysis") or {}).get("status") == "not_analyzed":
        new["picture_analysis"] = copy.deepcopy(old_analysis)
    if isinstance(old.get("manual"), dict):
        new["manual"] = copy.deepcopy(old["manual"])


def merge_result(existing: dict | None, new: dict) -> dict:
    """按评论 ID 取并集：重复采集不增加重复评论，不覆盖旧语音/图片成果。"""
    if not existing:
        return new
    merged = copy.deepcopy(new)
    merged["first_collected_at"] = existing.get("first_collected_at") or existing.get("collected_at")
    history = list(existing.get("collection_history") or [])[-9:]
    history.append({"collected_at": merged.get("collected_at"), "status": (merged.get("completeness") or {}).get("status")})
    merged["collection_history"] = history

    old_comments = {c["comment_id"]: c for c in existing.get("comments") or [] if c.get("comment_id")}
    new_ids = {c["comment_id"] for c in merged.get("comments") or []}
    merged_comments: list[dict] = []
    for comment in merged.get("comments") or []:
        previous = old_comments.get(comment["comment_id"])
        if previous:
            _carry_forward(previous, comment)
        comment["in_latest"] = True
        merged_comments.append(comment)
    for cid, previous in old_comments.items():
        if cid in new_ids:
            continue
        kept = copy.deepcopy(previous)
        kept["in_latest"] = False
        kept["pinned"] = {"status": "none", "evidence": None}
        merged_comments.append(kept)

    merged_comments = _sort_comments(merged_comments)
    merged["comments"] = merged_comments
    merged["pinned_comment_id"] = _apply_pinned(merged_comments)
    merged["counts"] = _counts(merged_comments)

    old_meta = existing.get("note_meta") or {}
    new_meta = merged.get("note_meta") or {}
    if not new_meta.get("title") and old_meta.get("title"):
        merged["note_meta"] = {**new_meta, "title": old_meta["title"], "author": new_meta.get("author") or old_meta.get("author")}
    return merged


def group_comments(result: dict) -> list[dict]:
    """主评论按点赞降序，每条挂接其全部后代回复；孤儿回复单列。"""
    comments = result.get("comments") or []
    by_id = {c["comment_id"]: c for c in comments}
    mains = [c for c in comments if not c.get("parent_comment_id")]
    replies = [c for c in comments if c.get("parent_comment_id")]
    grouped = []
    used: set[str] = set()
    for main in mains:
        children = [r for r in replies if r["comment_id"] not in used and _root_of(r, by_id) == main["comment_id"]]
        for child in children:
            used.add(child["comment_id"])
        grouped.append({"main": main, "replies": children})
    orphans = [r for r in replies if r["comment_id"] not in used]
    return grouped + ([{"main": None, "replies": orphans}] if orphans else [])


def _reply_depth(comment: dict, by_id: dict[str, dict]) -> int:
    depth = 0
    current = comment
    seen = {comment["comment_id"]}
    while current.get("parent_comment_id") and current["parent_comment_id"] in by_id:
        parent_id = current["parent_comment_id"]
        if parent_id in seen:
            break
        seen.add(parent_id)
        depth += 1
        current = by_id[parent_id]
    return depth


def result_summary(result: dict) -> dict:
    completeness = result.get("completeness") or {}
    collector = result.get("collector") or {}
    note_meta = result.get("note_meta") or {}
    return {
        "note_id": result.get("note_id"),
        "title": note_meta.get("title"),
        "source_url": result.get("source_url"),
        "collected_at": result.get("collected_at"),
        "first_collected_at": result.get("first_collected_at"),
        "mode": collector.get("mode"),
        "completeness_status": completeness.get("status"),
        "completeness_reasons": completeness.get("reasons") or [],
        "counts": result.get("counts") or {},
        "pinned_comment_id": result.get("pinned_comment_id"),
        "voice_status": (result.get("voice_run") or {}).get("status"),
    }


def _display_time(comment: dict) -> str:
    value = comment.get("created_at")
    return value.replace("T", " ")[:16] if value else "时间未知"


def _image_lines(comment: dict) -> list[str]:
    lines: list[str] = []
    for picture in comment.get("pictures") or []:
        if picture.get("status") == "downloaded" and picture.get("local_path"):
            lines.append(f"![评论图片]({picture['local_path']})")
        elif picture.get("status") == "failed":
            reason = picture.get("error") or "下载失败"
            lines.append(f"- 图片未下载（{reason}）：{picture.get('url')}")
        else:
            lines.append(f"- 图片待下载：{picture.get('url')}")
    return lines


def _voice_line(comment: dict) -> str | None:
    voice = comment.get("voice") or {}
    if voice.get("status") == "transcript_available" and voice.get("text"):
        suffix = "（关联待核对）" if voice.get("association") == "heuristic" else ""
        return f"语音转写{suffix}：{voice['text']}"
    return None


def build_report(result: dict) -> str:
    """完整可读报告：置顶、全部主评论及回复、图片与语音状态、完整性说明。"""
    completeness = result.get("completeness") or {}
    note_meta = result.get("note_meta") or {}
    counts = result.get("counts") or {}
    collector = result.get("collector") or {}
    title = note_meta.get("title") or f"作品 {result.get('note_id')}"
    status = completeness.get("status")
    status_text = {
        "complete": "采集完整（已取到分页结束标志）",
        "incomplete": "采集不完整（见下方原因）",
        "failed": "采集失败",
    }.get(status, "状态未知")
    mode = collector.get("mode")
    mode_text = "模拟数据（未访问真实平台）" if mode == "fixture" else "真实平台采集"
    lines = [
        f"# 小红书评论采集报告：{title}",
        "",
        f"- 作品链接：{result.get('source_url')}",
        f"- 作品 ID：{result.get('note_id')}",
        f"- 采集时间：{(result.get('collected_at') or '').replace('T', ' ')[:19]}（北京时间）",
        f"- 数据来源：{mode_text}（引擎：{collector.get('engine') or '未知'}）",
        f"- 完整性：{status_text}",
    ]
    pages = completeness.get("pages_fetched")
    page_end = "是" if completeness.get("page_end_reached") else "否"
    platform_total = completeness.get("platform_total")
    lines.append(f"- 分页：已获取 {pages} 页，分页结束：{page_end}，平台返回总数：{platform_total if platform_total is not None else '未返回'}")
    lines.append(
        f"- 数量：评论 {counts.get('total_comments', 0)} 条（主评论 {counts.get('main_comments', 0)} / 回复 {counts.get('replies', 0)}）"
    )
    lines.append(
        f"- 图片：{counts.get('images_total', 0)} 张（成功 {counts.get('images_downloaded', 0)} / 失败 {counts.get('images_failed', 0)}）"
        f"；语音转写：{counts.get('voice_transcripts', 0)} 条"
    )
    if completeness.get("reason_texts"):
        lines.append("")
        lines.append("## 不完整原因")
        for text in completeness["reason_texts"]:
            lines.append(f"- {text}")
    warnings = completeness.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("## 提示")
        for code in warnings:
            lines.append(f"- {reason_label(code)}")

    comments = result.get("comments") or []
    pinned = next((c for c in comments if c["comment_id"] == result.get("pinned_comment_id")), None)
    if pinned:
        status_label = "平台明确置顶" if pinned["pinned"]["status"] == "confirmed" else "可能置顶（顺序推断）"
        lines += [
            "",
            "## 置顶评论",
            "",
            f"**{pinned.get('nickname') or '匿名用户'}**（{pinned.get('like_count', 0)} 赞，{_display_time(pinned)}）",
            "",
            pinned.get("content") or "（无文本内容）",
        ]
        if pinned["pinned"].get("evidence"):
            lines.append("")
            lines.append(f"依据：{pinned['pinned']['evidence']}（{status_label}）")
        for line in _image_lines(pinned):
            lines.append(line)
        voice = _voice_line(pinned)
        if voice:
            lines.append(voice)
        lines.append("")

    grouped = group_comments(result)
    by_id = {c["comment_id"]: c for c in comments}
    main_index = 0
    lines += ["", "## 主评论（按点赞数降序）", ""]
    for node in grouped:
        main = node["main"]
        replies = node["replies"]
        if main is None:
            lines += ["### 未关联到主评论的回复", "", "回复（按点赞数降序）：", ""]
        else:
            main_index += 1
            tag = "（置顶）" if main["comment_id"] == result.get("pinned_comment_id") else ""
            lines += [
                f"### {main_index}. {main.get('nickname') or '匿名用户'}{tag}",
                "",
                f"{main.get('like_count', 0)} 赞 · {_display_time(main)} · 评论 ID：{main['comment_id']}",
                "",
                main.get("content") or "（无文本内容，可能为语音/表情/图片评论，未核实）",
                "",
            ]
            lines += _image_lines(main)
            voice = _voice_line(main)
            if voice:
                lines.append(voice)
            lines.append("")
            if replies:
                declared = main.get("sub_comment_count_raw")
                declared_text = f" / 平台显示 {declared}" if declared not in (None, "", 0, "0") else ""
                lines.append(f"回复（本次已关联 {len(replies)} 条{declared_text}，按点赞数降序）：")
                lines.append("")
        for reply in replies:
            depth = _reply_depth(reply, by_id) if main is not None else 1
            indent = "  " * max(depth - 1, 0)
            prefix = f"{indent}┗ "
            lines.append(f"- {prefix}**{reply.get('nickname') or '匿名用户'}**（{reply.get('like_count', 0)} 赞，{_display_time(reply)}）：{reply.get('content') or '（无文本内容）'}")
            line_indent = "  " * (depth + 1)
            for line in _image_lines(reply):
                lines.append(f"{line_indent}{line}")
            voice = _voice_line(reply)
            if voice:
                lines.append(f"{line_indent}{voice}")
        lines.append("")

    stale = [c for c in comments if not c.get("in_latest", True)]
    if stale:
        lines += ["## 本次采集未返回的旧评论（保留历史）", ""]
        for comment in stale:
            lines.append(f"- {comment.get('nickname') or '匿名用户'}（{comment.get('like_count', 0)} 赞）：{comment.get('content') or '（无文本内容）'}")
        lines.append("")

    unassociated = (result.get("voice_run") or {}).get("unassociated") or []
    if unassociated:
        lines += ["## 未关联的语音转写", "", "以下转写无法可靠对应到具体评论，仅原样保留：", ""]
        for row in unassociated:
            lines.append(f"- {row.get('nickname') or '未知用户'}（{row.get('like_count') if row.get('like_count') is not None else '点赞未知'}）：{row.get('text')}")
        lines.append("")

    lines += [
        "## 未完成事项",
        "",
        "- 评论图片的 AI 内容识别未接入：图片仅保存原图，状态为未分析，不编造图片文字或内容。",
        "- 语音：仅使用平台网页已有转写；未能关联或未取得的项目按状态保留，未使用本地转写。",
        "",
        "---",
        "报告由本地评论采集流程生成；模拟数据模式的结果不代表真实平台采集。",
    ]
    if result.get("first_collected_at") and result.get("first_collected_at") != result.get("collected_at"):
        lines.append(f"首次采集：{(result.get('first_collected_at') or '').replace('T', ' ')[:19]}；本次为重新采集。")
    return "\n".join(lines)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
