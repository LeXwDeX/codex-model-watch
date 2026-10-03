#!/usr/bin/env python3
"""Keep a manually started dashboard healthy. launchd manages this supervisor."""
import argparse
import json
import os
import signal
import subprocess
import threading
import time
import urllib.request
from datetime import datetime


def log(message):
    print("%s [watch-supervisor] %s" %
          (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def check_health(url, expected_pid, timeout=2):
    """Check the child itself, rather than another listener on the same port."""
    try:
        # A proxy configured for external requests must not intercept loopback.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as response:
            body = json.loads(response.read(4096))
        return (body.get("service") == "codex-model-watch"
                and body.get("status") == "ok"
                and body.get("pid") == expected_pid)
    except (OSError, ValueError, AttributeError):
        return False


class Supervisor:
    def __init__(self, command, health_url, interval=5, failure_limit=6,
                 startup_grace=30, restart_delay=3, health_timeout=2,
                 stop_timeout=5):
        self.command = command
        self.health_url = health_url
        self.interval = interval
        self.failure_limit = failure_limit
        self.startup_grace = startup_grace
        self.restart_delay = restart_delay
        self.health_timeout = health_timeout
        self.stop_timeout = stop_timeout
        self.stopping = threading.Event()
        self.child = None

    def request_stop(self, signum=None, _frame=None):
        if signum is not None:
            log("收到 %s，停止网页进程" % signal.Signals(signum).name)
        self.stopping.set()

    def stop_child(self):
        child = self.child
        if child is None or child.poll() is not None:
            return
        try:
            child.terminate()
            child.wait(timeout=self.stop_timeout)
        except subprocess.TimeoutExpired:
            log("网页进程未响应 SIGTERM，结束 PID %d" % child.pid)
            child.kill()
            child.wait()
        except ProcessLookupError:
            child.wait()

    def run(self):
        try:
            while not self.stopping.is_set():
                try:
                    # Keep the child in launchd's process group so launchd can
                    # also clean up an orphaned child when this job stops.
                    self.child = subprocess.Popen(self.command)
                except OSError as exc:
                    log("无法启动网页进程：%s" % exc)
                    return 1
                child = self.child
                log("启动网页进程 PID %d（管理器 PID %d）" % (child.pid, os.getpid()))
                deadline = time.monotonic() + self.startup_grace
                failures, ready = 0, False
                while not self.stopping.wait(self.interval):
                    code = child.poll()
                    if code is not None:
                        log("网页进程 PID %d 退出，exit=%d；准备重启" % (child.pid, code))
                        break
                    if check_health(self.health_url, child.pid, self.health_timeout):
                        if not ready:
                            log("网页健康检查通过，PID %d" % child.pid)
                        failures, ready = 0, True
                    elif ready or time.monotonic() >= deadline:
                        failures += 1
                        if failures >= self.failure_limit:
                            log("网页连续 %d 次健康检查失败；重启 PID %d" % (failures, child.pid))
                            break
                self.stop_child()
                if self.stopping.wait(self.restart_delay):
                    break
        finally:
            self.stop_child()
        log("管理器已停止")
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--failure-limit", type=int, default=6)
    parser.add_argument("--startup-grace", type=float, default=30)
    parser.add_argument("--restart-delay", type=float, default=3)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("缺少网页进程命令")
    if args.interval <= 0 or args.failure_limit < 1 or args.startup_grace < 0 or args.restart_delay < 0:
        parser.error("检查间隔必须大于 0，失败次数至少为 1，等待时间不能为负")
    supervisor = Supervisor(command, args.health_url, args.interval,
                            args.failure_limit, args.startup_grace, args.restart_delay)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, supervisor.request_stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: log("忽略 SIGHUP，继续托管网页进程"))
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
