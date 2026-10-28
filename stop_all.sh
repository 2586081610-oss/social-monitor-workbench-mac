#!/bin/sh
# Only stop PID-file processes with the recorded creation time + full command.
# Port occupants are reported, never used as targets for termination.
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
. "$ROOT/tools/mac_runtime.sh"
lock_runtime
trap 'rmdir "$ROOT/.run/control.lock" 2>/dev/null || true' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
result=0
for name in backend frontend monitor; do
  stop_recorded "$name" || result=1
done
for port in 8000 5173; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[WARN] 端口 $port 仍被占用，仅报告，不终止任何未登记进程："
    lsof -nP -iTCP:"$port" -sTCP:LISTEN || true
    result=1
  fi
done
[ "$result" = 0 ] && echo "已登记服务已停止，端口检查未见占用。"
exit "$result"
