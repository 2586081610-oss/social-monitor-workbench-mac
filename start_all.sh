#!/bin/sh
# ============================================================
#  start_all.sh - 一键启动博主监控工作台（Mac 版）
#  默认只启动: 后端 uvicorn(:8000) + 前端 vite preview(:5173)
#  监控层(monitor/main.py)默认【不启动】: 其启动即注册 RSSHub/抖音
#  轮询与 trigger 消费（monitor/main.py build_scheduler 无条件注册），
#  需要时用 start_monitor.sh 显式开启。
#  停止: ./stop_all.sh
#  用法: ./start_all.sh [--sandbox]
#    --sandbox  数据/媒体/笔记/归档根全部指向 .sandbox/（隔离验收用，
#               不触碰真实 aggregator.db、media/、Obsidian/、健身/）
#  本脚本不设置任何代理变量，仅对本脚本启动的子进程合并
#  NO_PROXY/no_proxy 的 localhost 直连项；不改系统代理。
# ============================================================
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

SANDBOX=0
for arg in "$@"; do
  case "$arg" in
    --sandbox) SANDBOX=1 ;;
    *) echo "[ERROR] 未知参数: $arg（支持: --sandbox）"; exit 1 ;;
  esac
done

# ---- 运行时定位（可被环境变量覆盖；不依赖 WorkBuddy 托管运行时） ----
UV_BIN="${UV_BIN:-$HOME/.local/bin/uv}"
NODE_DIR="${NODE_DIR:-$HOME/Library/Application Support/socialmonitor-node/current}"
PYTHON="$ROOT/.venv/bin/python"
NODE="$NODE_DIR/bin/node"

if [ ! -x "$PYTHON" ]; then
  [ -x "$UV_BIN" ] && "$UV_BIN" venv "$ROOT/.venv" --python 3.13.18 || {
    echo "[ERROR] 缺少 $PYTHON 且 uv 不可用；请先运行 tools/install-mac.sh"; exit 1; }
fi
if [ ! -x "$NODE" ]; then
  echo "[ERROR] 缺少 Node: $NODE（请先运行 tools/install-mac.sh 或设置 NODE_DIR）"; exit 1
fi

PATH="$ROOT/tools/bin:$NODE_DIR/bin:$PATH"
export PATH

# ---- 配置来源：backend/.env 由 config.py 自行加载（仅补缺失键，已导出环境优先）。
#      本脚本不读取、不回显 .env 内容，避免密钥进入 shell 历史/日志。 ----

# ---- 隔离沙箱（--sandbox）：显式重定向全部持久化位置 ----
if [ "$SANDBOX" = "1" ]; then
  SB="$ROOT/.sandbox"
  mkdir -p "$SB/media" "$SB/obsidian" "$SB/workbuddy" "$SB/fitness"
  DATABASE_PATH="$SB/aggregator.db"
  MEDIA_ROOT="$SB/media"
  OBSIDIAN_ROOT="$SB/obsidian"
  WORKBUDDY_DIR="$SB/workbuddy"
  # fitness 归档根默认指向工作区"健身/"，沙箱模式必须一并重定向（config.py _archive_root_paths）
  ARCHIVE_ROOTS="{\"fitness\":\"$SB/fitness\"}"
  export DATABASE_PATH MEDIA_ROOT OBSIDIAN_ROOT WORKBUDDY_DIR ARCHIVE_ROOTS
  echo "[sandbox] 数据/媒体/笔记/归档根已指向 $SB"
fi

# ---- NO_PROXY/no_proxy 合并（保留已有值，仅追加缺失的 localhost 直连项） ----
case ",${NO_PROXY:-}," in *,localhost,*) : ;; *) NO_PROXY="${NO_PROXY:+$NO_PROXY,}localhost,127.0.0.1,::1"; export NO_PROXY ;; esac
case ",${no_proxy:-}," in *,localhost,*) : ;; *) no_proxy="${no_proxy:+$no_proxy,}localhost,127.0.0.1,::1"; export no_proxy ;; esac

# ---- 前置检查：端口占用即安全报错（不杀任何进程） ----
for port in 8000 5173; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[ERROR] 端口 $port 已被占用，拒绝启动（不自动清理）。占用者："
    lsof -nP -iTCP:"$port" -sTCP:LISTEN
    exit 1
  fi
done
if [ ! -f "$ROOT/frontend/dist/index.html" ]; then
  echo "[ERROR] frontend/dist 未构建，请先: cd frontend && npm ci && npm run build"; exit 1
fi

mkdir -p "$ROOT/.run/logs"

echo "============================================================"
echo " 启动博主监控工作台（监控层默认不开；--sandbox=${SANDBOX}）"
echo "   1/2 后端   http://127.0.0.1:8000/docs"
echo "   2/2 前端   http://127.0.0.1:5173"
echo "============================================================"

# ---- 1. 后端 (uvicorn :8000) ----
nohup "$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --app-dir "$ROOT/backend" \
  > "$ROOT/.run/logs/backend.log" 2>&1 &
echo $! > "$ROOT/.run/backend.pid"

# ---- 2. 前端 (vite preview :5173) ----
(
  cd "$ROOT/frontend"
  # 用绝对路径启动：保证进程命令行含项目根路径，stop_all.sh 才能完成身份核验
  nohup "$NODE" "$ROOT/frontend/node_modules/vite/bin/vite.js" preview --port 5173 --host 127.0.0.1 \
    > "$ROOT/.run/logs/frontend.log" 2>&1 &
  echo $! > "$ROOT/.run/frontend.pid"
)

sleep 2
echo "已启动（PID 见 .run/）。日志: .run/logs/。停止: ./stop_all.sh"
