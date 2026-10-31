"""账号健康与切换：四平台登录态真校验 + Cookie 更新落盘（F2/下载管道消费同一来源）。

诚实原则：探测不到就如实 UNKNOWN/WARN，不编造已登录。
- bilibili: api.bilibili.com/x/web-interface/nav（isLogin 字段，无需签名）
- douyin:   cookie 必须含会话键（sessionid*），passport/account/info/v2 探测；探测不可达时降级 WARN
- xhs:      cookie 必须含 web_session，selfinfo 探测
- twitter:  cookie 必须含 auth_token，verify_credentials 探测（该站常需代理，网络不可达如实 WARN）

Cookie 落盘（切换账号 = 覆写后立即校验）：
- douyin → config/f2/douyin_cookie.txt（F2_DOUYIN_COOKIE_FILE 可覆盖，F2 个人来源消费）
- bilibili/xhs/twitter → config/platform_cookies.json 的 <platform>.cookie（xhs/推特下载管道消费）

日志与返回值均不包含 Cookie 内容。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import validation
from app.repositories import health_repo
from app.schemas.health import CookieHealth
from app.utils.timeutil import now_iso

PLATFORMS = ("douyin", "bilibili", "xhs", "twitter")

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
_TIMEOUT = httpx.Timeout(6.0)

# 浏览器端公开常量，仅用于登录态探测（verify_credentials）
_TWITTER_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

_DOUYIN_COOKIE_FILE = settings.workspace_root / "config" / "f2" / "douyin_cookie.txt"
_PLATFORM_COOKIES_FILE = settings.workspace_root / "config" / "platform_cookies.json"

# 各平台登录会话的必要 Cookie 键：缺失即判未登录（未登录的站点不会下发这些键）
_SESSION_KEYS: dict[str, tuple[str, ...]] = {
    "douyin": ("sessionid", "sessionid_ss", "sid_tt", "sid_guard"),
    "bilibili": ("SESSDATA",),
    "xhs": ("web_session",),
    "twitter": ("auth_token",),
}


# ---------- Cookie 存取（douyin 单文件 / 其余 JSON） ----------
def _cookie_path(platform: str) -> Path:
    if platform == "douyin":
        return Path(os.getenv("F2_DOUYIN_COOKIE_FILE") or _DOUYIN_COOKIE_FILE)
    return _PLATFORM_COOKIES_FILE


def _read_stored_cookie(platform: str) -> str:
    path = _cookie_path(platform)
    try:
        if platform == "douyin":
            return path.read_text(encoding="utf-8").strip()
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    section = payload.get(platform) if isinstance(payload, dict) else None
    if isinstance(section, dict):
        return str(section.get("cookie") or "").strip()
    return ""


def _write_stored_cookie(platform: str, cookie: str) -> None:
    path = _cookie_path(platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    if platform == "douyin":
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(cookie + "\n", encoding="utf-8")
        tmp.replace(path)
        return
    payload: dict = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            payload = loaded
    except (OSError, json.JSONDecodeError):
        payload = {}
    section = payload.get(platform)
    payload[platform] = {**section, "cookie": cookie} if isinstance(section, dict) else {"cookie": cookie}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _cookie_keys(cookie: str) -> set[str]:
    return {part.split("=", 1)[0].strip() for part in cookie.split(";") if "=" in part}


# ---------- 在线探测 ----------
def _probe(
    url: str,
    *,
    cookie: str,
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response | None:
    headers = {"User-Agent": _UA, "Cookie": cookie}
    if extra_headers:
        headers.update(extra_headers)
    try:
        return httpx.get(url, headers=headers, timeout=_TIMEOUT, follow_redirects=False)
    except (httpx.HTTPError, OSError):
        return None


def _check_bilibili(cookie: str) -> tuple[str, str]:
    if not _SESSION_KEYS["bilibili"][0] in _cookie_keys(cookie):
        return "NEEDS_RELOGIN", "凭证缺 SESSDATA（未登录或导出不完整）"
    resp = _probe("https://api.bilibili.com/x/web-interface/nav", cookie=cookie)
    if resp is None:
        return "WARN", "有 SESSDATA 但网络不可达，未完成在线校验"
    try:
        payload = resp.json()
    except ValueError:
        return "UNKNOWN", f"B站响应异常（HTTP {resp.status_code}）"
    data = payload.get("data") if isinstance(payload, dict) else None
    if resp.status_code == 200 and isinstance(data, dict) and data.get("isLogin") is True:
        uname = str(data.get("uname") or "").strip()
        return ("OK", f"已登录：{uname}") if uname else ("OK", "已登录")
    return "NEEDS_RELOGIN", "B站在线校验未通过（isLogin=false 或被拒）"


def _check_douyin(db: Session, cookie: str) -> tuple[str, str]:
    if not _cookie_keys(cookie) & set(_SESSION_KEYS["douyin"]):
        return "NEEDS_RELOGIN", "凭证缺 sessionid（未登录或导出不完整）"
    resp = _probe("https://www.douyin.com/passport/account/info/v2/?aid=6383", cookie=cookie)
    if resp is None:
        status, note = "WARN", "有会话凭证（sessionid）但网络不可达，未完成在线校验"
    else:
        try:
            payload = resp.json()
        except ValueError:
            status, note = "UNKNOWN", f"抖音响应异常（HTTP {resp.status_code}）"
        else:
            data = payload.get("data") if isinstance(payload, dict) else None
            if resp.status_code == 200 and isinstance(data, dict) and data.get("user_id"):
                who = str(data.get("name") or data.get("user_id") or "").strip()
                status, note = ("OK", f"已登录：{who}") if who else ("OK", "已登录")
            else:
                status, note = "NEEDS_RELOGIN", "抖音在线校验未通过（会话失效或被风控）"
    if status != "NEEDS_RELOGIN" and _douyin_recent_f2_ok(db):
        note = f"{note}；F2 采集近期成功"
    return status, note


def _check_xhs(cookie: str) -> tuple[str, str]:
    if not _SESSION_KEYS["xhs"][0] in _cookie_keys(cookie):
        return "NEEDS_RELOGIN", "凭证缺 web_session（未登录或导出不完整）"
    resp = _probe(
        "https://edith.xiaohongshu.com/api/sns/web/v1/user/selfinfo",
        cookie=cookie,
        extra_headers={
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.xiaohongshu.com",
            "Referer": "https://www.xiaohongshu.com/",
        },
    )
    if resp is None:
        return "WARN", "有 web_session 但网络不可达，未完成在线校验"
    if resp.status_code == 200:
        try:
            payload = resp.json()
        except ValueError:
            return "UNKNOWN", "小红书响应不是 JSON，无法判定"
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and (data.get("user_id") or data.get("nickname") or data.get("nick_name")):
            who = str(data.get("nickname") or data.get("nick_name") or data.get("user_id") or "").strip()
            return ("OK", f"已登录：{who}") if who else ("OK", "已登录")
        return "NEEDS_RELOGIN", "小红书在线校验未通过（会话失效）"
    # 平台对未签名请求一律 406 拒绝（实测有无 Cookie 响应相同），无法在线区分登录态——如实降级
    return "WARN", "有 web_session 凭证；平台接口拒绝未签名请求，无法在线判定登录态（以采集管道实际运行为准）"


def _check_twitter(cookie: str) -> tuple[str, str]:
    keys = _cookie_keys(cookie)
    if "auth_token" not in keys:
        return "NEEDS_RELOGIN", "凭证缺 auth_token（未登录或导出不完整）"
    csrf = ""
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "ct0":
            csrf = value.strip()
            break
    headers = {"Authorization": _TWITTER_BEARER}
    if csrf:
        headers["x-csrf-token"] = csrf
    resp = _probe(
        "https://api.x.com/1.1/account/verify_credentials.json",
        cookie=cookie,
        extra_headers=headers,
    )
    if resp is None:
        return "WARN", "有 auth_token 但网络不可达（该站通常需代理），未完成在线校验"
    if resp.status_code != 200:
        return "NEEDS_RELOGIN", f"推特在线校验未通过（HTTP {resp.status_code}）"
    try:
        data = resp.json()
    except ValueError:
        return "OK", "已登录（HTTP 200，响应体异常）"
    screen = str(data.get("screen_name") or "").strip()
    return ("OK", f"已登录：@{screen}") if screen else ("OK", "已登录")


_CHECKERS = {
    "bilibili": _check_bilibili,
    "douyin": _check_douyin,
    "xhs": _check_xhs,
    "twitter": _check_twitter,
}


def _douyin_recent_f2_ok(db: Session) -> bool:
    """F2 个人来源轮询最近 60 分钟成功 = 抖音登录态的旁证。"""
    log = health_repo.latest_poll_logs(db).get("douyin-personal")
    if log is None or log.status != "succeeded":
        return False
    try:
        finished = datetime.fromisoformat(str(log.finished_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - finished <= timedelta(minutes=60)


# ---------- 对外入口 ----------
def check_platform(db: Session, platform: str) -> CookieHealth:
    """立即校验单平台登录态，并把结果落库（cookies 表）。"""
    if platform not in PLATFORMS:
        raise validation("PLATFORM_NOT_SUPPORTED")
    cookie = _read_stored_cookie(platform)
    if not cookie:
        status, note = "NEEDS_RELOGIN", "未配置登录凭证（请在下方切换账号处粘贴 Cookie）"
    else:
        if platform == "douyin":
            status, note = _check_douyin(db, cookie)
        else:
            status, note = _CHECKERS[platform](cookie)
    row = health_repo.upsert_cookie(db, platform, status, note=note, last_checked_at=now_iso())
    db.commit()
    return CookieHealth.model_validate(row)


def check_all_platforms(db: Session) -> list[CookieHealth]:
    return [check_platform(db, platform) for platform in PLATFORMS]


def switch_account(db: Session, platform: str, cookie: str) -> CookieHealth:
    """切换账号：覆写该平台 Cookie 存储并立即校验；失败保持诚实状态。"""
    if platform not in PLATFORMS:
        raise validation("PLATFORM_NOT_SUPPORTED")
    text = " ".join(cookie.split())  # 粘贴串可能带换行；Cookie 头应为单行
    if len(text) < 20:
        raise validation("COOKIE_TOO_SHORT")
    _write_stored_cookie(platform, text)
    return check_platform(db, platform)
