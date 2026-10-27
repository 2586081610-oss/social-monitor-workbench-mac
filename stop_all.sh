#!/bin/sh
# ============================================================
#  stop_all.sh - 停止 start_all.sh / start_monitor.sh 启动的服务（Mac 版）
#  停止边界（对应任务书"只处理身份可验证的本项目进程"）:
#   1) 只处理 .run/*.pid 记录的 PID，且核对命令行含本项目根路径；
#      不匹配则跳过并警告，绝不误杀。
#   2) 记录 PID 已失效但端口仍被占时：若占用者命令行含本项目根路径
#      （本项目遗留子进程），按身份停止；否则仅报告占用者并安全退出，
#      不杀未知归属进程。
#   3) 不使用 pkill/模式匹配扫杀。
# ============================================================
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"

stop_verified_pid() {
  # $1=pid  $2=名称；返回 0=已处理 1=未处理
  pid="$1"; name="$2"
  kill -0 "$pid" 2>/dev/null || return 1
  cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  case "$cmd" in
    *"$ROOT"*)
      kill "$pid" 2>/dev/null || return 1
      echo "已停止 $name (pid $pid)"
      return 0 ;;
    *)
      echo "[WARN] $name 的 PID $pid 命令行不含本项目路径，跳过: ${cmd:-未知}"; return 1 ;;
  esac
}

for name in backend frontend monitor; do
  pidfile="$ROOT/.run/$name.pid"
  [ -f "$pidfile" ] || continue
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [ -n "${pid:-}" ]; then
    stop_verified_pid "$pid" "$name" || {
      kill -0 "$pid" 2>/dev/null || echo "$name 的记录 PID $pid 已不存在"
    }
  fi
  rm -f "$pidfile"
done

# ---- 等待退出后兜底检查端口；只处理身份可验证的本项目遗留进程 ----
sleep 2
for port in 8000 5173; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    for pid in $(lsof -tnP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null); do
      cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
      case "$cmd" in
        *"$ROOT"*)
          kill "$pid" 2>/dev/null || true
          echo "已停止端口 $port 上本项目遗留的进程 (pid $pid)" ;;
        *)
          echo "[ERROR] 端口 $port 被非本项目进程占用，未处理（PID ${pid}）："
          echo "        ${cmd}"
          echo "        如需处理请人工确认后自行操作。" ;;
      esac
    done
  fi
done

echo "stop_all 完成。确认端口已释放: lsof -nP -iTCP:8000 -iTCP:5173 -sTCP:LISTEN（无输出=已停止）"
