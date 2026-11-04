"""抖音作品类型判定（视频 / 图文）的唯一实现，供各抓取/导入入口共用。

判型真值来自抓取产物 `data/douyin/<sec_uid>/pages/page_0*.json` 的 `aweme_list`
（creator_id=4 共 174 条逐条核验：167 视频 + 7 图文）：

- 图文：`aweme_type=68`，`media_type ∈ {2, 42}`，`images` 非空（1/3/8 张）
- 视频：`aweme_type=0`，`media_type=4`，`images` 为空/缺失，`video.play_addr.url_list` 有值

注意：图文条目同样带 `video.play_addr`（抖音把图集合成幻灯片视频），
因此**禁止**用 `play_addr` 判型；`duration=0` 也只是辅助信号。

判定优先级（有正向图文信号才判图文）：
1. `images` 是非空列表 → image（权威信号，压过 `play_addr`）
2. `aweme_type == 68` → image
3. `media_type ∈ {2, 42}` → image
4. 其余一律 video

缺失/非法字段的回退：字段缺失、类型不符（如 `images` 是 None 或字典、
`media_type` 是字符串）时信号不成立，按 video 处理——这是更保守的回退，
避免把正常视频误判成图文（图文误判最坏只是走了视频下载路径）。
"""
from __future__ import annotations

IMAGE_AWEME_TYPES = frozenset({68})
IMAGE_MEDIA_TYPES = frozenset({2, 42})


def infer_content_type(aweme) -> str:
    """按真值字段优先级判定抖音作品是 "video" 还是 "image"。"""
    if not isinstance(aweme, dict):
        return "video"
    images = aweme.get("images")
    if isinstance(images, list) and images:
        return "image"
    if aweme.get("aweme_type") == 68:
        return "image"
    media_type = aweme.get("media_type")
    if isinstance(media_type, int) and media_type in IMAGE_MEDIA_TYPES:
        return "image"
    return "video"
