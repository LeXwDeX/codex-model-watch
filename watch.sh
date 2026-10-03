#!/usr/bin/env bash
# 仅手动启动。plist 保留在状态目录，不放入 LaunchAgents，不会开机/登录自启。
# launchd 重启管理器；管理器重启退出或失去响应的网页进程。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${WATCH_STATE_DIR:-$HOME/.codex-model-watch}"
LOG="${LOG:-$STATE_DIR/watch.log}"
LABEL="com.codex-model-watch"
PLIST="$STATE_DIR/$LABEL.plist"
LEGACY_PIDFILE="$STATE_DIR/watch.pid"
LEGACY_AGENT_PLIST="${WATCH_LEGACY_AGENT_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
DOMAIN="gui/$(id -u)"
TARGET="$DOMAIN/$LABEL"
LEGACY_TARGET="user/$(id -u)/$LABEL"

resolve_python() {
  local py="${PYTHON:-python3}"
  if [[ "$py" != /* ]]; then py="$(command -v "$py" || true)"; fi
  if [[ -z "$py" || ! -x "$py" ]]; then
    echo "找不到 Python 解释器（可用 PYTHON=/绝对路径 指定）" >&2
    exit 1
  fi
  printf '%s\n' "$py"
}

PY="$(resolve_python)"
# 未传 PORT 时沿用上次手动启动的端口。
PORT="${PORT:-$("$PY" - "$PLIST" <<'PY'
import plistlib, sys
try:
    with open(sys.argv[1], 'rb') as file:
        args = plistlib.load(file)['ProgramArguments']
    print(args[args.index('--port') + 1])
except (OSError, ValueError, KeyError, IndexError, plistlib.InvalidFileException):
    print(8787)
PY
)}"
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( 10#$PORT < 1 || 10#$PORT > 65535 )); then
  echo "PORT 必须是 1–65535 之间的整数" >&2
  exit 2
fi
PORT="$((10#$PORT))"
URL="http://127.0.0.1:$PORT/"
HEALTH_URL="${URL}api/health"

is_loaded() { launchctl print "${1:-$TARGET}" >/dev/null 2>&1; }
current_pid() {
  { launchctl print "${1:-$TARGET}" 2>/dev/null || true; } |
    awk '$1 == "pid" && $2 == "=" { print $3; exit }'
}
job_info() {
  { launchctl print "${1:-$TARGET}" 2>/dev/null || true; } |
    awk '/state =|runs =|last exit code =|last terminating signal =/ { sub(/^[ \t]+/, "  "); print }'
}
healthcheck() {
  curl --noproxy '*' -fs --connect-timeout 1 --max-time 2 "$HEALTH_URL" 2>/dev/null |
    "$PY" -c 'import json,sys
try:
    d=json.load(sys.stdin)
    ok=d.get("service")=="codex-model-watch" and d.get("status")=="ok" and isinstance(d.get("pid"),int) and d["pid"]>0
except (ValueError,AttributeError):
    ok=False
sys.exit(0 if ok else 1)'
}
action_log() {
  mkdir -p "$STATE_DIR" "$(dirname "$LOG")"
  printf '%s [watch-control] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG"
}

write_plist() {
  # plistlib 转义路径，并原子替换配置。launchd 不继承终端 PATH。
  CONFIG_CHANGED="$("$PY" - "$PLIST" "$PY" "$DIR" "$PORT" "$LOG" "$LABEL" <<'PY'
import os, plistlib, sys, tempfile
path, py, directory, port, log, label = sys.argv[1:]
config = {
    'Label': label,
    'ProgramArguments': [py, os.path.join(directory, 'service_supervisor.py'),
                         '--health-url', 'http://127.0.0.1:%s/api/health' % port,
                         '--', py, os.path.join(directory, 'codex_model_watch.py'),
                         '--port', port, '--no-open'],
    'WorkingDirectory': directory,
    'EnvironmentVariables': {'PYTHONUNBUFFERED': '1'},
    'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
    'AbandonProcessGroup': False, 'ProcessType': 'Background',
    'StandardOutPath': log, 'StandardErrorPath': log,
}
data = plistlib.dumps(config)
os.makedirs(os.path.dirname(path), exist_ok=True)
try:
    with open(path, 'rb') as file:
        unchanged = file.read() == data
except OSError:
    unchanged = False
if not unchanged:
    fd, temporary = tempfile.mkstemp(prefix='.watch-', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
print('0' if unchanged else '1')
PY
)"
  plutil -lint "$PLIST" >/dev/null
}

unload_job() {
  local target="$1"
  is_loaded "$target" || return 0
  action_log "卸载 $target"
  launchctl bootout "$target" 2>/dev/null || true
  for _ in {1..15}; do
    is_loaded "$target" || return 0
    sleep 1
  done
  echo "未能卸载 $target，请查看日志: $LOG" >&2
  return 1
}

stop_legacy() {
  # 早期版本可能留下登录自启入口。确认是本应用后移出自启目录并保留备份。
  local note
  note="$("$PY" - "$LEGACY_AGENT_PLIST" "$STATE_DIR" "$LABEL" <<'PY'
import os, plistlib, sys, time
path, state_dir, label = sys.argv[1:]
if not os.path.isfile(path):
    sys.exit(0)
try:
    with open(path, 'rb') as file:
        config = plistlib.load(file)
    args = config.get('ProgramArguments', [])
    owned = config.get('Label') == label and isinstance(args, list) and any(
        isinstance(arg, str) and os.path.basename(arg) in
        ('codex_model_watch.py', 'service_supervisor.py') for arg in args)
except (OSError, ValueError, AttributeError, plistlib.InvalidFileException):
    owned = False
if owned:
    os.makedirs(state_dir, exist_ok=True)
    backup = os.path.join(state_dir, 'legacy-login-agent-%d.plist' % time.time_ns())
    os.replace(path, backup)
    print('已移出旧登录自启入口，备份: ' + backup)
else:
    print('保留未确认属于本应用的登录配置: ' + path)
PY
)"
  if [[ -n "$note" ]]; then action_log "$note"; fi
  # 只处理已记录且命令确认属于本项目的旧 nohup 实例。
  [[ -f "$LEGACY_PIDFILE" ]] || return 0
  local pid command
  pid="$(cat "$LEGACY_PIDFILE")"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    command="$(ps -p "$pid" -o command=)"
    if [[ "$command" == *"$DIR/codex_model_watch.py"* ]]; then
      action_log "停止旧 nohup 网页进程 PID $pid"
      kill "$pid" 2>/dev/null || true
      for _ in {1..10}; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
      if kill -0 "$pid" 2>/dev/null; then
        echo "旧进程 PID $pid 尚未退出，未启动新实例" >&2
        return 1
      fi
    fi
  fi
  rm -f "$LEGACY_PIDFILE"
}

wait_up() {
  local pid
  for _ in {1..30}; do
    pid="$(current_pid)"
    if [[ -n "$pid" ]] && healthcheck; then
      echo "运行中 (管理器 PID $pid)，面板: $URL"
      echo "仅本次手动启动后托管，不会开机/登录自启。停止: $0 stop"
      echo "日志: $LOG"
      return 0
    fi
    sleep 1
  done
  echo "30 秒内网页未就绪。后台管理器仍会重试，请查看日志: $LOG" >&2
  job_info >&2
  tail -n 20 "$LOG" >&2 || true
  return 1
}

do_start() {
  local force="${1:-0}"
  mkdir -p "$STATE_DIR" "$(dirname "$LOG")"
  stop_legacy
  write_plist
  if is_loaded "$LEGACY_TARGET"; then unload_job "$LEGACY_TARGET"; fi
  if [[ "$force" == 0 && "$CONFIG_CHANGED" == 0 && -n "$(current_pid)" ]] && healthcheck; then
    echo "已在运行 (管理器 PID $(current_pid))，面板: $URL"
    return 0
  fi
  unload_job "$TARGET"
  # 端口被其他服务占用时直接说明，不终止不相关进程。
  if command -v lsof >/dev/null && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "端口 $PORT 已被其他进程占用。可用 PORT=其他端口 $0 start" >&2
    return 1
  fi
  action_log "手动启动 ${TARGET}，端口 $PORT"
  launchctl enable "$TARGET"
  launchctl bootstrap "$DOMAIN" "$PLIST"
  wait_up
}

do_stop() {
  local was=0
  action_log "手动停止（不再自动恢复）"
  stop_legacy
  if is_loaded "$TARGET"; then unload_job "$TARGET"; was=1; fi
  if is_loaded "$LEGACY_TARGET"; then unload_job "$LEGACY_TARGET"; was=1; fi
  if [[ "$was" == 1 ]]; then echo "已停止"; else echo "未在运行"; fi
  # 保留状态与配置。该目录不被 launchd 登录时自动加载。
}

case "${1:-up}" in
  up) do_start; open "$URL" ;;
  start) do_start ;;
  stop) do_stop ;;
  restart) do_start 1 ;;
  open) open "$URL" ;;
  status)
    target="$TARGET"
    if ! is_loaded "$target" && is_loaded "$LEGACY_TARGET"; then target="$LEGACY_TARGET"; fi
    pid="$(current_pid "$target")"
    if [[ -n "$pid" ]] && healthcheck; then
      echo "运行中 (管理器 PID $pid)，网页健康: $URL"
      job_info "$target"
    elif is_loaded "$target"; then
      echo "已托管，网页尚未响应；管理器会自动恢复。日志: $LOG"
      job_info "$target"
      exit 1
    else
      echo "未在运行。手动启动: $0 start"
      echo "最近记录: $LOG"
      exit 1
    fi
    ;;
  log|logs) tail -n 50 -f "$LOG" ;;
  *) echo "用法: $0 [start|stop|restart|status|log|open]（无参数 = 手动启动并打开网页）" >&2; exit 2 ;;
esac
