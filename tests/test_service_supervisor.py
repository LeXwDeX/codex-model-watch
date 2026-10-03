import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from service_supervisor import Supervisor, check_health


CHILD_SOURCE = '''
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
port, history, hang = sys.argv[1:]
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if Path(hang).exists():
            Path(hang).unlink()
            time.sleep(5)
        body = json.dumps({'service': 'codex-model-watch', 'status': 'ok', 'pid': os.getpid()}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
HTTPServer.allow_reuse_address = True
server = HTTPServer(('127.0.0.1', int(port)), Handler)
with open(history, 'a') as f: f.write(str(os.getpid()) + '\\n')
server.serve_forever()
'''


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError('Timed out waiting for service state')


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        child = self.directory / 'child.py'
        child.write_text(CHILD_SOURCE)
        self.history = self.directory / 'pids'
        self.hang = self.directory / 'hang'
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            self.port = s.getsockname()[1]
        self.url = 'http://127.0.0.1:%d/api/health' % self.port
        self.command = [sys.executable, str(child), str(self.port), str(self.history), str(self.hang)]

    def start_supervisor(self):
        supervisor = Supervisor(self.command, self.url, interval=0.05, failure_limit=2,
                                startup_grace=1, restart_delay=0.05, health_timeout=0.1,
                                stop_timeout=1)
        thread = threading.Thread(target=supervisor.run)
        thread.start()

        def cleanup():
            supervisor.request_stop()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.addCleanup(cleanup)
        wait_for(lambda: supervisor.child is not None and check_health(self.url, supervisor.child.pid, 0.1))
        return supervisor, thread

    def test_child_exit_recovers_and_explicit_stop_stays_stopped(self):
        supervisor, thread = self.start_supervisor()
        old_pid = supervisor.child.pid
        supervisor.child.kill()
        wait_for(lambda: supervisor.child.pid != old_pid and check_health(self.url, supervisor.child.pid, 0.1))
        supervisor.request_stop()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(supervisor.child.poll())
        count = len(self.history.read_text().splitlines())
        time.sleep(0.2)
        self.assertEqual(count, len(self.history.read_text().splitlines()))

    def test_unresponsive_child_is_replaced(self):
        supervisor, _ = self.start_supervisor()
        old_pid = supervisor.child.pid
        self.hang.write_text('simulate a blocked HTTP request')
        wait_for(lambda: supervisor.child.pid != old_pid and check_health(self.url, supervisor.child.pid, 0.1))
        self.assertGreaterEqual(len(self.history.read_text().splitlines()), 2)

    def test_health_rejects_other_pid(self):
        supervisor, _ = self.start_supervisor()
        self.assertFalse(check_health(self.url, supervisor.child.pid + 1, 0.1))

    @unittest.skipUnless(hasattr(__import__('signal'), 'SIGHUP'), 'Unix signals required')
    def test_cli_ignores_hangup_and_sigterm_stops_child(self):
        import signal
        script = Path(__file__).resolve().parents[1] / 'service_supervisor.py'
        manager = subprocess.Popen([sys.executable, str(script), '--health-url', self.url,
                                    '--interval', '0.05', '--failure-limit', '2',
                                    '--startup-grace', '1', '--restart-delay', '0.05',
                                    '--'] + self.command,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup():
            if manager.poll() is None:
                manager.terminate()
                manager.wait(5)
        self.addCleanup(cleanup)
        wait_for(lambda: self.history.exists())
        child_pid = int(self.history.read_text().splitlines()[-1])
        wait_for(lambda: check_health(self.url, child_pid, 0.1))
        manager.send_signal(signal.SIGHUP)
        time.sleep(0.15)
        self.assertIsNone(manager.poll())
        self.assertTrue(check_health(self.url, child_pid, 0.1))
        manager.terminate()
        self.assertEqual(manager.wait(5), 0)
        self.assertFalse(check_health(self.url, child_pid, 0.1))
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)


if __name__ == '__main__':
    unittest.main()
