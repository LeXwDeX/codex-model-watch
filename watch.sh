#!/usr/bin/env bash
# 手动启动；启动后交给 launchd 托管：脱离终端运行，任何原因退出都会被自动拉起。
# plist 放在状态目录而不是 ~/Library/LaunchAgents，所以不会登录自启；注销/重启后需再次手动启动。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8787}"
STATE_DIR="$HOME/.codex-model-watch"
LOG="${LOG:-$STATE_DIR/watch.log}"
LEGACY_PIDFILE="$STATE_DIR/watch.pid"
URL="http://127.0.0.1:$PORT/"
LABEL="com.codex-model-watch"
PLIST="$STATE_DIR/$LABEL.plist"
LEGACY_AGENT_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
TARGET="$DOMAIN/$LABEL"

mkdir -p "$STATE_DIR"

# launchd 不继承 shell 的 PATH，必须写死解释器绝对路径
resolve_python() {
  local py="${PYTHON:-python3}"
  if [[ "$py" != /* ]]; then
    py="$(command -v "$py" || true)"
  fi
  if [[ -z "$py" || ! -x "$py" ]]; then
    echo "找不到 Python 解释器（可用 PYTHON=/绝对路径 指定）" >&2
    exit 1
  fi
  echo "$py"
}

xml_escape() {
  local s="$1"
  s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"
  printf '%s' "$s"
}

write_plist() {
  local py
  py="$(resolve_python)"
  cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$py")</string>
    <string>$(xml_escape "$DIR/codex_model_watch.py")</string>
    <string>--port</string><string>$PORT</string>
    <string>--no-open</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml_escape "$DIR")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$(xml_escape "$LOG")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$LOG")</string>
</dict>
</plist>
EOF
  plutil -lint "$PLIST" >/dev/null
}

is_loaded() { launchctl print "$TARGET" >/dev/null 2>&1; }

current_pid() {
  { launchctl print "$TARGET" 2>/dev/null || true; } | awk '$1 == "pid" && $2 == "=" { print $3; exit }'
}

# 旧版脚本用 nohup 起的实例会占住端口，迁移时先停掉
stop_legacy() {
  # 早期版本把 plist 放进 LaunchAgents 会登录自启，删掉它
  rm -f "$LEGACY_AGENT_PLIST"
  [[ -f "$LEGACY_PIDFILE" ]] || return 0
  local pid
  pid="$(cat "$LEGACY_PIDFILE")"
  if kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o command= | grep -q codex_model_watch.py; then
    kill "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    echo "已停止旧版 nohup 实例 (PID $pid)"
  fi
  rm -f "$LEGACY_PIDFILE"
}

wait_up() {
  local pid=""
  for _ in $(seq 1 15); do
    pid="$(current_pid)"
    if [[ -n "$pid" ]] && curl -fs -o /dev/null --max-time 2 "$URL"; then
      echo "运行中 (PID $pid)，面板: $URL"
      echo "日志: $LOG ；停止: $0 stop"
      return 0
    fi
    sleep 1
  done
  echo "启动后 15 秒内面板未就绪，请查看日志: $LOG" >&2
  tail -n 20 "$LOG" >&2 || true
  return 1
}

do_start() {
  stop_legacy
  if [[ -n "$(current_pid)" ]]; then
    echo "已在运行 (PID $(current_pid))，面板: $URL"
    return 0
  fi
  write_plist
  if is_loaded; then
    # 已托管但进程不在（正在自动重启间隔中），按新配置重新加载
    launchctl bootout "$TARGET" 2>/dev/null || true
    for _ in $(seq 1 10); do is_loaded || break; sleep 1; done
  fi
  launchctl bootstrap "$DOMAIN" "$PLIST"
  wait_up
}

do_stop() {
  local was=0
  stop_legacy
  if is_loaded; then
    launchctl bootout "$TARGET" 2>/dev/null || true
    # bootout 是异步卸载，等它真正退出，避免紧接着的 start/status 看到残留状态
    for _ in $(seq 1 10); do is_loaded || break; sleep 1; done
    was=1
  fi
  rm -f "$PLIST"
  if [[ $was == 1 ]]; then echo "已停止"; else echo "未在运行"; fi
}

case "${1:-up}" in
  up)
    do_start
    open "$URL"
    ;;
  start)
    do_start
    ;;
  stop)
    do_stop
    ;;
  restart)
    if is_loaded; then
      launchctl kickstart -k "$TARGET"
      wait_up
    else
      do_start
    fi
    ;;
  open)
    open "$URL"
    ;;
  status)
    pid="$(current_pid)"
    info() { { launchctl print "$TARGET" 2>/dev/null || true; } | awk '/runs =|last exit code/ { sub(/^[ \t]+/, "  "); print }'; }
    if [[ -n "$pid" ]]; then
      echo "运行中 (PID $pid)，面板: $URL"
      info
    elif is_loaded; then
      echo "已托管但当前未运行（launchd 会自动重启），日志: $LOG"
      info
      exit 1
    else
      echo "未在运行"
      exit 1
    fi
    ;;
  log|logs)
    tail -n 50 -f "$LOG"
    ;;
  *)
    echo "用法: $0 [start|stop|restart|status|log|open]  （无参数 = 启动并打开网页）" >&2
    exit 2
    ;;
esac
