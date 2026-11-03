"""下载清晰度设置（/api/v1/settings/download-quality + monitor 运行时读取）。

关键约定：
- 默认值：B 站 64=720P（-q 64）、抖音 480p；未设置时 explicit=False；
- 非法档位 422 拒绝；脏值/读取失败回退默认，不流向下载器；
- B 站高清档（≥64）自动注入 platform_cookies.json 登录态（--auth 内联），
  缺失时匿名回退且必须留 warning 日志；低档不带凭据；
- 抖音 config.yml 的手动合法档位不被默认回退覆盖（仅显式设置才同步）；
- 新下载视频记录实际分辨率（DownloadResult.video_resolution + 日志）；
- B 站登录态只报告存在性，不回显 Cookie 内容。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

from app.models import AppSetting  # noqa: E402
from app.repositories import app_setting_repo  # noqa: E402

QUALITY_URL = "/api/v1/settings/download-quality"
BILIBILI_KEY = "download_quality_bilibili"
DOUYIN_KEY = "download_quality_douyin"


def _clear_quality_rows(db) -> None:
    for key in (BILIBILI_KEY, DOUYIN_KEY):
        row = db.get(AppSetting, key)
        if row is not None:
            db.delete(row)
    db.commit()


def _set_quality(db, key: str, value: str) -> None:
    app_setting_repo.set_value(db, key, value)
    db.commit()


# ---------- API：读取 / 保存 / 校验 / 鉴权 ----------

def test_get_download_quality_defaults(client, auth_headers, db):
    _clear_quality_rows(db)
    resp = client.get(QUALITY_URL, headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    # 默认值 = B 站 64=720P、抖音 480p（未设置 → explicit=False）
    assert data["bilibili"] == "64" and data["bilibili_explicit"] is False
    assert data["douyin"] == "480p" and data["douyin_explicit"] is False
    assert [opt["value"] for opt in data["bilibili_options"]] == ["16", "32", "64", "80"]
    assert [opt["value"] for opt in data["douyin_options"]] == ["480p", "720p", "1080p"]
    assert isinstance(data["bilibili_login_available"], bool)


def test_put_download_quality_persists_and_reflects(client, auth_headers, db):
    _clear_quality_rows(db)
    try:
        resp = client.put(QUALITY_URL, json={"bilibili": "80", "douyin": "720p"}, headers=auth_headers)
        assert resp.status_code == 200
        saved = resp.json()["data"]
        assert saved["bilibili"] == "80" and saved["bilibili_explicit"] is True
        assert saved["douyin"] == "720p" and saved["douyin_explicit"] is True

        data = client.get(QUALITY_URL, headers=auth_headers).json()["data"]
        assert data["bilibili"] == "80" and data["douyin"] == "720p"
        assert db.get(AppSetting, BILIBILI_KEY).value == "80"
        assert db.get(AppSetting, DOUYIN_KEY).value == "720p"
    finally:
        _clear_quality_rows(db)


def test_put_download_quality_rejects_illegal_values(client, auth_headers, db):
    _clear_quality_rows(db)
    try:
        for payload in ({"bilibili": "999"}, {"bilibili": "highest"}, {"douyin": "4k"},
                        {"douyin": "lowest"}, {"douyin": ""}, {"bilibili": ""}):
            resp = client.put(QUALITY_URL, json=payload, headers=auth_headers)
            assert resp.status_code == 422, f"{payload} 应被拒绝"
        # 大小写/首尾空白归一化后合法
        resp = client.put(QUALITY_URL, json={"douyin": " 720P "}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["douyin"] == "720p"
        # 非法值未落库（bilibili 保持默认）
        assert db.get(AppSetting, BILIBILI_KEY) is None
    finally:
        _clear_quality_rows(db)


def test_download_quality_requires_admin(client):
    assert client.get(QUALITY_URL).status_code == 401
    assert client.put(QUALITY_URL, json={"bilibili": "80"}).status_code == 401


# ---------- 纯函数与只读读取：脏值回退 / 绝不写入 ----------

def test_quality_from_raw_falls_back_on_dirty_values():
    from app.services.download_quality_service import quality_from_raw

    state = quality_from_raw({BILIBILI_KEY: "999", DOUYIN_KEY: "lowest"})
    assert state == {
        "bilibili": "64", "douyin": "480p",
        "bilibili_explicit": False, "douyin_explicit": False,
    }
    state = quality_from_raw({BILIBILI_KEY: " 64 ", DOUYIN_KEY: "1080P"})
    assert state["bilibili"] == "64" and state["bilibili_explicit"] is True
    assert state["douyin"] == "1080p" and state["douyin_explicit"] is True


def test_read_quality_readonly_never_writes(tmp_path):
    import sqlite3

    from app.services.download_quality_service import read_quality_readonly

    missing = tmp_path / "missing.db"
    state = read_quality_readonly(missing)
    assert state["bilibili"] == "64" and state["douyin"] == "480p"
    assert not missing.exists()

    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    before = empty.stat().st_size
    state = read_quality_readonly(empty)
    assert state["bilibili"] == "64" and state["douyin"] == "480p"
    assert empty.stat().st_size == before  # 不建表

    full = tmp_path / "full.db"
    with sqlite3.connect(full) as con:
        con.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
        con.execute(f"INSERT INTO app_settings VALUES ('{BILIBILI_KEY}', '64')")
        con.execute(f"INSERT INTO app_settings VALUES ('{DOUYIN_KEY}', '1080p')")
    state = read_quality_readonly(full)
    assert state["bilibili"] == "64" and state["douyin"] == "1080p"
    assert state["bilibili_explicit"] is True and state["douyin_explicit"] is True


# ---------- monitor 下载路径：运行时读取与档位回退 ----------

def _capture_cli(monkeypatch, module) -> list[list[str]]:
    commands: list[list[str]] = []
    monkeypatch.setattr(module, "_run_cli", lambda _platform, command, **_kwargs: commands.append(command))
    return commands


def test_bilibili_command_uses_setting_quality(db, monkeypatch, tmp_path):
    from monitor.pipeline import download

    monkeypatch.setattr(download, "_load_platform_cookies", lambda: {})  # 隔离真实 cookie 文件
    monkeypatch.setattr(download, "_project_root", lambda: tmp_path)  # 隔离真实 yutto_auth.toml
    _set_quality(db, BILIBILI_KEY, "80")
    try:
        commands = _capture_cli(monkeypatch, download)
        download._invoke_downloader("bilibili", "https://www.bilibili.com/video/BV1xx411c7mD", tmp_path)
        command = commands[0]
        assert command[command.index("-q") + 1] == "80"
    finally:
        _clear_quality_rows(db)


def test_bilibili_quality_falls_back_to_default_without_setting(db, monkeypatch, tmp_path):
    from monitor.pipeline import download

    monkeypatch.setattr(download, "_load_platform_cookies", lambda: {})
    monkeypatch.setattr(download, "_project_root", lambda: tmp_path)
    _clear_quality_rows(db)
    commands = _capture_cli(monkeypatch, download)
    download._invoke_downloader("bilibili", "https://www.bilibili.com/video/BV1xx411c7mD", tmp_path)
    command = commands[0]
    assert command[command.index("-q") + 1] == "64"  # 未设置时默认 720P


# ---------- B 站登录态接入（platform_cookies.json → yutto --auth 内联） ----------

_FAKE_BILIBILI_COOKIE = (
    "buvid3=synthetic-tracker; SESSDATA=FAKESESSDATAVALUE; "
    "bili_jct=FAKEJCTVALUE; DedeUserID=123456; unrelated=synthetic"
)


def test_bilibili_high_tier_injects_inline_auth_from_cookies(db, monkeypatch, tmp_path):
    """≥720P 档 + cookie 含 SESSDATA → 命令行注入 --auth（只挑标准认证键）。"""
    from monitor.pipeline import download

    monkeypatch.setattr(download, "_load_platform_cookies",
                        lambda: {"bilibili": {"cookie": _FAKE_BILIBILI_COOKIE}})
    monkeypatch.setattr(download, "_project_root", lambda: tmp_path)
    _set_quality(db, BILIBILI_KEY, "80")
    try:
        commands = _capture_cli(monkeypatch, download)
        download._invoke_downloader("bilibili", "https://www.bilibili.com/video/BV1xx411c7mD", tmp_path)
        command = commands[0]
        assert "--auth" in command
        auth_value = command[command.index("--auth") + 1]
        assert "SESSDATA=FAKESESSDATAVALUE" in auth_value
        assert "bili_jct=FAKEJCTVALUE" in auth_value
        assert "DedeUserID=123456" in auth_value
        assert "buvid3" not in auth_value and "unrelated" not in auth_value  # 追踪键不传
    finally:
        _clear_quality_rows(db)


def test_bilibili_high_tier_without_login_warns_and_stays_anonymous(db, monkeypatch, tmp_path, caplog):
    """≥720P 档但无 cookie → 匿名下载且日志明确警告（不静默降档）。"""
    import logging as _logging

    from monitor.pipeline import download

    monkeypatch.setattr(download, "_load_platform_cookies", lambda: {})
    monkeypatch.setattr(download, "_project_root", lambda: tmp_path)
    _set_quality(db, BILIBILI_KEY, "80")
    try:
        commands = _capture_cli(monkeypatch, download)
        with caplog.at_level(_logging.WARNING, logger="monitor.pipeline.download"):
            download._invoke_downloader("bilibili", "https://www.bilibili.com/video/BV1xx411c7mD", tmp_path)
        command = commands[0]
        assert "--auth" not in command and "--auth-file" not in command
        warnings = [r for r in caplog.records if "login unavailable" in r.message]
        assert warnings, "匿名回退必须留下 warning 日志"
        assert "SESSDATA" not in caplog.text  # 日志不泄露凭据
    finally:
        _clear_quality_rows(db)


def test_bilibili_low_tier_does_not_attach_auth(db, monkeypatch, tmp_path):
    """360P/480P 匿名可下：即使有 cookie 也不注入 --auth（最小暴露面）。"""
    from monitor.pipeline import download

    monkeypatch.setattr(download, "_load_platform_cookies",
                        lambda: {"bilibili": {"cookie": _FAKE_BILIBILI_COOKIE}})
    monkeypatch.setattr(download, "_project_root", lambda: tmp_path)
    _set_quality(db, BILIBILI_KEY, "32")  # 显式 480P（默认已是 720P，档位需显式设置）
    try:
        commands = _capture_cli(monkeypatch, download)
        download._invoke_downloader("bilibili", "https://www.bilibili.com/video/BV1xx411c7mD", tmp_path)
        command = commands[0]
        assert "--auth" not in command
    finally:
        _clear_quality_rows(db)


def test_bilibili_explicit_auth_file_takes_priority(db, monkeypatch, tmp_path):
    """用户显式维护的 yutto_auth.toml 优先于 cookie 注入（向后兼容）。"""
    from monitor.pipeline import download

    monkeypatch.setattr(download, "_load_platform_cookies",
                        lambda: {"bilibili": {"cookie": _FAKE_BILIBILI_COOKIE}})
    monkeypatch.setattr(download, "_project_root", lambda: tmp_path)
    auth_file = tmp_path / "config" / "yutto_auth.toml"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text("[default]\nSESSDATA = 'another'\n", encoding="utf-8")
    _set_quality(db, BILIBILI_KEY, "80")
    try:
        commands = _capture_cli(monkeypatch, download)
        download._invoke_downloader("bilibili", "https://www.bilibili.com/video/BV1xx411c7mD", tmp_path)
        command = commands[0]
        assert "--auth-file" in command and "--auth" not in command
    finally:
        _clear_quality_rows(db)


# ---------- 实际分辨率可见性（ffprobe/ffmpeg 探测） ----------

def test_probe_video_resolution_reads_real_mp4(tmp_path):
    from tests.test_f2_round6_regressions import real_mp4_bytes

    from monitor.pipeline import download

    video = tmp_path / "sample.mp4"
    video.write_bytes(real_mp4_bytes())
    resolution = download._probe_video_resolution(video)
    # 合成 H.264 视频自带尺寸；ffmpeg 可用时必须解出分辨率（形如 WxH）
    assert resolution is None or ("x" in resolution and resolution.replace("x", "").isdigit())


def test_probe_video_resolution_returns_none_on_garbage(tmp_path):
    from monitor.pipeline import download

    junk = tmp_path / "garbage.mp4"
    junk.write_bytes(b"not a video at all" * 32)
    assert download._probe_video_resolution(junk) is None


def test_download_with_result_records_video_resolution(monkeypatch, tmp_path):
    """新下载的视频文件 → DownloadResult.video_resolution 记录实际分辨率并打日志。"""
    import logging as _logging

    from monitor.pipeline import download
    from tests.test_f2_round6_regressions import real_mp4_bytes

    monkeypatch.setattr(download, "_invoke_downloader",
                        lambda _p, _u, d: (d / "original.mp4").write_bytes(real_mp4_bytes()))
    monkeypatch.setattr(download, "_probe_video_resolution", lambda _path: "1920x1080")

    records: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    download.logger.addHandler(handler)
    try:
        result = download.download_with_result(
            "bilibili", "https://www.bilibili.com/video/BV1xx411c7mD", tmp_path / "media",
        )
        assert result.disposition == "downloaded"
        assert result.video_resolution == "1920x1080"
        assert any("actual resolution: 1920x1080" in m for m in records)
    finally:
        download.logger.removeHandler(handler)


def test_runtime_download_quality_falls_back_when_read_fails(monkeypatch):
    from app.services import download_quality_service
    from monitor.pipeline import download

    def boom(_db_path):
        raise RuntimeError("synthetic read failure")

    monkeypatch.setattr(download_quality_service, "read_quality_readonly", boom)
    state = download._runtime_download_quality()
    assert state == {
        "bilibili": "64", "douyin": "480p",
        "bilibili_explicit": False, "douyin_explicit": False,
    }


def _prepare_jiji(monkeypatch, tmp_path) -> Path:
    jiji_dir = tmp_path / "douyin-downloader"
    jiji_dir.mkdir()
    monkeypatch.setenv("DOUYIN_DOWNLOADER_DIR", str(jiji_dir))
    return jiji_dir / "config.yml"


def test_douyin_config_synced_when_explicitly_configured(db, monkeypatch, tmp_path):
    """显式设置 720p → 下载前 config.yml 同步为 720p（设置是流水线的事实源）。"""
    from monitor.pipeline import download

    cfg = _prepare_jiji(monkeypatch, tmp_path)
    cfg.write_text("video_quality: 480p\nthread: 3\n", encoding="utf-8")
    _set_quality(db, DOUYIN_KEY, "720p")
    try:
        _capture_cli(monkeypatch, download)
        download._invoke_downloader("douyin", "https://www.douyin.com/video/7123456789012345678", tmp_path)
        assert "video_quality: 720p" in cfg.read_text(encoding="utf-8")
    finally:
        _clear_quality_rows(db)


def test_douyin_manual_quality_not_overridden_by_default_fallback(db, monkeypatch, tmp_path):
    """无显式设置（回退默认 480p）→ 手动写入的 1080p 不被覆盖（老行为保持）。"""
    from monitor.pipeline import download

    cfg = _prepare_jiji(monkeypatch, tmp_path)
    cfg.write_text("video_quality: 1080p\nthread: 3\n", encoding="utf-8")
    _clear_quality_rows(db)
    try:
        _capture_cli(monkeypatch, download)
        download._invoke_downloader("douyin", "https://www.douyin.com/video/7123456789012345678", tmp_path)
        assert "video_quality: 1080p" in cfg.read_text(encoding="utf-8")
    finally:
        _clear_quality_rows(db)


def test_douyin_lowest_quality_still_fixed_with_runtime_default(db, monkeypatch, tmp_path):
    """config.yml 为 lowest → 用运行时档位（未设置时=480p）替换（老行为保持）。"""
    from monitor.pipeline import download

    cfg = _prepare_jiji(monkeypatch, tmp_path)
    cfg.write_text("video_quality: lowest\nthread: 3\n", encoding="utf-8")
    _clear_quality_rows(db)
    try:
        _capture_cli(monkeypatch, download)
        download._invoke_downloader("douyin", "https://www.douyin.com/video/7123456789012345678", tmp_path)
        assert "video_quality: 480p" in cfg.read_text(encoding="utf-8")
    finally:
        _clear_quality_rows(db)


def test_douyin_missing_config_created_with_runtime_quality(db, monkeypatch, tmp_path):
    """config.yml 缺失 → 用运行时档位生成（显式 1080p 时直接按设置创建）。"""
    from monitor.pipeline import download

    cfg = _prepare_jiji(monkeypatch, tmp_path)
    _set_quality(db, DOUYIN_KEY, "1080p")
    try:
        _capture_cli(monkeypatch, download)
        download._invoke_downloader("douyin", "https://www.douyin.com/video/7123456789012345678", tmp_path)
        text = cfg.read_text(encoding="utf-8")
        assert "video_quality: 1080p" in text and "lowest" not in text
    finally:
        _clear_quality_rows(db)


def test_dirty_setting_value_never_reaches_downloaders(db, monkeypatch, tmp_path):
    """库里的脏值（绕过 API 写入）→ 回退默认档，不流向下载器。"""
    from monitor.pipeline import download

    _set_quality(db, BILIBILI_KEY, "999")
    try:
        commands = _capture_cli(monkeypatch, download)
        download._invoke_downloader("bilibili", "https://www.bilibili.com/video/BV1xx411c7mD", tmp_path)
        command = commands[0]
        assert command[command.index("-q") + 1] == "64"
    finally:
        _clear_quality_rows(db)
