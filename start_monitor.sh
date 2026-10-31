#!/bin/sh
# ============================================================
#  start_monitor.sh - 显式启动监控层 monitor/main.py（Mac 版）
#  【默认不随 start_all.sh 启动】monitor 一旦启动会立即注册并执行:
#    - RSSHub 平台轮询（bilibili/twitter，POLL_RSSHUB_INTERVAL 默认 300s）
#    - 抖音轮询（POLL_DOUYIN_INTERVAL 默认 600s，需 MediaCrawler+cookie）
#    - trigger 消费（10s）与心跳写入
#    - MONITOR_RUN_PIPELINE=true 时才执行下载→转写→抽帧流水线
#  请确认上述行为符合预期再运行；停止: ./stop_all.sh
#  用法: ./start_monitor.sh [--sandbox]
# ============================================================
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

SANDBOX=0
for arg in "$@"; do
  case "$arg" in --sandbox) SANDBOX=1 ;; *) echo "[ERROR] 未知参数: $arg"; exit 1 ;; esac
done

NODE_DIR="${NODE_DIR:-$HOME/Library/Application Support/socialmonitor-node/current}"
PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || { echo "[ERROR] 缺少 $PYTHON"; exit 1; }
PATH="$ROOT/tools/bin:$NODE_DIR/bin:$PATH"; export PATH

if [ "$SANDBOX" = "1" ]; then
  SB="$ROOT/.sandbox"
  mkdir -p "$SB/media" "$SB/obsidian" "$SB/workbuddy" "$SB/fitness"
  DATABASE_PATH="$SB/aggregator.db"; MEDIA_ROOT="$SB/media"
  OBSIDIAN_ROOT="$SB/obsidian"; WORKBUDDY_DIR="$SB/workbuddy"
  ARCHIVE_ROOTS="{\"fitness\":\"$SB/fitness\"}"
  export DATABASE_PATH MEDIA_ROOT OBSIDIAN_ROOT WORKBUDDY_DIR ARCHIVE_ROOTS
  echo "[sandbox] 监控层使用隔离数据: $SB"
fi

. "$ROOT/tools/mac_runtime.sh"
merge_loopback_proxy

lock_runtime
monitor_done=0
monitor_started=0
finish_monitor_start() {
  if [ "$monitor_done" != 1 ] && [ "$monitor_started" = 1 ]; then
    stop_recorded monitor || true
    echo "[ERROR] 监控进程启动失败，查看 .run/logs/monitor.log"
  fi
  rmdir "$ROOT/.run/control.lock" 2>/dev/null || true
}
trap finish_monitor_start EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
refuse_running_record monitor

echo "[提示] 将启动的轮询: RSSHub(bilibili/twitter) 每 ${POLL_RSSHUB_INTERVAL:-300}s、抖音每 ${POLL_DOUYIN_INTERVAL:-600}s、小红书每 ${POLL_XHS_INTERVAL:-600}s、trigger 每 ${POLL_TRIGGER_INTERVAL:-10}s；流水线 MONITOR_RUN_PIPELINE=${MONITOR_RUN_PIPELINE:-false}"
nohup "$PYTHON" "$ROOT/monitor/main.py" > "$ROOT/.run/logs/monitor.log" 2>&1 &
echo $! > "$ROOT/.run/monitor.pid"
monitor_started=1
sleep 1
record_identity monitor || exit 1
verified_alive monitor || exit 1
monitor_done=1
echo "监控层进程已启动（平台可用性需另行验收） (pid $(cat "$ROOT/.run/monitor.pid"))，日志: .run/logs/monitor.log"
