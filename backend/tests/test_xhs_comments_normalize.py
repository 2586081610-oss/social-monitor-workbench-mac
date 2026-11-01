"""小红书评论结构化的纯逻辑测试：去重、排序、置顶、完整性与合并。"""
from __future__ import annotations

from monitor.platforms import xhs_comments as core


def raw(comment_id: str, content: str = "", likes: int = 0, *, parent: str = "", **extra):
    data = {
        "id": comment_id,
        "content": content,
        "like_count": str(likes),
        "create_time": 1785000000000,
        "user_info": {"nickname": f"用户{comment_id}"},
        "sub_comment_count": "0",
        **extra,
    }
    if parent:
        data["target_comment_id"] = parent
    return data


def build(comments, *, reasons=None, page_end=True, sub_complete=True, total=None):
    return core.build_result(
        note_id="6aabbccd0000000000000011",
        source_url="https://www.xiaohongshu.com/explore/6aabbccd0000000000000011",
        comments=comments,
        note_meta={"title": "测试作品", "author": "测试博主"},
        collector={"mode": "fixture", "engine": "test"},
        pages_fetched=2,
        page_end_reached=page_end,
        platform_total=total,
        sub_pagination_complete=sub_complete,
        reasons=reasons or [],
    )


def test_dedupe_and_like_sorting_keep_parent_relations():
    comments = [
        core.normalize_comment(raw("c1", "主评论一", 3), platform_order=0),
        core.normalize_comment(raw("c2", "主评论二", 9), platform_order=1),
        core.normalize_comment(raw("c2", "重复行（内容更短）", 9), platform_order=1),
        core.normalize_comment(raw("r1", "回复", 2, parent="c1"), parent_comment_id="c1"),
    ]
    result = build(comments)
    by_id = {c["comment_id"]: c for c in result["comments"]}
    assert set(by_id) == {"c1", "c2", "r1"}
    assert by_id["r1"]["parent_comment_id"] == "c1"
    mains = [c["comment_id"] for c in result["comments"] if not c["parent_comment_id"]]
    assert mains == ["c2", "c1"]
    assert result["counts"]["total_comments"] == 3
    assert result["counts"]["replies"] == 1


def test_explicit_pinned_flag_is_confirmed_and_probable_is_inferred():
    comments = [
        core.normalize_comment(raw("c1", "置顶", 5, is_pinned=True), platform_order=0),
        core.normalize_comment(raw("c2", "最高赞", 99), platform_order=1),
    ]
    result = build(comments)
    assert result["pinned_comment_id"] == "c1"
    pinned = next(c for c in result["comments"] if c["comment_id"] == "c1")
    assert pinned["pinned"]["status"] == "confirmed"

    comments = [
        core.normalize_comment(raw("c1", "第一条但非最高", 5), platform_order=0),
        core.normalize_comment(raw("c2", "最高赞", 99), platform_order=1),
    ]
    result = build(comments)
    assert result["pinned_comment_id"] == "c1"
    pinned = next(c for c in result["comments"] if c["comment_id"] == "c1")
    assert pinned["pinned"]["status"] == "probable"
    assert "未获平台明确标识" in pinned["pinned"]["evidence"]


def test_picture_variants_are_extracted_in_order_without_duplicates():
    comment = core.normalize_comment(
        {
            "id": "c1",
            "content": "图",
            "pictures": [
                {"url_default": "https://img.example/a.jpg"},
                {"urlDefault": "https://img.example/b.jpg"},
                {"url_default": "https://img.example/a.jpg"},
            ],
        }
    )
    assert [p["url"] for p in comment["pictures"]] == [
        "https://img.example/a.jpg",
        "https://img.example/b.jpg",
    ]
    text_comment = core.normalize_comment(
        {"id": "c2", "content": "图", "pictures": "https://img.example/c.webp, https://img.example/d.webp"}
    )
    assert [p["url"] for p in text_comment["pictures"]] == [
        "https://img.example/c.webp",
        "https://img.example/d.webp",
    ]


def test_voice_association_is_exact_or_unique_heuristic_and_never_guessed():
    comments = [
        core.normalize_comment(raw("c1", "", 3, user_info={"nickname": "语音用户"})),
        core.normalize_comment(raw("c2", "普通评论", 1, user_info={"nickname": "同名"})),
        core.normalize_comment(raw("c3", "普通评论", 1, user_info={"nickname": "同名"})),
    ]
    rows = [
        {"comment_id": "c1", "text": "精确关联文本"},
        {"nickname": "同名", "likes": "1", "text": "同名不唯一，不能猜"},
        {"nickname": "语音用户", "likes": "3", "text": "昵称唯一但点赞不一致，不能猜"},
    ]
    attached, unassociated = core.associate_voice(comments, rows)
    assert attached == 1
    assert comments[0]["voice"]["status"] == "transcript_available"
    assert comments[0]["voice"]["association"] == "exact"
    assert len(unassociated) == 2


def test_completeness_requires_page_end_and_reply_consistency():
    comments = [
        core.normalize_comment(raw("c1", "主评论", 1, sub_comment_count="3"), platform_order=0),
        core.normalize_comment(raw("r1", "只有一条回复", 0), parent_comment_id="c1"),
    ]
    result = build(comments)
    assert result["completeness"]["status"] == "incomplete"
    assert "reply_incomplete" in result["completeness"]["reasons"]
    assert result["completeness"]["reply_shortfall"][0]["expected"] == 3

    result = build(comments, page_end=False, reasons=["page_interrupted"])
    assert result["completeness"]["status"] == "incomplete"
    assert core.reason_label("page_interrupted") in result["completeness"]["reason_texts"]


def test_failed_collection_keeps_reason_and_marks_failed():
    result = core.build_result(
        note_id="6aabbccd0000000000000011",
        source_url="https://www.xiaohongshu.com/explore/6aabbccd0000000000000011",
        comments=[],
        note_meta=None,
        collector={"mode": "live", "engine": "test"},
        pages_fetched=0,
        page_end_reached=False,
        platform_total=None,
        sub_pagination_complete=False,
        reasons=["login_required"],
        fatal_error="login_required",
    )
    assert result["completeness"]["status"] == "failed"
    assert result["completeness"]["reasons"] == ["login_required"]


def test_merge_is_idempotent_and_preserves_previous_voice_and_images(tmp_path):
    old = build(
        [
            core.normalize_comment(
                {
                    "id": "c1",
                    "content": "",
                    "like_count": "3",
                    "pictures": [{"url_default": "https://img.example/a.jpg"}],
                }
            )
        ]
    )
    old["comments"][0]["voice"] = {
        "status": "transcript_available",
        "text": "旧的平台转写",
        "source": "xhs_web_dom",
        "association": "heuristic",
        "evidence": "昵称唯一",
    }
    old["comments"][0]["pictures"][0].update(
        {"status": "downloaded", "local_path": "images/c_c1_1.jpg", "error": None}
    )
    new = build([core.normalize_comment({"id": "c1", "content": "", "like_count": "4"})])
    merged = core.merge_result(old, new)
    comment = merged["comments"][0]
    assert comment["like_count"] == 4
    assert comment["voice"]["text"] == "旧的平台转写"
    assert comment["voice"]["carried_from_previous"] is True
    assert comment["pictures"][0]["status"] == "downloaded"
    assert merged["counts"]["total_comments"] == 1
    assert merged["first_collected_at"] == old["first_collected_at"]
    assert len(merged["collection_history"]) == 2
    # 合并结果再次参与合并不会增加任何评论
    again = core.merge_result(merged, build([core.normalize_comment({"id": "c1", "content": "", "like_count": "4"})]))
    assert len(again["comments"]) == 1


def test_report_lists_all_mains_and_reply_status(tmp_path):
    comments = [
        core.normalize_comment(raw("c1", "主评论一", 5), platform_order=0),
        core.normalize_comment(raw("c2", "主评论二", 2), platform_order=1),
        core.normalize_comment(raw("r1", "回复一", 1), parent_comment_id="c1"),
        core.normalize_comment(
            {"id": "c3", "content": "", "pictures": [{"url_default": "https://img.example/a.jpg"}]}
        ),
    ]
    comments[3]["pictures"][0].update({"status": "failed", "error": "download_failed"})
    result = build(comments, page_end=False, reasons=["page_interrupted"])
    report = core.build_report(result)
    assert "主评论二" in report
    assert "回复（本次已关联 1 条" in report
    assert "图片未下载（download_failed）" in report
    assert "采集不完整" in report
    assert "AI 内容识别未接入" in report


def test_extract_picture_urls_dedupes_mirror_hosts_by_path():
    """同一张图在不同 CDN 镜像域（路径相同、域名不同）只保留一张。"""
    raw_data = raw(
        "c9",
        "带镜像重复图",
        1,
        pictures=[
            {"url_default": "https://sns-webpic-qc0.xhscdn.com/20261031/comment/1040g2h0abc!nd_whlt34_webp_wm_1"},
            {"url_default": "https://sns-webpic-qc1.xhscdn.com/20261031/comment/1040g2h0abc!nd_whlt34_webp_wm_1"},
            {"url_default": "https://sns-webpic-qc0.xhscdn.com/20261031/comment/1040g2h0def!nd_whlt34_webp_wm_2"},
        ],
    )
    urls = core.extract_picture_urls(raw_data)
    assert len(urls) == 2
    assert urls[0].endswith("wm_1")
    assert urls[1].endswith("wm_2")


def test_merge_duplicate_collapses_mirror_picture_entries():
    """跨页重复评论携带镜像 URL 时，合并后同一张图只剩一条记录。"""
    first = core.normalize_comment(
        raw("m1", "主评", 3, pictures=[{"url_default": "https://qc0.xhscdn.com/x/comment/imgA!nd.webp"}])
    )
    second = core.normalize_comment(
        raw("m1", "主评", 4, pictures=[{"url_default": "https://qc1.xhscdn.com/x/comment/imgA!nd.webp"}])
    )
    merged = core.dedupe_comments([first, second])
    assert len(merged) == 1
    assert len(merged[0]["pictures"]) == 1


def test_carry_forward_collapses_mirror_picture_entries():
    """重试合并时镜像 URL 视为同一张图：旧成果保留一份，不重复携带。"""
    old = core.normalize_comment(
        raw("m2", "主评二", 2, pictures=[{"url_default": "https://qc0.xhscdn.com/x/comment/imgB!nd.webp"}])
    )
    old["pictures"][0].update({"status": "downloaded", "local_path": "images/c_m2_1.webp"})
    new = core.normalize_comment(
        raw("m2", "主评二", 2, pictures=[{"url_default": "https://qc9.xhscdn.com/x/comment/imgB!nd.webp"}])
    )
    core._carry_forward(old, new)
    assert len(new["pictures"]) == 1
    assert new["pictures"][0]["status"] == "downloaded"


# ---------- 楼中楼被回复人（reply_to_nickname）解析 ----------

def test_reply_to_nickname_parsed_from_content_prefix():
    """子评论内容带「回复 @某人：」前缀 → 提取被回复人昵称；主评论不解析。"""
    reply = core.normalize_comment(
        raw("r9", "回复 @伤心荷包蛋：你说得对", parent="c1"), parent_comment_id="c1"
    )
    assert reply["reply_to_nickname"] == "伤心荷包蛋"
    # 内容保持原样，不改写
    assert reply["content"] == "回复 @伤心荷包蛋：你说得对"

    main = core.normalize_comment(raw("c9", "回复 @某人：主评论不该解析前缀"), platform_order=0)
    assert main["reply_to_nickname"] is None


def test_reply_to_nickname_prefers_explicit_api_field():
    """API 显式下发目标用户时优先于内容前缀。"""
    reply = core.normalize_comment(
        raw("r8", "回复 @内容里的名字：hi", parent="c1", target_user_nickname="API字段名"),
        parent_comment_id="c1",
    )
    assert reply["reply_to_nickname"] == "API字段名"


def test_reply_to_nickname_absent_for_plain_reply():
    """普通子评论（无前缀、无 API 字段）→ None，不猜测。"""
    reply = core.normalize_comment(raw("r7", "普通回复内容", parent="c1"), parent_comment_id="c1")
    assert reply["reply_to_nickname"] is None
