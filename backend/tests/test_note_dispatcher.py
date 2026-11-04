"""AI 笔记消费端测试：包扫描、优先级、执行契约与不静默失败。

背景（2026-11-01 用户确认）：workbuddy/pending 积压无消费者。铁律回归：
完成=文件真的存在（绝不伪造 done）；失败留 *.error.json 且 attempts 累计；
executor 不可用时只标 dispatched 并说明，绝不调回填。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monitor import note_dispatcher


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """独立工作区：pending 目录 + summary_targets 配置隔离。"""
    (tmp_path / "workbuddy" / "pending").mkdir(parents=True)
    (tmp_path / ".run").mkdir(parents=True)
    from app.core.config import settings as app_settings

    # 服务层（summary_target_service/note_service）读全局 settings.workspace_root，
    # 与本测试的 tmp_path 对齐，避免写进 conftest 的临时目录
    monkeypatch.setattr(app_settings, "workspace_root", tmp_path)
    # pending_dir/workbuddy_dir 在 settings 初始化时算好，需一并指向 tmp_path
    monkeypatch.setattr(app_settings, "workbuddy_dir", tmp_path / "workbuddy")
    monkeypatch.setattr(app_settings, "pending_dir", tmp_path / "workbuddy" / "pending")
    monkeypatch.setattr(
        "app.services.summary_target_service._settings_path",
        lambda: tmp_path / "config" / "summary_settings.json",
    )
    monkeypatch.setattr(note_dispatcher, "_work_facts", lambda work_id: {"source_type": "personal_source", "title": "GitHub开源神器盘点", "transcript_path": None})
    return tmp_path


def _write_package(workspace: Path, name: str, package: dict) -> Path:
    path = workspace / "workbuddy" / "pending" / name
    path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _package(work_id: int, **overrides) -> dict:
    data = {
        "work_id": work_id,
        "platform": "douyin",
        "title": "测试作品",
        "url": "https://example.com/video/1",
        "expected_output": f"Obsidian/待分组/2026-11-01-测试作品-{work_id}.md",
        "status": "pending",
        "target": "workbuddy",
    }
    data.update(overrides)
    return data


class TestScanPackages:
    def test_only_pending_returned(self, workspace):
        _write_package(workspace, "1.json", _package(1))
        _write_package(workspace, "2.json", _package(2, status="done"))
        (workspace / "workbuddy" / "pending" / "broken.json").write_text("{bad json", encoding="utf-8")
        entries = note_dispatcher.scan_packages(workspace)
        assert [e["package"]["work_id"] for e in entries] == [1]

    def test_directory_targets_scanned(self, workspace):
        monkey_target = workspace / "summary_targets" / "codex"
        (monkey_target / "pending").mkdir(parents=True)
        (monkey_target / "pending" / "9.json").write_text(
            json.dumps(_package(9, target="codex", expected_output="codex output/2026-11-01-九.md")),
            encoding="utf-8",
        )
        # 注册 codex 目录目标
        settings_file = workspace / "config" / "summary_settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(
            json.dumps({"default_target": "workbuddy", "targets": {"codex": {"kind": "directory", "root": "summary_targets/codex"}}}),
            encoding="utf-8",
        )
        entries = note_dispatcher.scan_packages(workspace)
        assert {e["package"]["work_id"] for e in entries} == {9}


class TestPriorityOrdering:
    def test_monitor_first_then_category_then_newest(self, workspace, monkeypatch):
        facts = {
            1: {"source_type": "monitor", "title": "博主监控作品", "transcript_path": None},
            2: {"source_type": "personal_source", "title": "GitHub开源神器", "transcript_path": None},
            3: {"source_type": "personal_source", "title": "随便拍着玩", "transcript_path": None},
            4: {"source_type": "personal_source", "title": "赚钱干货分享", "transcript_path": None},
        }
        monkeypatch.setattr(note_dispatcher, "_work_facts", lambda work_id: facts[work_id])
        for work_id in facts:
            _write_package(workspace, f"{work_id}.json", _package(work_id))
        entries = note_dispatcher.scan_packages(workspace)
        ordered = [e["package"]["work_id"] for e in sorted(entries, key=note_dispatcher.priority_key)]
        assert ordered == [1, 2, 4, 3]  # 监控 → ai_tools → money → other


class TestDispatchContract:
    def _config(self, command: tuple[str, ...]) -> note_dispatcher.DispatcherConfig:
        return note_dispatcher.DispatcherConfig(
            executor="fake", command=command, env_extra={}, timeout_seconds=30, max_attempts=3
        )

    def test_success_marks_done_and_completes(self, workspace, monkeypatch):
        path = _write_package(workspace, "10.json", _package(10))
        out_abs = note_dispatcher.expected_output_abs(workspace, _package(10))
        calls: list[tuple[int, str]] = []
        monkeypatch.setattr(
            note_dispatcher, "complete_note",
            lambda work_id, note_rel, executor, target: calls.append((work_id, note_rel)) or {},
        )
        entry = {"path": path, "package": json.loads(path.read_text(encoding="utf-8"))}
        # 假执行器：python -c 脚本创建 sys.argv[1]（out 路径，含父目录），忽略追加的 -p prompt
        fake = (
            "python3",
            "-c",
            "import sys, pathlib; p = pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True); p.touch()",
        )
        result = note_dispatcher.dispatch_one(entry, self._config(fake + (str(out_abs),)), workspace)
        assert result == "done"
        assert calls == [(10, "待分组/2026-11-01-测试作品-10.md")]  # Obsidian/ 前缀已剥离
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["status"] == "done"
        assert saved["dispatch"]["executor"] == "fake"

    def test_failure_keeps_pending_with_error_receipt(self, workspace, monkeypatch):
        path = _write_package(workspace, "11.json", _package(11))
        monkeypatch.setattr(
            note_dispatcher, "complete_note", lambda *a, **k: pytest.fail("失败时绝不该回填")
        )
        entry = {"path": path, "package": json.loads(path.read_text(encoding="utf-8"))}
        result = note_dispatcher.dispatch_one(entry, self._config(("false",)), workspace)
        assert result == "retry"
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["status"] == "pending"
        assert saved["dispatch"]["attempts"] == 1
        assert (path.parent / "11.error.json").is_file()

    def test_max_attempts_marks_failed(self, workspace, monkeypatch):
        path = _write_package(workspace, "12.json", _package(12, dispatch={"attempts": 2}))
        entry = {"path": path, "package": json.loads(path.read_text(encoding="utf-8"))}
        result = note_dispatcher.dispatch_one(entry, self._config(("false",)), workspace)
        assert result == "failed"
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["status"] == "failed"

    def test_no_executor_never_fakes_completion(self, workspace, monkeypatch):
        path = _write_package(workspace, "13.json", _package(13))
        monkeypatch.setattr(
            note_dispatcher, "complete_note", lambda *a, **k: pytest.fail("no-executor 绝不该回填")
        )
        entry = {"path": path, "package": json.loads(path.read_text(encoding="utf-8"))}
        config = note_dispatcher.DispatcherConfig(
            executor="none", command=(), env_extra={}, timeout_seconds=30, max_attempts=3
        )
        result = note_dispatcher.dispatch_one(entry, config, workspace)
        assert result == "skipped-no-executor"
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["status"] == "dispatched"
        assert "配置" in saved["dispatch"]["message"]


class TestReassign:
    """切换执行者：积压包搬队列（重建任务包 + 删旧文件）。"""

    def _fake_work(self, work_id: int):
        class _W:
            pass

        w = _W()
        w.id = work_id
        w.platform = "douyin"
        w.external_id = "vid"
        w.url = "https://example.com/video/1"
        w.title = "测试作品"
        w.published_at = None
        w.created_at = None
        w.media_path = None
        w.transcript_path = None
        w.creator = None
        w.group = None
        return w

    def _register_codex(self, workspace: Path) -> None:
        settings_file = workspace / "config" / "summary_settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(
            json.dumps({"targets": {"codex": {"kind": "directory", "root": "summary_targets/codex"}}}),
            encoding="utf-8",
        )

    def test_reassign_moves_package_and_recomputes_target(self, workspace, monkeypatch):
        self._register_codex(workspace)
        path = _write_package(workspace, "40.json", _package(40))
        monkeypatch.setattr(note_dispatcher, "_get_work", lambda work_id: self._fake_work(work_id))
        results = note_dispatcher.reassign(5, "codex", dry_run=False, workspace_root=workspace)
        assert results[0]["result"] == "moved"
        assert not path.exists()
        (new_path,) = (workspace / "summary_targets" / "codex" / "pending").glob("*.json")
        saved = json.loads(new_path.read_text(encoding="utf-8"))
        assert saved["target"] == "codex"
        assert saved["expected_output"].startswith("summary_targets/codex/output/")
        assert saved["status"] == "pending"

    def test_reassign_dry_run_and_same_target_skip(self, workspace, monkeypatch):
        self._register_codex(workspace)
        path = _write_package(workspace, "41.json", _package(41))
        monkeypatch.setattr(note_dispatcher, "_get_work", lambda work_id: self._fake_work(work_id))
        before = path.read_text(encoding="utf-8")
        dry = note_dispatcher.reassign(5, "codex", dry_run=True, workspace_root=workspace)
        assert dry[0]["result"] == "dry-run"
        assert path.read_text(encoding="utf-8") == before
        # 搬到包已在的队列：跳过不删
        same = note_dispatcher.reassign(5, "workbuddy", dry_run=False, workspace_root=workspace)
        assert same[0]["result"] == "skipped-same-target"
        assert path.exists()

    def test_reassign_missing_work_skipped(self, workspace, monkeypatch):
        self._register_codex(workspace)
        path = _write_package(workspace, "42.json", _package(42))
        monkeypatch.setattr(note_dispatcher, "_get_work", lambda work_id: None)
        results = note_dispatcher.reassign(5, "codex", dry_run=False, workspace_root=workspace)
        assert results[0]["result"] == "skipped-work-missing"
        assert path.exists()


class TestDryRun:
    def test_dry_run_does_not_mutate(self, workspace):
        path = _write_package(workspace, "20.json", _package(20))
        before = path.read_text(encoding="utf-8")
        results = note_dispatcher.run_once(max_n=5, dry_run=True, workspace_root=workspace)
        assert len(results) == 1
        assert results[0]["result"] == "dry-run"
        assert path.read_text(encoding="utf-8") == before


class TestNotePathForApi:
    def test_workbuddy_strips_obsidian_prefix(self, workspace):
        package = _package(30, expected_output="Obsidian/待分组/x.md")
        assert note_dispatcher.note_path_for_api(workspace, package) == "待分组/x.md"

    def test_directory_target_relative_to_output_root(self, workspace):
        settings_file = workspace / "config" / "summary_settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(
            json.dumps({"targets": {"codex": {"kind": "directory", "root": "summary_targets/codex"}}}),
            encoding="utf-8",
        )
        package = _package(31, target="codex", expected_output="summary_targets/codex/output/a.md")
        assert note_dispatcher.note_path_for_api(workspace, package) == "a.md"


class TestNoteFrameSelection:
    """笔记侧读帧：8–12 张、语义/场景优先、时间轴覆盖。"""

    def test_budget_adapts_to_manifest_size(self):
        assert note_dispatcher.note_frame_budget(0) == 0
        assert note_dispatcher.note_frame_budget(5) == 5
        assert note_dispatcher.note_frame_budget(8) == 8
        assert note_dispatcher.note_frame_budget(12) == 10
        assert note_dispatcher.note_frame_budget(60) == 12

    def test_semantic_and_scene_win_over_sample_and_timeline_is_covered(self):
        frames = [
            {"file": f"frames/frame_{i:04d}.jpg", "time_seconds": float(i * 10), "source": "sample", "score": 0.6}
            for i in range(1, 31)
        ]
        # 早期两张语义帧（分数不高）必须优先于同段 sample 帧
        frames[1] = {"file": "frames/frame_0002.jpg", "time_seconds": 10.0, "source": "semantic", "score": 0.55}
        frames[5] = {"file": "frames/frame_0006.jpg", "time_seconds": 60.0, "source": "scene", "score": 0.7}
        selected = note_dispatcher.select_note_frames(frames)
        assert len(selected) == note_dispatcher.note_frame_budget(len(frames)) == 12
        assert {f["source"] for f in selected} >= {"semantic", "scene"}
        times = [f["time_seconds"] for f in selected]
        assert times == sorted(times)
        # 12 张覆盖 0–290s：相邻间隔不超过约 2.5 个桶宽（保证不是全挤在前半段）
        assert max(times) >= 240

    def test_legacy_manifest_without_source_uses_fallback_flag(self):
        frames = [{"file": f"frames/frame_{i:04d}.jpg", "time_seconds": float(i), "hits": []} for i in range(1, 4)]
        selected = note_dispatcher.select_note_frames(frames, fallback=True, limit=3)
        assert len(selected) == 3
        assert all(note_dispatcher._frame_source(f, True) == "fallback" for f in selected)

    def test_build_frame_guide_lists_paths_and_mixed_sources(self, workspace, monkeypatch):
        from monitor.store.shared_db import settings as shared_settings

        media = workspace / "media"
        frames_dir = "未分组/测试/frames"
        (media / frames_dir).mkdir(parents=True)
        manifest = {
            "work_id": 12,
            "fallback": False,
            "frames": [
                {"file": f"frames/frame_{i:04d}.jpg", "time_seconds": float(i * 20), "source": "sample", "score": 0.6}
                for i in range(1, 25)
            ]
            + [{"file": "frames/frame_0025.jpg", "time_seconds": 480.0, "source": "semantic", "score": 0.95, "hits": [{"term": "深蹲"}]}],
        }
        (media / frames_dir / "frame_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(shared_settings, "media_root", media)
        guide = note_dispatcher.build_frame_guide({"frames_dir": frames_dir}, workspace)
        assert "总帧数 25，本次实读 12 张" in guide
        assert "media/未分组/测试/frames/frame_0025.jpg | 480.0s | semantic" in guide
        assert "命中: 深蹲" in guide

    def test_build_frame_guide_missing_manifest_is_explicit(self, workspace):
        guide = note_dispatcher.build_frame_guide({"frames_dir": "未分组/缺失/frames"}, workspace)
        assert "清单缺失" in guide
        assert "无帧可读" in guide
