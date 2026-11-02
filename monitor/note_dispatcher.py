"""AI 笔记消费端：取 pending 任务包 → 无头 AI CLI 生成笔记 → 校验回填。

原设计（SPEC §10）只"备料"：workbuddy/pending/*.json 素材就绪清单等
WorkBuddy App 定时自动化消费——Mac 上从未配置，积压 30+ 包。本模块补上
消费端（2026-11-01 用户确认的方案）：

  取包（按 [[monitor.triage]] 分诊优先级排序）→ 调用可配置的无头 AI CLI
  → 校验 expected_output 真的落盘 → 调 complete-note API 回填
  （work 置 DONE + provenance 回执）→ 包内状态推进 done。

自动化铁律（对齐 2026-11 Kavela 调研结论）：
  - 不静默失败：每次结果留痕（包内 dispatch 字段 + *.error.json 旁车）
  - 完成=文件真的存在：没生成就绝不回填；attempts 累计，超限标 failed
  - 单实例：flock 互斥防重叠；单并发逐包执行

执行器（executor）完全可配置（config/note_dispatcher.json）：
  - 默认 zcode：本机 ZCode.app 内置 CLI（Electron-as-Node 跑 zcode.cjs），
    `-p` 单发无头模式（默认 yolo 权限，见 zcode --help）
  - 兜底手动：executor=none 时只把包标 dispatched 并留提示，绝不伪造完成
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

POLL_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_MAX_ATTEMPTS = 3
LOCK_NAME = "note_dispatcher.lock"
CONFIG_NAME = "note_dispatcher.json"
_ZCODE_APP = Path("/Applications/ZCode.app/Contents/MacOS/ZCode")
_ZCODE_CJS = Path("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs")

PROMPT_TEMPLATE = """你是「博主监控·AI 笔记工作台」的笔记执行者。只完成下面这一个总结任务，不要做任何其他事。

任务包：{package_path}（相对当前目录；先读它）

步骤：
1. 读取任务包 JSON：transcript_path（逐句转写 JSON，含 text 字段）、frames_dir（抽帧清单 frame_manifest.json，可能不存在）、lexicon_hits、expected_output、url/title/blogger。
2. 素材相对当前目录：转写在 media/<transcript_path>。以转写文本为主依据；有抽帧清单可参考图片名。
3. 写一篇中文笔记到 expected_output 指定的相对路径（目录不存在就创建）。结构：
   - frontmatter：work_id / platform / blogger / title / url / date / tags
   - 正文：一句话摘要 → 要点提炼（3-6 条）→ 值得记录的细节 → 双层标签（#大标签 #小标签）
   - 素材信息不足就如实写少，绝不编造。
4. 只允许新建/覆盖这一个 .md 文件，不得改动其他任何文件。
完成后只输出：DONE <文件路径>
"""


@dataclass(frozen=True)
class DispatcherConfig:
    executor: str                  # zcode / codex / trae / none / 自定义名
    command: tuple[str, ...]       # 可执行与固定参数（prompt 按 prompt_flag 追加）
    env_extra: dict[str, str]
    timeout_seconds: int
    max_attempts: int
    prompt_flag: str = "-p"        # ""=prompt 作为位置参数（codex exec / trae-cli run）


def _default_zcode_command() -> tuple[tuple[str, ...], dict[str, str]] | None:
    """探测本机 ZCode 无头入口：PATH 上的 zcode 优先，否则 App 内置 CLI。"""
    if shutil.which("zcode"):
        return ("zcode",), {}
    if _ZCODE_APP.is_file() and _ZCODE_CJS.is_file():
        return (str(_ZCODE_APP), str(_ZCODE_CJS)), {"ELECTRON_RUN_AS_NODE": "1"}
    return None


def _config_from_spec(spec: dict[str, Any], *, default_executor: str, timeout: int, attempts: int) -> DispatcherConfig:
    """由一份执行器配置 dict 构建 DispatcherConfig（全局默认或单目标均可）。"""
    if spec.get("command"):
        return DispatcherConfig(
            executor=str(spec.get("executor") or default_executor),
            command=tuple(str(part) for part in spec["command"]),
            env_extra={str(k): str(v) for k, v in (spec.get("env") or {}).items()},
            timeout_seconds=int(spec.get("timeout_seconds", timeout)),
            max_attempts=int(spec.get("max_attempts", attempts)),
            prompt_flag=str(spec.get("prompt_flag", "-p")),
        )
    return DispatcherConfig(
        executor="none",
        command=(),
        env_extra={},
        timeout_seconds=int(spec.get("timeout_seconds", timeout)),
        max_attempts=int(spec.get("max_attempts", attempts)),
        prompt_flag=str(spec.get("prompt_flag", "-p")),
    )


def load_config(workspace_root: Path, target: str | None = None) -> DispatcherConfig:
    """读 config/note_dispatcher.json（可选）。

    结构：全局默认字段 + 可选 "targets": {<目标名>: {同款字段}}。
    指定 target 时优先用该目标的专属执行器配置（各执行者吃各自队列）；
    未配置且是内置 zcode/codex/trae 名时回退到探测逻辑，否则 none。
    """
    timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    max_attempts = DEFAULT_MAX_ATTEMPTS
    path = workspace_root / "config" / CONFIG_NAME
    overrides: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                overrides = loaded
        except (json.JSONDecodeError, OSError):
            overrides = {}
    timeout_seconds = int(overrides.get("timeout_seconds", timeout_seconds))
    max_attempts = int(overrides.get("max_attempts", max_attempts))
    if target:
        target_spec = (overrides.get("targets") or {}).get(target)
        if isinstance(target_spec, dict):
            return _config_from_spec(
                target_spec, default_executor=target, timeout=timeout_seconds, attempts=max_attempts
            )
        if target == "zcode":
            spec: dict[str, Any] = {"executor": "zcode"}
            detected = _default_zcode_command()
            if detected is not None:
                spec["command"] = list(detected[0])
                spec["env"] = detected[1]
            return _config_from_spec(spec, default_executor="zcode", timeout=timeout_seconds, attempts=max_attempts)
        if target == "codex":
            codex_bin = shutil.which("codex")
            spec = {"executor": "codex", "prompt_flag": ""}
            if codex_bin:
                spec["command"] = [
                    codex_bin, "exec", "--cd", str(workspace_root),
                    "--sandbox", "workspace-write", "--skip-git-repo-check",
                ]
            return _config_from_spec(spec, default_executor="codex", timeout=timeout_seconds, attempts=max_attempts)
        if target == "trae":
            trae_bin = shutil.which("trae-cli")
            spec = {"executor": "trae", "prompt_flag": ""}
            if trae_bin:
                spec["command"] = [trae_bin, "run"]
            return _config_from_spec(spec, default_executor="trae", timeout=timeout_seconds, attempts=max_attempts)
        return DispatcherConfig(
            executor="none", command=(), env_extra={}, timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
    # 全局默认（不带 target）：沿用既有行为
    executor = str(overrides.get("executor", "")).strip()
    if overrides.get("command"):
        return _config_from_spec(overrides, default_executor=executor or "custom", timeout=timeout_seconds, attempts=max_attempts)
    if executor in ("", "zcode", "auto"):
        detected = _default_zcode_command()
        if detected is None:
            executor, command, env_extra = "none", (), {}
        else:
            executor, (command, env_extra) = "zcode", detected
        return DispatcherConfig(
            executor=executor, command=command, env_extra=env_extra,
            timeout_seconds=timeout_seconds, max_attempts=max_attempts,
        )
    return DispatcherConfig(
        executor="none", command=(), env_extra={}, timeout_seconds=timeout_seconds, max_attempts=max_attempts
    )


def scan_packages(workspace_root: Path, target: str | None = None) -> list[dict[str, Any]]:
    """收集 pending 包：默认扫全部队列；指定 target 时只扫该目标的队列。

    目标绑定（见 docs/AI笔记执行者切换手册.md）：
      workbuddy / zcode → workbuddy/pending + summary_targets/zcode/pending；
      其他目标 → 各自 summary_targets/<名字>/pending。
    """
    from app.services import summary_target_service

    if target:
        if target in ("workbuddy", "zcode"):
            dirs = [Path(workspace_root) / "workbuddy" / "pending", Path(workspace_root) / "summary_targets" / "zcode" / "pending"]
        else:
            resolved = summary_target_service.resolve_target(target)
            dirs = [Path(resolved["pending_dir"])]
    else:
        dirs = [Path(workspace_root) / "workbuddy" / "pending"]
        try:
            for spec in summary_target_service.load_summary_settings()["targets"].values():
                if spec.get("kind") == "directory":
                    dirs.append(Path(workspace_root) / str(spec.get("root", "")) / "pending")
        except Exception:  # pragma: no cover - 配置损坏不影响 workbuddy 主目录扫描
            pass
    packages: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            if path in seen:
                continue
            seen.add(path)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, dict) and data.get("status") == "pending":
                packages.append({"path": path, "package": data})
    return packages


def _work_facts(work_id: int) -> dict[str, str | None]:
    """查作品的分诊事实（来源/标题/转写路径）；查不到如实返回空。"""
    from monitor.store.shared_db import SessionLocal

    from app.models import Work

    with SessionLocal() as db:
        work = db.get(Work, work_id)
        if work is None:
            return {"source_type": None, "title": None, "transcript_path": None}
        return {
            "source_type": work.source_type,
            "title": work.title,
            "transcript_path": work.transcript_path,
        }


def priority_key(entry: dict[str, Any]) -> tuple[int, int]:
    """排序键：分诊优先级升序，同优先级内 work_id 大（新）者优先。"""
    from monitor import triage

    package: dict[str, Any] = entry["package"]
    work_id = int(package.get("work_id") or 0)
    facts = _work_facts(work_id)
    transcript = ""
    if facts.get("transcript_path"):
        transcript = triage._read_transcript_text(
            _settings_media_root(), facts["transcript_path"]
        )
    result = triage.classify(facts.get("title") or package.get("title") or "", transcript_text=transcript)
    rank = triage.priority_rank(facts.get("source_type"), result.category)
    return (rank, -work_id)


def _settings_media_root() -> Path:
    from monitor.store.shared_db import settings

    return Path(settings.media_root)


def expected_output_abs(workspace_root: Path, package: dict[str, Any]) -> Path:
    """expected_output（相对工作区根）→ 绝对路径。"""
    rel = str(package.get("expected_output") or "").replace("\\", "/")
    if not rel:
        raise ValueError("expected_output 缺失")
    return (Path(workspace_root) / rel).resolve()


def note_path_for_api(workspace_root: Path, package: dict[str, Any]) -> str:
    """complete-note API 需要「相对匹配根」的路径：workbuddy 相对 Obsidian 根
    （即去掉 expected_output 的 Obsidian/ 前缀）；目录目标相对各自 output/ 根。"""
    rel = str(package.get("expected_output") or "").replace("\\", "/")
    target = str(package.get("target") or "workbuddy")
    if target == "workbuddy":
        prefix = "Obsidian/"
        return rel[len(prefix):] if rel.startswith(prefix) else rel
    from app.services import summary_target_service

    spec = summary_target_service.load_summary_settings()["targets"].get(target) or {}
    root = (Path(workspace_root) / str(spec.get("root", "")) / "output").resolve()
    absolute = expected_output_abs(workspace_root, package)
    try:
        return absolute.relative_to(root).as_posix()
    except ValueError as exc:  # 包指向了别的根：如实报错，不猜
        raise ValueError(f"expected_output 不在目标 {target} 的 output 根下") from exc


def build_prompt(package_path: Path) -> str:
    return PROMPT_TEMPLATE.format(package_path=package_path.as_posix())


def build_argv(config: DispatcherConfig, prompt: str) -> list[str]:
    """执行器 argv：prompt_flag 为 "" 时 prompt 作为位置参数（codex exec / trae-cli run）。"""
    if config.prompt_flag:
        return [*config.command, config.prompt_flag, prompt]
    return [*config.command, prompt]


def run_executor(
    config: DispatcherConfig, prompt: str, cwd: Path
) -> tuple[int, str]:
    """跑一次无头执行器，返回 (returncode, 合并输出尾部)。"""
    if not config.command:
        raise RuntimeError("执行器不可用（executor=none）：未探测到无头 AI CLI")
    env = dict(os.environ)
    env.update(config.env_extra)
    proc = subprocess.run(  # noqa: S603 - 固定配置命令，无用户可控片段
        build_argv(config, prompt),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=config.timeout_seconds,
        env=env,
        stdin=subprocess.DEVNULL,  # 无 TTY 环境防挂起（codex exec 已知坑）
    )
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return proc.returncode, output[-2000:]


def _rewrite_package(path: Path, package: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def complete_note(work_id: int, note_rel: str, executor: str, target: str) -> dict[str, Any]:
    """调聚合 API 回填完成（work DONE + provenance 回执）。"""
    from monitor.store.shared_db import settings

    import httpx

    base = str(getattr(settings, "aggregator_base_url", "") or "http://127.0.0.1:8000")
    response = httpx.post(
        f"{base}/api/v1/works/{work_id}/complete-note",
        headers={"Authorization": f"Bearer {settings.admin_token}"},
        json={"note_path": note_rel, "executor": executor, "target": target},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def dispatch_one(entry: dict[str, Any], config: DispatcherConfig, workspace_root: Path) -> str:
    """消费一个包，返回结果：done / failed / retry / skipped-no-executor。"""
    from app.utils.timeutil import now_iso

    path: Path = entry["path"]
    package: dict[str, Any] = entry["package"]
    work_id = int(package.get("work_id") or 0)
    target = str(package.get("target") or "workbuddy")
    attempts = int((package.get("dispatch") or {}).get("attempts") or 0) + 1

    if config.executor == "none":
        package["status"] = "dispatched"
        package["dispatch"] = {
            "executor": "none",
            "attempts": attempts,
            "message": "未探测到无头 AI CLI（zcode）；请在 config/note_dispatcher.json 配置 executor",
            "at": now_iso(),
        }
        _rewrite_package(path, package)
        return "skipped-no-executor"

    package["status"] = "dispatched"
    package["dispatch"] = {"executor": config.executor, "attempts": attempts, "at": now_iso()}
    _rewrite_package(path, package)

    out_abs = expected_output_abs(workspace_root, package)
    try:
        returncode, output = run_executor(config, build_prompt(path), workspace_root)
    except (subprocess.TimeoutExpired, RuntimeError, OSError) as exc:
        returncode, output = -1, f"{type(exc).__name__}: {exc}"

    if out_abs.is_file():
        note_rel = note_path_for_api(workspace_root, package)
        package["status"] = "done"
        package["dispatch"].update({"completed_at": now_iso(), "note_path": note_rel})
        _rewrite_package(path, package)
        try:
            complete_note(work_id, note_rel, config.executor, target)
        except Exception as exc:  # 回填失败不伪造成功：留痕并如实返回
            (path.parent / (path.stem + ".error.json")).write_text(
                json.dumps(
                    {"work_id": work_id, "stage": "complete_note", "error": str(exc), "at": now_iso()},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return "note-written-complete-failed"
        return "done"

    package["status"] = "failed" if attempts >= config.max_attempts else "pending"
    package["dispatch"].update({"failed_at": now_iso(), "returncode": returncode})
    _rewrite_package(path, package)
    (path.parent / (path.stem + ".error.json")).write_text(
        json.dumps(
            {"work_id": work_id, "attempts": attempts, "returncode": returncode, "output_tail": output},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return "failed" if attempts >= config.max_attempts else "retry"


def run_once(max_n: int, dry_run: bool, workspace_root: Path, target: str | None = None) -> list[dict[str, Any]]:
    """单轮消费：按优先级取包执行，返回每包结果（dry_run 只输出计划不落盘）。

    指定 target 时只消费该目标的队列，并用该目标专属执行器（各执行者互不越界）。
    """
    from monitor import triage

    config = load_config(workspace_root, target)
    entries = sorted(scan_packages(workspace_root, target), key=priority_key)[:max_n]
    results: list[dict[str, Any]] = []
    for entry in entries:
        package: dict[str, Any] = entry["package"]
        # 分诊铁律：archive 档（情感/剧情/颜值/朋友支持/集赞/未分类）只归档不写笔记，
        # 与 ZCode 桌面自动化同一契约；包保持 pending 留在队列（不烧任何执行者额度）
        facts = _work_facts(int(package.get("work_id") or 0))
        transcript = ""
        if facts.get("transcript_path"):
            transcript = triage._read_transcript_text(_settings_media_root(), facts["transcript_path"])
        verdict = triage.classify(facts.get("title") or package.get("title") or "", transcript_text=transcript)
        if verdict.action != "note":
            results.append(
                {
                    "work_id": package.get("work_id"),
                    "result": "skipped-archive",
                    "category": verdict.label,
                    "path": str(entry["path"]),
                }
            )
            continue
        plan = {
            "work_id": package.get("work_id"),
            "target": package.get("target"),
            "path": str(entry["path"]),
        }
        if dry_run:
            plan["result"] = "dry-run"
            plan["executor"] = config.executor
            results.append(plan)
            continue
        plan["result"] = dispatch_one(entry, config, workspace_root)
        results.append(plan)
    return results


def _get_work(work_id: int):
    """按 id 取 Work（reassign 重建任务包用）；查不到返回 None。"""
    from monitor.store.shared_db import SessionLocal

    from app.models import Work

    with SessionLocal() as db:
        return db.get(Work, work_id)


def reassign(max_n: int, target: str, dry_run: bool, workspace_root: Path) -> list[dict[str, Any]]:
    """把 pending 包搬去指定目标的队列（切换执行者时迁移积压用）。

    复用 note_service.write_pending_manifest 重建任务包（expected_output/target
    按新目标重算），成功后删除旧包文件与旧 error 回执。同名目标跳过不搬。
    """
    from app.services import note_service, summary_target_service

    resolved = summary_target_service.resolve_target(target)
    dest_pending = Path(resolved["pending_dir"])
    results: list[dict[str, Any]] = []
    scanned = sorted(scan_packages(workspace_root), key=priority_key)
    for entry in scanned:
        if len([r for r in results if r["result"] != "skipped-same-target"]) >= max_n:
            break
        path: Path = entry["path"]
        package: dict[str, Any] = entry["package"]
        if path.parent.resolve() == dest_pending.resolve():
            results.append({"work_id": package.get("work_id"), "result": "skipped-same-target", "path": str(path)})
            continue
        work = _get_work(int(package.get("work_id") or 0))
        if work is None:
            results.append({"work_id": package.get("work_id"), "result": "skipped-work-missing", "path": str(path)})
            continue
        if dry_run:
            results.append(
                {"work_id": package.get("work_id"), "result": "dry-run", "from": str(path), "to": str(dest_pending)}
            )
            continue
        from app.utils.timeutil import now_iso

        new_rel = note_service.write_pending_manifest(work, target=target)
        new_path = Path(new_rel) if Path(new_rel).is_absolute() else workspace_root / new_rel
        path.unlink(missing_ok=True)
        path.with_name(path.stem + ".error.json").unlink(missing_ok=True)
        results.append(
            {
                "work_id": package.get("work_id"),
                "result": "moved",
                "from": str(path),
                "to": str(new_path),
                "at": now_iso(),
            }
        )
    return results


def main() -> int:
    from monitor.store.shared_db import settings

    parser = argparse.ArgumentParser(description="AI 笔记消费端：pending 包 → 无头 AI CLI → 回填")
    parser.add_argument("--once", action="store_true", help="单轮消费后退出（默认行为）")
    parser.add_argument("--loop", action="store_true", help="常驻轮询（每 POLL_SECONDS 一轮）")
    parser.add_argument("--max", type=int, default=5, help="单轮最多消费 N 个包")
    parser.add_argument("--reassign", metavar="TARGET", default="", help="把 pending 包搬到指定目标的队列（如 codex/trae/zcode/workbuddy），配合 --max 限数")
    parser.add_argument("--target", metavar="TARGET", default="", help="只消费该目标的队列并使用其专属执行器（codex/trae/zcode）")
    parser.add_argument("--dry-run", action="store_true", help="只输出消费计划，不落盘不调执行器")
    parser.add_argument("--self-test", action="store_true", help="只探测执行器可用性（跑 --version）")
    args = parser.parse_args()
    workspace_root = Path(settings.workspace_root)

    if args.self_test:
        config = load_config(workspace_root, args.target or None)
        print(f"executor={config.executor} command={list(config.command)} prompt_flag={config.prompt_flag!r}")
        if config.command:
            if config.prompt_flag == "-p":
                returncode, output = run_executor(config, "--version", workspace_root)
            else:
                # 位置参数型（codex exec/trae-cli run）：只探测二进制版本，避免把探针当任务真跑
                probe = subprocess.run(
                    [config.command[0], "--version"],  # noqa: S603 - 固定配置命令
                    capture_output=True, text=True, timeout=60,
                )
                returncode = probe.returncode
                output = (probe.stdout or "") + "\n" + (probe.stderr or "")
            print(f"probe rc={returncode} output={output.strip()[:200]}")
            return 0 if returncode == 0 else 1
        return 1

    lock_path = workspace_root / ".run" / LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("另一个 note_dispatcher 实例正在运行，退出", file=sys.stderr)
            return 2
        while True:
            if args.reassign:
                results = reassign(args.max, args.reassign, args.dry_run, workspace_root)
            else:
                results = run_once(args.max, args.dry_run, workspace_root, args.target or None)
            for row in results:
                print(
                    f"{row['result']:>24} | work {row.get('work_id')} | "
                    f"{row.get('path') or row.get('from') or ''}"
                    f"{(' -> ' + row['to']) if row.get('to') else ''}"
                )
            if not args.loop or not results:
                return 0
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
