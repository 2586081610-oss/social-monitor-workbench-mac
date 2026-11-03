"""tan-pipeline 脚本共享路径解析（路径可移植性核心模块）。

两个概念分开：
- WORKBENCH_ROOT：被调用的 socialmonitor-mac workbench 仓库根（判定标志：同时含
  backend/ 与 monitor/ 目录）。解析顺序：
    1. 环境变量 TAN_WORKBENCH（校验标志目录，非法即报错退出）
    2. 从本文件向上逐级探测含标志目录的祖先（仓库内 tools/tan-pipeline/ 场景命中仓库根）
    3. 本机历史默认路径（原 zcode 外挂脚本场景）
    4. 全部失败 → 清晰报错退出（SystemExit，不崩栈）
- DATA_ROOT：任务数据目录（含 data/、pipeline_checkpoint.json、pipeline_selection.json、
  bilibili/douyin 抓取结果的工作区根）。解析顺序：
    1. 环境变量 TAN_DATA_DIR
    2. 本机历史默认 zcode 工作区路径
  并做存在性校验，缺失时清晰报错退出。

环境变量与默认值：
  TAN_WORKBENCH  默认：<向上探测> → /Users/zzboy/Documents/ChatGPT/总控 2/socialmonitor-mac/workbench
  TAN_DATA_DIR   默认：/Users/zzboy/.zcode/workspace/35161a77-cfbb-413b-b8f4-515b9e9caa08
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_LEGACY_WORKBENCH = Path("/Users/zzboy/Documents/ChatGPT/总控 2/socialmonitor-mac/workbench")
_LEGACY_DATA = Path("/Users/zzboy/.zcode/workspace/35161a77-cfbb-413b-b8f4-515b9e9caa08")
_MARKERS = ("backend", "monitor")


def _is_workbench_root(p: Path) -> bool:
    return all((p / m).is_dir() for m in _MARKERS)


def _fail(msg: str) -> None:
    print(f"[tan_paths] ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def _resolve_workbench_root() -> Path:
    env = os.environ.get("TAN_WORKBENCH")
    if env:
        p = Path(env).expanduser().resolve()
        if _is_workbench_root(p):
            return p
        _fail(f"TAN_WORKBENCH={env} 不是有效的 workbench 仓库根（需同时含 backend/ 与 monitor/ 目录）")
    for cand in Path(__file__).resolve().parents:
        if _is_workbench_root(cand):
            return cand
    if _is_workbench_root(_LEGACY_WORKBENCH):
        return _LEGACY_WORKBENCH
    _fail(
        "无法定位 workbench 仓库根：从脚本位置向上未找到含 backend/+monitor/ 的目录，"
        f"默认路径 {_LEGACY_WORKBENCH} 也不可用。\n请设置环境变量 TAN_WORKBENCH=/path/to/workbench 后重试。"
    )
    raise AssertionError("unreachable")


def _resolve_data_root() -> Path:
    env = os.environ.get("TAN_DATA_DIR")
    p = Path(env).expanduser().resolve() if env else _LEGACY_DATA
    if p.is_dir():
        return p
    src = f"TAN_DATA_DIR={env}" if env else f"默认路径 {_LEGACY_DATA}"
    _fail(
        f"任务数据目录不存在：{p}（来源：{src}）。\n"
        "任务数据（data/、pipeline_checkpoint.json、pipeline_selection.json）仍保存在原 zcode 工作区，\n"
        "请设置环境变量 TAN_DATA_DIR=/path/to/任务工作区 后重试。"
    )
    raise AssertionError("unreachable")


WORKBENCH_ROOT = _resolve_workbench_root()
DATA_ROOT = _resolve_data_root()


def inject_syspath() -> None:
    """等价原脚本 sys.path.insert 行为：WORKBENCH_ROOT/backend 与 WORKBENCH_ROOT 入列。"""
    # 保持与原脚本相同的最终顺序：WORKBENCH_ROOT 在 [0]，backend 在 [1]
    for p in (str(WORKBENCH_ROOT / "backend"), str(WORKBENCH_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
