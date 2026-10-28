#!/bin/bash
# 启动个人来源任务进程（只运行已授权的本人点赞/收藏定时同步与下载）。
# 与 start_all.sh 不同：本脚本不启动后端/前端，也不注册 RSSHub/抖音博主轮询、
# trigger 消费或全局下载队列——只运行 monitor.personal_worker。
# 同步/下载开关默认关闭；请先在工作台「个人来源」页完成配置与授权。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  echo "[ERROR] 缺少解释器: $PYTHON（请先运行 tools/install-mac.sh）" >&2
  exit 1
fi

cd "$ROOT"
exec "$PYTHON" -m monitor.personal_worker
