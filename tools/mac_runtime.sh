#!/bin/sh
# Shared by Mac entrypoints. ROOT is provided by the caller.
merge_loopback_proxy() {
  for loopback in localhost 127.0.0.1 ::1; do
    case ",${NO_PROXY:-}," in *",$loopback,"*) ;; *) NO_PROXY="${NO_PROXY:+$NO_PROXY,}$loopback" ;; esac
    case ",${no_proxy:-}," in *",$loopback,"*) ;; *) no_proxy="${no_proxy:+$no_proxy,}$loopback" ;; esac
  done
  export NO_PROXY no_proxy
}

lock_runtime() {
  mkdir -p "$ROOT/.run/logs"
  mkdir "$ROOT/.run/control.lock" 2>/dev/null || {
    echo "[ERROR] 另一个启停操作正在执行，或遗留 .run/control.lock；请核对后重试"; return 1;
  }
}

valid_pid() {
  case "$1" in ''|*[!0-9]*|0|1) return 1 ;; esac
}

process_alive() {
  valid_pid "$1" || return 1
  kill -0 "$1" 2>/dev/null || return 1
  case "$(ps -p "$1" -o stat= 2>/dev/null)" in ''|*Z*) return 1 ;; esac
}

process_identity() {
  # Include creation time and full command, not just a path substring.
  LC_ALL=C ps -ww -p "$1" -o lstart= -o command= 2>/dev/null
}

refuse_running_record() {
  if [ -f "$ROOT/.run/$1.pid" ]; then
    existing_pid="$(cat "$ROOT/.run/$1.pid")"
    if process_alive "$existing_pid"; then
      echo "[ERROR] $1 有存活的 PID 记录，拒绝重复启动或覆盖身份文件"; return 1
    fi
  fi
}

record_identity() {
  r_pid="$(cat "$ROOT/.run/$1.pid")"
  r_attempt=0
  while [ "$r_attempt" -lt 10 ]; do
    process_alive "$r_pid" || return 1
    r_command="$(LC_ALL=C ps -ww -p "$r_pid" -o command= 2>/dev/null)"
    # nohup/env/shell may still be exec-ing the real interpreter. Recording
    # that transient command would reject our own service moments later.
    case "$r_command" in
      /usr/bin/env\ *|/bin/sh\ *|/bin/bash\ *|nohup\ *|/usr/bin/nohup\ *)
        r_attempt=$((r_attempt + 1)); sleep 1 ;;
      *)
        process_identity "$r_pid" > "$ROOT/.run/$1.identity"
        [ -s "$ROOT/.run/$1.identity" ]; return $? ;;
    esac
  done
  return 1
}

verified_alive() {
  [ -f "$ROOT/.run/$1.pid" ] && [ -s "$ROOT/.run/$1.identity" ] || return 1
  v_pid="$(cat "$ROOT/.run/$1.pid")"
  process_alive "$v_pid" || return 1
  [ "$(process_identity "$v_pid")" = "$(cat "$ROOT/.run/$1.identity")" ]
}

stop_recorded() {
  s_name="$1"
  [ -f "$ROOT/.run/$s_name.pid" ] || return 0
  s_pid="$(cat "$ROOT/.run/$s_name.pid")"
  if ! valid_pid "$s_pid"; then
    echo "[WARN] $s_name PID 记录无效，未发送信号"; return 1
  fi
  if process_alive "$s_pid"; then
    if ! verified_alive "$s_name"; then
      echo "[WARN] $s_name 身份无法核实（pid ${s_pid}），未处理；保留记录供人工确认"; return 1
    fi
    kill "$s_pid" 2>/dev/null || return 1
    s_wait=0
    while process_alive "$s_pid" && [ "$s_wait" -lt 5 ]; do
      sleep 1
      s_wait=$((s_wait + 1))
    done
    if process_alive "$s_pid"; then
      echo "[ERROR] $s_name 尚未退出，保留记录；未强制终止"; return 1
    fi
    echo "已停止 $s_name (pid $s_pid)"
  fi
  rm -f "$ROOT/.run/$s_name.pid" "$ROOT/.run/$s_name.identity"
}
