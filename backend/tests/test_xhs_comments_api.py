"""小红书评论采集 API：模拟数据端到端（提交 → 进度 → 结果 → 报告 → 图片 → 重试）。"""
from __future__ import annotations

import json
import struct
import time
import zlib
from pathlib import Path

import pytest

NOTE = "6aabbccd0000000000000022"
NOTE_PARTIAL = "6aabbccd0000000000000033"


def _png_bytes() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\xff\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _write_fixture(root: Path, note_id: str, pages: dict, image: bool = True) -> Path:
    directory = root / note_id
    (directory / "img").mkdir(parents=True, exist_ok=True)
    if image:
        (directory / "img" / "a.png").write_bytes(_png_bytes())
    (directory / "pages.json").write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")
    return directory


def _page_comment(
    cid: str,
    content: str,
    likes: int,
    *,
    nickname: str | None = None,
    sub_count: str = "0",
    pictures: list[str] | None = None,
    pinned: bool = False,
) -> dict:
    data = {
        "id": cid,
        "content": content,
        "like_count": str(likes),
        "create_time": 1785000000000,
        "user_info": {"nickname": nickname or f"用户{cid}"},
        "sub_comment_count": sub_count,
    }
    if pictures:
        data["pictures"] = [{"url_default": url} for url in pictures]
    if pinned:
        data["is_pinned"] = True
    return data


def _wait_job(client, headers, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    job = {}
    while time.time() < deadline:
        response = client.get(f"/api/v1/xhs-comments/jobs/{job_id}", headers=headers)
        assert response.status_code == 200
        job = response.json()["data"]
        if job["status"] in {"completed", "incomplete", "failed"}:
            return job
        time.sleep(0.2)
    raise AssertionError(f"job did not finish: {job}")


@pytest.fixture()
def fixtures_dir(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "fixtures"
    root.mkdir()
    monkeypatch.setenv("XHS_COMMENT_FIXTURE_DIR", str(root))
    return root


def test_collect_fixture_end_to_end(client, auth_headers, fixtures_dir):
    _write_fixture(
        fixtures_dir,
        NOTE,
        {
            "note": {"title": "模拟作品：评论验收", "author": "模拟博主", "platform_total": 8},
            "pages": [
                {
                    "comments": [
                        _page_comment("c1", "置顶评论", 5, sub_count="2", pinned=True),
                        _page_comment("c2", "最高赞评论", 99, sub_count="1"),
                        _page_comment("c3", "", 0, nickname="语音用户"),
                        _page_comment(
                            "c4",
                            "带图评论",
                            7,
                            pictures=["https://img.example/a.png", "https://img.example/missing.png"],
                        ),
                    ],
                    "has_more": True,
                    "cursor": "p2",
                    "total": 8,
                },
                {
                    "comments": [
                        _page_comment("c2", "重复出现的高赞评论", 99, sub_count="1"),
                        _page_comment("c5", "第二页评论", 3),
                    ],
                    "has_more": False,
                    "total": 8,
                },
            ],
            "replies": [
                {
                    "parent_id": "c1",
                    "comments": [{"id": "r1", "content": "一级回复", "like_count": "2", "user_info": {"nickname": "回复A"}}],
                    "has_more": False,
                },
                {
                    "parent_id": "c1",
                    "comments": [
                        {
                            "id": "r2",
                            "content": "回复的回复",
                            "like_count": "1",
                            "target_comment_id": "r1",
                            "user_info": {"nickname": "回复B"},
                        }
                    ],
                    "has_more": False,
                },
                {
                    "parent_id": "c2",
                    "comments": [{"id": "r3", "content": "高赞楼回复", "like_count": "4", "user_info": {"nickname": "回复C"}}],
                    "has_more": False,
                },
            ],
            "voice": {
                "status": "extracted",
                "rows": [
                    {"comment_id": "c3", "text": "平台自带语音转写", "nickname": "语音用户"},
                    {"nickname": "无法匹配用户", "text": "未关联转写"},
                ],
            },
            "image_files": {"https://img.example/a.png": "img/a.png"},
        },
    )

    response = client.post(
        "/api/v1/xhs-comments/jobs",
        json={
            "url": f"https://www.xiaohongshu.com/explore/{NOTE}",
            "include_images": True,
            "include_voice": True,
            "voice_authorized": True,
        },
        headers=auth_headers,
    )
    assert response.status_code == 202
    created = response.json()["data"]
    assert created["mode"] == "fixture"
    assert created["status"] in {"queued", "running"}
    assert created["voice_authorized"] is True
    assert created["voice_allow_open_page"] is False

    job = _wait_job(client, auth_headers, created["job_id"])
    assert job["status"] == "completed", job
    assert job["progress"]["pages_fetched"] == 2
    assert job["progress"]["main_comments"] >= 5
    assert job["progress"]["images_done"] == 1
    assert job["progress"]["images_failed"] == 1

    result_response = client.get(f"/api/v1/xhs-comments/notes/{NOTE}", headers=auth_headers)
    assert result_response.status_code == 200
    result = result_response.json()["data"]
    assert result["collector"]["mode"] == "fixture"
    assert result["completeness"]["status"] == "complete"
    assert result["completeness"]["page_end_reached"] is True
    assert result["completeness"]["platform_total"] == 8
    assert result["counts"]["total_comments"] == 8
    assert result["counts"]["main_comments"] == 5
    assert result["counts"]["replies"] == 3
    assert result["counts"]["images_downloaded"] == 1
    assert result["counts"]["images_failed"] == 1
    assert "image_download_failed" in result["completeness"]["warnings"]

    comments = {c["comment_id"]: c for c in result["comments"]}
    assert comments["c2"]["content"] == "最高赞评论"
    assert [c["comment_id"] for c in result["comments"] if not c["parent_comment_id"]] == [
        "c2",
        "c4",
        "c1",
        "c5",
        "c3",
    ]
    assert comments["c1"]["pinned"]["status"] == "confirmed"
    assert result["pinned_comment_id"] == "c1"
    assert comments["r2"]["parent_comment_id"] == "r1"
    assert comments["c3"]["voice"]["status"] == "transcript_available"
    assert comments["c3"]["voice"]["association"] == "exact"
    assert result["voice_run"]["unassociated"][0]["text"] == "未关联转写"
    assert result["voice_run"]["page_mode"] == "fixture"
    assert result["voice_run"]["opened_new_page"] is False
    assert comments["c4"]["pictures"][0]["status"] == "downloaded"
    assert comments["c4"]["pictures"][1]["status"] == "failed"

    report = client.get(f"/api/v1/xhs-comments/notes/{NOTE}/report.md", headers=auth_headers)
    assert report.status_code == 200
    assert "模拟作品：评论验收" in report.text
    assert "平台明确置顶" in report.text
    assert "本次已关联" in report.text
    assert "AI 内容识别未接入" in report.text

    image = client.get(f"/api/v1/xhs-comments/notes/{NOTE}/images/c_c4_1.png", headers=auth_headers)
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content.startswith(b"\x89PNG")

    listing = client.get("/api/v1/xhs-comments/notes", headers=auth_headers)
    assert listing.status_code == 200
    items = listing.json()["data"]["items"]
    assert any(item["note_id"] == NOTE for item in items)

    # 刷新后结果仍可查看，返回内容稳定
    again = client.get(f"/api/v1/xhs-comments/notes/{NOTE}", headers=auth_headers).json()["data"]
    assert again["comments"] == result["comments"]
    assert again["completeness"]["status"] == "complete"

    # 重试：需重新授权语音提取；不带语音提取时仍保留上次转写，评论不重复
    unauthorized_retry = client.post(f"/api/v1/xhs-comments/jobs/{created['job_id']}/retry", headers=auth_headers)
    assert unauthorized_retry.status_code == 422
    assert unauthorized_retry.json()["message"] == "VOICE_AUTHORIZATION_REQUIRED"
    retry = client.post(
        f"/api/v1/xhs-comments/jobs/{created['job_id']}/retry",
        json={"voice_authorized": True},
        headers=auth_headers,
    )
    assert retry.status_code == 202
    retry_job = _wait_job(client, auth_headers, retry.json()["data"]["job_id"])
    assert retry_job["status"] == "completed"
    merged = client.get(f"/api/v1/xhs-comments/notes/{NOTE}", headers=auth_headers).json()["data"]
    assert merged["counts"]["total_comments"] == 8
    assert len({c["comment_id"] for c in merged["comments"]}) == 8
    assert next(c for c in merged["comments"] if c["comment_id"] == "c3")["voice"]["text"] == "平台自带语音转写"
    assert merged["first_collected_at"] == result["first_collected_at"]
    assert len(merged["collection_history"]) == 2

    # 不带语音提取再次采集：沿用上次转写，评论仍然不重复
    no_voice = client.post(
        "/api/v1/xhs-comments/jobs",
        json={
            "url": f"https://www.xiaohongshu.com/explore/{NOTE}",
            "include_images": True,
            "include_voice": False,
        },
        headers=auth_headers,
    )
    assert no_voice.status_code == 202
    assert _wait_job(client, auth_headers, no_voice.json()["data"]["job_id"])["status"] == "completed"
    kept = client.get(f"/api/v1/xhs-comments/notes/{NOTE}", headers=auth_headers).json()["data"]
    assert kept["counts"]["total_comments"] == 8
    assert next(c for c in kept["comments"] if c["comment_id"] == "c3")["voice"]["text"] == "平台自带语音转写"
    assert kept["voice_run"]["status"] == "not_requested"
    assert kept["first_collected_at"] == result["first_collected_at"]
    assert len(kept["collection_history"]) == 3


def test_interrupted_pagination_is_marked_incomplete(client, auth_headers, fixtures_dir):
    _write_fixture(
        fixtures_dir,
        NOTE_PARTIAL,
        {
            "note": {"title": "模拟作品：分页中断", "author": "模拟博主", "platform_total": 4},
            "pages": [
                {"comments": [_page_comment("p1", "第一页评论", 6)], "has_more": True, "cursor": "p2", "total": 4},
                {"comments": [], "has_more": True, "error": "network_timeout"},
            ],
            "image_files": {},
        },
    )
    response = client.post(
        "/api/v1/xhs-comments/jobs",
        json={"url": f"https://www.xiaohongshu.com/explore/{NOTE_PARTIAL}", "include_images": False},
        headers=auth_headers,
    )
    assert response.status_code == 202
    job = _wait_job(client, auth_headers, response.json()["data"]["job_id"])
    assert job["status"] == "incomplete", job
    result = client.get(f"/api/v1/xhs-comments/notes/{NOTE_PARTIAL}", headers=auth_headers).json()["data"]
    assert result["completeness"]["status"] == "incomplete"
    assert "network_timeout" in result["completeness"]["reasons"]
    assert result["completeness"]["page_end_reached"] is False
    assert result["completeness"]["pages_fetched"] == 1
    assert result["counts"]["main_comments"] == 1


def test_voice_extraction_requires_explicit_authorization(client, auth_headers, fixtures_dir):
    response = client.post(
        "/api/v1/xhs-comments/jobs",
        json={
            "url": f"https://www.xiaohongshu.com/explore/{NOTE}",
            "include_voice": True,
            "voice_authorized": False,
        },
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["message"] == "VOICE_AUTHORIZATION_REQUIRED"


def test_voice_allow_open_page_requires_authorization_and_is_inherited_on_retry(
    client, auth_headers, fixtures_dir
):
    note = "6aabbccd0000000000000055"
    _write_fixture(
        fixtures_dir,
        note,
        {
            "note": {"title": "模拟作品：允许新开页", "author": "模拟博主"},
            "pages": [{"comments": [_page_comment("v1", "普通评论", 1)], "has_more": False, "total": 1}],
            "voice": {"status": "extracted", "rows": []},
            "image_files": {},
        },
    )
    invalid = client.post(
        "/api/v1/xhs-comments/jobs",
        json={
            "url": f"https://www.xiaohongshu.com/explore/{note}",
            "include_voice": False,
            "voice_authorized": True,
            "voice_allow_open_page": True,
        },
        headers=auth_headers,
    )
    assert invalid.status_code == 422
    assert invalid.json()["message"] == "VOICE_AUTHORIZATION_REQUIRED"

    created = client.post(
        "/api/v1/xhs-comments/jobs",
        json={
            "url": f"https://www.xiaohongshu.com/explore/{note}",
            "include_voice": True,
            "voice_authorized": True,
            "voice_allow_open_page": True,
        },
        headers=auth_headers,
    )
    assert created.status_code == 202
    job = created.json()["data"]
    assert job["voice_authorized"] is True
    assert job["voice_allow_open_page"] is True
    assert _wait_job(client, auth_headers, job["job_id"])["status"] == "completed"

    retry = client.post(
        f"/api/v1/xhs-comments/jobs/{job['job_id']}/retry",
        json={"voice_authorized": True},
        headers=auth_headers,
    )
    assert retry.status_code == 202
    retry_job = retry.json()["data"]
    assert retry_job["voice_authorized"] is True
    assert retry_job["voice_allow_open_page"] is True
    assert _wait_job(client, auth_headers, retry_job["job_id"])["status"] == "completed"


def test_comment_endpoints_require_auth(client):
    assert client.get("/api/v1/xhs-comments/notes").status_code == 401


def test_unknown_result_and_report_are_not_found(client, auth_headers):
    assert client.get("/api/v1/xhs-comments/notes/6aabbccd0000000000000099", headers=auth_headers).status_code == 404
    assert client.get("/api/v1/xhs-comments/notes/6aabbccd0000000000000099/report.md", headers=auth_headers).status_code == 404


def test_image_path_cannot_escape_note_directory(client, auth_headers):
    from app.core.errors import AppError
    from app.services.xhs_comment_service import xhs_comment_service

    with pytest.raises(AppError):
        xhs_comment_service.image_file("..", "comments.json")
    with pytest.raises(AppError):
        xhs_comment_service.image_file(NOTE, "../comments.json")
