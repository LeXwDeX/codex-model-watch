#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8787}"
STATE_DIR="$HOME/.codex-model-watch"
LOG="${LOG:-$STATE_DIR/watch.log}"
PIDFILE="$STATE_DIR/watch.pid"
URL="http://127.0.0.1:$PORT/"

mkdir -p "$STATE_DIR"

is_running() {
  [[ -f "$PIDFILE" ]] || return 1
  local pid
  pid="$(cat "$PIDFILE")"
  kill -0 "$pid" 2>/dev/null || return 1
  ps -p "$pid" -o command= | grep -q codex_model_watch.py
}

case "${1:-up}" in
  up)
    "$0" start
    open "$URL"
    ;;
  start)
    if is_running; then
      echo "已在运行 (PID $(cat "$PIDFILE"))，面板: $URL"
      exit 0
    fi
    rm -f "$PIDFILE"
    nohup "$PYTHON" "$DIR/codex_model_watch.py" --port "$PORT" --no-open >>"$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 1
    if is_running; then
      echo "已启动 (PID $(cat "$PIDFILE"))，面板: $URL"
      echo "日志: $LOG ；停止: $0 stop"
    else
      rm -f "$PIDFILE"
      echo "启动失败，请查看日志: $LOG" >&2
      exit 1
    fi
    ;;
  stop)
    if is_running; then
      kill "$(cat "$PIDFILE")"
      rm -f "$PIDFILE"
      echo "已停止"
    else
      rm -f "$PIDFILE"
      echo "未在运行"
    fi
    ;;
  restart)
    "$0" stop || true
    sleep 1
    exec "$0" start
    ;;
  open)
    open "$URL"
    ;;
  status)
    if is_running; then
      echo "运行中 (PID $(cat "$PIDFILE"))，面板: $URL"
    else
      echo "未在运行"
      exit 1
    fi
    ;;
  *)
    echo "用法: $0 [stop|restart|status]  （无参数 = 启动并打开网页）" >&2
    exit 2
    ;;
esac
