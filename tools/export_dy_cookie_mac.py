"""从 Mac 浏览器真实 profile 提取抖音 cookie，写入 MediaCrawler COOKIES（export_dy_cookie_edge.py 的 Mac 版）。

输出格式与 Windows 版一致：改写 tools/mediacrawler/config/base_config.py 的 COOKIES 行。
浏览器通道（--browser，默认按 edge → chrome → chromium 自动选择）：
  edge      ~/Library/Application Support/Microsoft Edge（需已装 Edge 且完全退出）
  chrome    ~/Library/Application Support/Google/Chrome（需完全退出 Chrome，Cmd+Q）
  chromium  Playwright 自带内核 + 专用 profile（tools/dy_profile/，首次在窗口内手动登录一次）
前提：所选浏览器里已登录抖音；使用真实 profile 时浏览器必须完全退出。
用法:
  python export_dy_cookie_mac.py --dry-run          # 只验证环境与目标文件，不启动浏览器
  python export_dy_cookie_mac.py [--browser chrome]
"""
import argparse
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "tools" / "mediacrawler" / "config" / "base_config.py"
DOUYIN_URLS = ["https://www.douyin.com", "https://douyin.com"]
KEY_COOKIE_NAMES = ("sessionid", "sid_tt", "odin_tt", "ttwid", "passport_csrf_token", "LOGIN_STATUS")


def resolve_browser(name: str | None) -> tuple[Path, str | None, str]:
    """返回 (profile 目录, playwright channel 或 None=自带 chromium, 说明)。"""
    candidates = [name] if name else ["edge", "chrome", "chromium"]
    for item in candidates:
        if item == "edge":
            ud = Path.home() / "Library" / "Application Support" / "Microsoft Edge"
            if ud.exists():
                return ud, "msedge", "Edge 真实 profile"
        elif item == "chrome":
            ud = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
            if ud.exists():
                return ud, "chrome", "Chrome 真实 profile"
        elif item == "chromium":
            return ROOT / "tools" / "dy_profile", None, "Playwright chromium 专用 profile（tools/dy_profile/）"
    print("[ERROR] 未找到可用浏览器 profile（可安装 Edge/Chrome，或用 --browser chromium）")
    sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mac 版抖音 cookie 导出（写入 MediaCrawler COOKIES）")
    parser.add_argument("--browser", choices=["edge", "chrome", "chromium"], default=None)
    parser.add_argument("--dry-run", action="store_true", help="只验证环境与目标文件，不启动浏览器")
    args = parser.parse_args()

    ud, channel, desc = resolve_browser(args.browser)
    print(f"[INFO] 浏览器通道: {desc} ({ud})")
    if not CONFIG.exists():
        print(f"[ERROR] MediaCrawler 未安装（缺 {CONFIG}）；先按 tools/README.md 克隆 tools/mediacrawler")
        return 1

    if args.dry_run:
        print("[OK] dry-run 通过：profile 与目标文件均可用；未启动浏览器、未读取/写入任何 cookie")
        return 0

    from playwright.sync_api import sync_playwright

    cookies = []
    with sync_playwright() as p:
        try:
            kwargs = dict(headless=False, args=["--no-proxy-server", "--disable-blink-features=AutomationControlled"])
            if channel:
                kwargs["channel"] = channel
            ctx = p.chromium.launch_persistent_context(str(ud), **kwargs)
        except Exception as e:
            print(f"[ERROR] 打开浏览器 profile 失败: {e}")
            print("提示: 使用真实 profile 时请先完全退出浏览器（Cmd+Q，菜单栏无浏览器进程）")
            return 2
        page = ctx.new_page()
        page.goto("https://www.douyin.com/", timeout=60000)
        page.wait_for_timeout(6000)  # 等登录态生效
        for u in DOUYIN_URLS:
            try:
                cookies.extend(ctx.cookies([u]))
            except Exception:
                pass
        ctx.close()

    if not cookies:
        print("[WARN] 未获取到 cookie（确认浏览器已登录抖音；chromium 通道首次需在窗口内手动登录）")
        return 3

    seen, unique = set(), []
    for c in cookies:
        if c["name"] not in seen:
            seen.add(c["name"])
            unique.append(c)
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in unique)
    keys = [c["name"] for c in unique]
    print(f"[OK] 导出 {len(unique)} 个 cookie（{date.today().isoformat()}）")
    print("关键字段:", [k for k in keys if k in KEY_COOKIE_NAMES])

    text = CONFIG.read_text(encoding="utf-8")
    new_text = re.sub(r'^COOKIES\s*=.*$', f'COOKIES = "{cookie_str}"', text, count=1, flags=re.M)
    if new_text == text:
        print("[WARN] 未匹配 COOKIES 行")
        return 4
    CONFIG.write_text(new_text, encoding="utf-8")
    print(f"[OK] 已写入 MediaCrawler COOKIES（{len(cookie_str)} 字符）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
