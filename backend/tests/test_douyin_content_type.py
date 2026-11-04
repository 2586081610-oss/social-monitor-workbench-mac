"""抖音作品「视频 / 图文」判型：抓取 / 导入入口共用同一实现的回归测试。

判型真值字段见 `monitor/platforms/douyin_content.py` 模块注释；用例取自
`data/douyin/tanchengyi_822069263606973/pages/page_0*.json` 的真实字段组合。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

from monitor.platforms.douyin_content import infer_content_type  # noqa: E402

_PLAY_ADDR = {"play_addr": {"url_list": ["https://v.example/play.mp4"]}}


def _video_aweme(**extra) -> dict:
    """真实视频条目：aweme_type=0，media_type=4，images 缺失，有 play_addr。"""
    aweme = {"aweme_id": "7001", "aweme_type": 0, "media_type": 4, "images": None, "video": _PLAY_ADDR}
    aweme.update(extra)
    return aweme


def _image_aweme(count: int, *, media_type: int = 2, **extra) -> dict:
    """真实图文条目：aweme_type=68，media_type ∈ {2,42}，images 非空，且同样带 play_addr。"""
    aweme = {
        "aweme_id": "8001",
        "aweme_type": 68,
        "media_type": media_type,
        "images": [{"url_list": [f"https://i.example/{i}.jpg"]} for i in range(count)],
        "video": _PLAY_ADDR,  # 图文也带幻灯片视频地址，不能据此判型
    }
    aweme.update(extra)
    return aweme


def test_video_entry_is_video():
    assert infer_content_type(_video_aweme()) == "video"


def test_single_image_entry_is_image():
    assert infer_content_type(_image_aweme(1)) == "image"


def test_three_image_entry_is_image():
    assert infer_content_type(_image_aweme(3)) == "image"


def test_eight_image_entry_is_image():
    assert infer_content_type(_image_aweme(8)) == "image"


def test_media_type_42_is_image():
    assert infer_content_type(_image_aweme(3, media_type=42)) == "image"


def test_missing_fields_fall_back_to_video():
    """字段缺失/非法类型一律按 video 回退（不把正常视频误判成图文）。"""
    assert infer_content_type({}) == "video"
    assert infer_content_type(None) == "video"
    assert infer_content_type({"aweme_type": None, "media_type": None, "images": None}) == "video"
    assert infer_content_type({"aweme_type": 0, "media_type": "2", "images": {}}) == "video"
    assert infer_content_type({"aweme_type": 0, "media_type": 4, "images": []}) == "video"


def test_play_addr_with_images_is_image():
    """图文条目同样带 video.play_addr，必须仍判为 image。"""
    aweme = _image_aweme(3)
    assert aweme["video"]["play_addr"]["url_list"]
    assert infer_content_type(aweme) == "image"


def test_aweme_type_68_without_images_is_image():
    """images 缺失但 aweme_type=68 仍是明确图文信号。"""
    assert infer_content_type({"aweme_type": 68, "media_type": 4, "images": None}) == "image"


@pytest.mark.parametrize("count, media_type", [(1, 2), (3, 2), (8, 2), (3, 42)])
def test_f2_extract_work_uses_shared_classifier(count, media_type):
    """F2 取数脚本输出的 media_type 走同一判型函数。"""
    from monitor.platforms import f2_fetch

    work, _skip = f2_fetch.extract_work(_image_aweme(count, media_type=media_type))
    assert work["media_type"] == "image"


def test_mediacrawler_fetch_uses_shared_classifier(monkeypatch, tmp_path):
    """MediaCrawler 抓取子进程不再硬编码 content_type=video。"""
    monkeypatch.setenv("MEDIACRAWLER_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    path = _WORKSPACE_ROOT / "monitor/platforms/dy_mediacrawler_fetch.py"
    spec = importlib.util.spec_from_file_location("fixture_fetch_content_type", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.infer_content_type(_video_aweme()) == "video"
    assert module.infer_content_type(_image_aweme(3)) == "image"
