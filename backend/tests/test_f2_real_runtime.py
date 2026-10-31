"""真实 F2 运行时回归（pytest 包装）：在隔离 F2 venv 中以子进程运行网络桩检查。

真实 Filter / handler 生成器参与验证（网络打桩，无真实平台请求）；
运行时缺失时跳过（fresh checkout 未安装 F2 环境不阻塞其余测试）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_PYTHON = _WORKSPACE_ROOT / "tools" / "f2-runtime" / ".venv" / "bin" / "python"
_CHECK_SCRIPT = Path(__file__).resolve().parent / "_f2_real_runtime_check.py"

pytestmark = pytest.mark.skipif(
    not _RUNTIME_PYTHON.exists(),
    reason="F2 隔离运行时未安装（tools/f2-runtime/.venv）",
)


def test_real_f2_filter_and_generator_semantics():
    result = subprocess.run(
        [str(_RUNTIME_PYTHON), str(_CHECK_SCRIPT)],
        cwd=str(_WORKSPACE_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"检查脚本失败: {result.stderr[-800:]}"
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True, f"真实运行时检查未通过: {payload['failures']}"
    assert payload["checks"]["missing_aweme_list"] == "platform_error"
    assert payload["checks"]["collects_status_8"] == "login_expired"
    # 单夹响应缺 aweme_list：隔离为单夹失败，不丢弃整次收藏夹范围
    assert payload["checks"]["folder_missing_aweme_list"] == "isolated"
    # 断网环境下点赞缺少显式标识：自动识别必须明确失败，不能冒充空点赞
    assert payload["checks"]["like_auto_identity"] == "self_identity_unavailable"
    assert payload["checks"]["auto_identity_like_complete"] is True
    assert payload["checks"]["explicit_identity_skips_profile"] is True
    assert payload["checks"]["auto_identity_missing_status"] == "self_identity_unavailable"
    # 3xx 一律拒绝跟随：本人资料与全部列表（GET/POST、含夹内作品）
    assert payload["checks"]["redirect_self_profile"] == "redirect_refused"
    assert payload["checks"]["redirect_like"] == "redirect_refused"
    assert payload["checks"]["redirect_collection"] == "redirect_refused"
    assert payload["checks"]["redirect_folder_items"] == "redirect_refused"
    assert payload["checks"]["detail_redirect"] == "redirect_refused"
    assert payload["checks"]["detail_restores_handler_crawler"] is True
    # 连续 403：只尝试有限次数后停止，剩余夹标记未完成
    assert payload["checks"]["consecutive_403_requests"] == 2
    assert payload["checks"]["consecutive_403_stopped"] is True
    # 多次短预算通过持久化游标/夹进度最后确实完整
    assert payload["checks"]["resume_completed_in_three_runs"] is True
