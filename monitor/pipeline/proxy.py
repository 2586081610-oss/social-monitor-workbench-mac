"""只读解析本机代理设置，供需要代理的平台子进程使用。

原则：
- 用户显式设置（TWITTER_PROXY / 标准代理环境变量）优先，原样使用；
- 未显式设置时在 macOS 上只读 `scutil --proxy` 的主机与端口（每次调用实时读取，
  本机代理可能变化；scutil 输出不含凭据，本模块也不读取或记录任何凭据）；
- 解析不到代理 → 返回 None，由调用方决定直连；
- 拒绝带用户信息的代理 URL；localhost 保持直连由调用方环境（NO_PROXY）保证，
  本模块不修改任何全局代理设置。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from urllib.parse import urlsplit

logger = logging.getLogger("monitor.pipeline.proxy")

_ALLOWED_SCHEMES = {"http", "https", "socks5", "socks5h", "socks4"}
_HOST_PATTERN = re.compile(r"^[0-9A-Za-z._-]+$")


def normalize_proxy_url(value: str) -> str | None:
    """校验并规范化代理 URL；不接受用户信息（凭据）与非常规主机名。"""
    value = (value or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = f"http://{value}"
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in _ALLOWED_SCHEMES
        or not host
        or not _HOST_PATTERN.fullmatch(host)
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not (1 <= port <= 65535)
    ):
        return None
    return f"{parsed.scheme.lower()}://{host}:{port}"


def resolve_platform_proxy(env_var: str = "TWITTER_PROXY") -> str | None:
    """按 显式环境变量 → 标准代理环境变量 → macOS 系统代理 顺序解析。"""
    explicit = os.getenv(env_var, "").strip()
    if explicit:
        normalized = normalize_proxy_url(explicit)
        if normalized is None:
            logger.warning("environment proxy %s is set but not a plain host:port URL; ignoring it", env_var)
        else:
            return normalized
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = os.getenv(name, "").strip()
        if value:
            normalized = normalize_proxy_url(value)
            if normalized is not None:
                return normalized
    if sys.platform == "darwin":
        return _macos_system_proxy()
    return None


def _macos_system_proxy() -> str | None:
    """只读 scutil --proxy 的主机/端口；输出不含凭据，也不落日志。"""
    try:
        completed = subprocess.run(
            ["scutil", "--proxy"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    data: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep:
            data[key.strip()] = value.strip()

    def _enabled(key: str) -> bool:
        return data.get(key) in {"1", "true", "TRUE"}

    def _endpoint(prefix: str, scheme: str) -> str | None:
        if not _enabled(f"{prefix}Enable"):
            return None
        host = data.get(f"{prefix}Proxy", "").strip()
        try:
            port = int(data.get(f"{prefix}Port", ""))
        except ValueError:
            return None
        if not host or not _HOST_PATTERN.fullmatch(host) or not (1 <= port <= 65535):
            return None
        return f"{scheme}://{host}:{port}"

    return (
        _endpoint("HTTP", "http")
        or _endpoint("HTTPS", "http")
        or _endpoint("SOCKS", "socks5")
    )
