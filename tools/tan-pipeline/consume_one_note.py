"""AI笔记消费端（zcode 本体执行版）。

用法：
  show <work_id>              # 打印 pending 包字段 + 转写全文 + 抽帧摘要（供 zcode 阅读撰写笔记）
  finish <work_id> <note.md>  # 把写好的笔记写入 expected_output（Obsidian）→ complete-note 回填成功 → 包标 done
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from tan_paths import WORKBENCH_ROOT, inject_syspath  # noqa: E402

ROOT = WORKBENCH_ROOT  # workbench 仓库根（向上探测，TAN_WORKBENCH 可覆盖）
inject_syspath()

from app.utils.timeutil import now_iso  # noqa: E402
from monitor.store.shared_db import settings  # noqa: E402

PENDING = Path(settings.pending_dir) if hasattr(settings, "pending_dir") else ROOT / "workbuddy" / "pending"
API = "http://127.0.0.1:8000/api/v1"
TOKEN = "dev-admin-token-change-me"
EXECUTOR = "zcode"


def find_package(work_id: int) -> tuple[Path, dict] | None:
    for p in sorted(PENDING.glob(f"{work_id}-*.json")):
        return p, json.loads(p.read_text())
    return None


def resolve_media(rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else (settings.media_root / p)


def show(work_id: int) -> None:
    found = find_package(work_id)
    if not found:
        print(f"NO PACKAGE for {work_id} in {PENDING}")
        return
    path, pkg = found
    print("=== package ===")
    print(json.dumps({k: v for k, v in pkg.items() if k != "provenance"}, ensure_ascii=False, indent=1))
    tp = resolve_media(pkg["transcript_path"])
    if tp.exists():
        data = json.loads(tp.read_text())
        segs = data if isinstance(data, list) else data.get("segments", [])
        print(f"=== transcript ({len(segs)} segments) ===")
        for s in segs:
            st = int(float(s.get("start", 0)))
            print(f"[{st//60:02d}:{st%60:02d}] {s.get('text','').strip()}")
    else:
        print(f"=== transcript MISSING: {tp} ===")
    fd = resolve_media(pkg["frames_dir"])
    manifest = fd / "frame_manifest.json"
    if manifest.exists():
        m = json.loads(manifest.read_text())
        frames = m.get("frames", m if isinstance(m, list) else [])
        print(f"=== frames: {len(frames)} ({'fallback' if m.get('fallback') else 'semantic'}) ===")
        for f in frames[:14]:
            print("  ", Path(str(f.get('path', f))).name, str(f.get('timestamp', ''))[:8])
    else:
        print("=== frames manifest missing ===")


def finish(work_id: int, note_file: str) -> None:
    found = find_package(work_id)
    if not found:
        sys.exit(f"NO PACKAGE for {work_id}")
    path, pkg = found
    note_text = Path(note_file).read_text()
    out_rel = str(pkg["expected_output"]).replace("\\", "/")
    out_abs = settings.workspace_root / out_rel
    out_abs.parent.mkdir(parents=True, exist_ok=True)
    out_abs.write_text(note_text)
    print(f"note written: {out_abs} ({len(note_text)} chars)")

    import httpx
    api_rel = out_rel[len("Obsidian/"):] if out_rel.startswith("Obsidian/") else out_rel
    # 流水线 runner 转写期间持有 SQLite 写锁，complete-note 可能 database is locked → 退避重试
    # 顺序：先回填成功再标 done；任何失败包保持原状可重试
    ok = False
    for attempt in range(12):
        try:
            r = httpx.post(
                f"{API}/works/{work_id}/complete-note",
                headers={"Authorization": f"Bearer {TOKEN}"},
                json={"note_path": api_rel, "executor": EXECUTOR, "target": pkg.get("target", "workbuddy")},
                timeout=15,
            )
            body = r.json()
        except Exception as exc:  # 网络错误/非JSON响应按失败重试，包保持可重试
            print(f"complete-note attempt {attempt + 1}: {type(exc).__name__}: {str(exc)[:80]}")
            time.sleep(20)
            continue
        if r.status_code == 200 and body.get("code") == 0:
            print(f"complete-note OK (attempt {attempt + 1})")
            ok = True
            break
        print(f"complete-note attempt {attempt + 1}: {r.status_code} {str(body.get('message'))[:60]}")
        time.sleep(20)

    if not ok:
        print(
            f"ERROR: complete-note 连续失败，包未标 done 保持可重试（DB 可能仍 NOTE_GENERATING）。\n"
            f"补救：待后端/SQLite 锁恢复后重跑 → python scripts/consume_one_note.py finish {work_id} {note_file}\n"
            f"（笔记已写入 {out_abs}，重跑覆盖同内容，complete-note 幂等可重复调用）",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(2)

    # 回填成功后才把包标 done（tmp+replace 原子替换）
    pkg["status"] = "done"
    pkg["dispatch"] = {"executor": EXECUTOR, "attempts": 1, "completed_at": now_iso(), "note_path": out_rel}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(pkg, ensure_ascii=False, indent=2))
    tmp.replace(path)
    print(f"package marked done: {path.name}")


def dump(work_id: int) -> None:
    """清洗后的转写全文（去字间空格）+ 笔记 prompt（含 build_prompt 产出的读图清单）。

    读图清单与笔记生成侧同源（monitor.note_dispatcher.build_prompt），
    按 semantic > scene > sample > fallback 优先级选 8–12 张并分桶覆盖，
    避免操作员按旧版 dump 只读前 3 张。
    """
    found = find_package(work_id)
    if not found:
        sys.exit(f"NO PACKAGE for {work_id}")
    path, pkg = found
    tp = resolve_media(pkg["transcript_path"])
    data = json.loads(tp.read_text())
    segs = data if isinstance(data, list) else data.get("segments", [])
    print(f"# work_id={work_id} | {pkg['title']} | {pkg['publish_date']} | {len(segs)}段")
    print(f"# transcript: {tp}")
    print(f"# frames_dir: {resolve_media(pkg['frames_dir'])}")
    for s in segs:
        t = "".join(str(s.get("text", "")).split())
        if not t:
            continue
        st = int(float(s.get("start", 0)))
        print(f"[{st//60:02d}:{st%60:02d}] {t}")

    # 与自动派发同一份 prompt/选帧逻辑，避免两处漂移
    from monitor.note_dispatcher import build_prompt

    print("=== note prompt（含读图清单，按此清单逐张打开） ===")
    print(build_prompt(path, ROOT))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "show":
        show(int(sys.argv[2]))
    elif cmd == "dump":
        dump(int(sys.argv[2]))
    elif cmd == "finish":
        finish(int(sys.argv[2]), sys.argv[3])
    else:
        sys.exit(__doc__)
