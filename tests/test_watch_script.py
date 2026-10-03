import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest


LAUNCHCTL_SOURCE = '''
import json, os, sys
from pathlib import Path
p = Path(os.environ['FAKE_LAUNCHCTL_STATE'])
state = json.loads(p.read_text())
args = sys.argv[1:]
state['calls'].append(args)
code = 0
if args[0] == 'print':
    if args[1] in state['loaded']:
        print('state = running\\npid = 4242\\nruns = 1')
    else: code = 1
elif args[0] == 'bootstrap':
    state['loaded'].append(args[1] + '/com.codex-model-watch')
elif args[0] == 'bootout':
    if args[1] in state['loaded']: state['loaded'].remove(args[1])
p.write_text(json.dumps(state))
sys.exit(code)
'''


class WatchScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='watch tests ')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        bin_dir = self.directory / 'bin'
        bin_dir.mkdir()
        self.state_file = self.directory / 'launchctl.json'
        self.state_file.write_text(json.dumps({'loaded': [], 'calls': []}))
        self.env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ.get('PATH', ''),
                        WATCH_STATE_DIR=str(self.directory / 'state & files'),
                        WATCH_LEGACY_AGENT_PLIST=str(self.directory / 'LaunchAgents' / 'com.codex-model-watch.plist'),
                        PYTHON=sys.executable, FAKE_LAUNCHCTL_STATE=str(self.state_file), PORT='45678')
        self.script = Path(__file__).resolve().parents[1] / 'watch.sh'
        fixtures = {
            'launchctl': '#!%s\n%s' % (sys.executable, LAUNCHCTL_SOURCE),
            'curl': '#!%s\nimport os\nprint(os.environ.get("FAKE_HEALTH", \'{"service":"codex-model-watch","status":"ok","pid":4243}\'))\n' % sys.executable,
            'lsof': '#!%s\nimport os,sys\nsys.exit(0 if os.environ.get("FAKE_PORT_BUSY") else 1)\n' % sys.executable,
        }
        for name, source in fixtures.items():
            path = bin_dir / name
            path.write_text(source)
            path.chmod(0o755)

    def run_script(self, command, expected=0, **env):
        result = subprocess.run(['bash', str(self.script), command], env=dict(self.env, **env),
                                capture_output=True, text=True, errors='replace', timeout=10)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def state(self):
        return json.loads(self.state_file.read_text())

    def plist(self):
        path = Path(self.env['WATCH_STATE_DIR']) / 'com.codex-model-watch.plist'
        with path.open('rb') as f:
            return plistlib.load(f)

    def test_manual_start_is_idempotent_and_stop_unloads(self):
        self.run_script('start')
        self.run_script('start')
        state = self.state()
        self.assertEqual(len([c for c in state['calls'] if c[0] == 'bootstrap']), 1)
        self.assertEqual(state['loaded'], ['gui/%d/com.codex-model-watch' % os.getuid()])
        config = self.plist()
        self.assertTrue(config['KeepAlive'])
        self.assertFalse(config['AbandonProcessGroup'])
        self.assertIn('service_supervisor.py', config['ProgramArguments'][1])
        self.assertIn('45678', config['ProgramArguments'])
        self.assertNotIn('LaunchAgents', str(config))
        self.run_script('stop')
        self.assertEqual(self.state()['loaded'], [])
        self.run_script('status', expected=1)

    def test_restart_applies_new_port_and_status_preserves_it(self):
        self.run_script('start')
        self.run_script('restart', PORT='45679')
        self.assertEqual(self.plist()['ProgramArguments'][-2:], ['45679', '--no-open'])
        self.env.pop('PORT')
        result = self.run_script('status')
        self.assertIn('45679', result.stdout)
        self.assertEqual(len([c for c in self.state()['calls'] if c[0] == 'bootstrap']), 2)

    def test_unhealthy_page_is_not_reported_running(self):
        self.run_script('start')
        result = self.run_script('status', expected=1, FAKE_HEALTH='{"service":"other","status":"ok","pid":42}')
        self.assertIn('网页尚未响应', result.stdout)

    def test_migrates_loaded_user_job(self):
        state = self.state()
        legacy = 'user/%d/com.codex-model-watch' % os.getuid()
        state['loaded'] = [legacy]
        self.state_file.write_text(json.dumps(state))
        self.run_script('start')
        calls = self.state()['calls']
        bootout = calls.index(['bootout', legacy])
        bootstrap = next(i for i, c in enumerate(calls) if c[0] == 'bootstrap')
        self.assertLess(bootout, bootstrap)

    def test_port_conflict_does_not_bootstrap_or_kill_other_process(self):
        result = self.run_script('start', expected=1, FAKE_PORT_BUSY='1')
        self.assertIn('已被其他进程占用', result.stderr)
        self.assertFalse(any(c[0] == 'bootstrap' for c in self.state()['calls']))

    def test_invalid_port_is_rejected_before_loading(self):
        self.run_script('start', expected=2, PORT='not-a-port')
        self.assertEqual(self.state()['calls'], [])

    def create_legacy_agent(self, label='com.codex-model-watch'):
        path = Path(self.env['WATCH_LEGACY_AGENT_PLIST'])
        path.parent.mkdir()
        with path.open('wb') as f:
            plistlib.dump({'Label': label, 'RunAtLoad': True,
                           'ProgramArguments': [sys.executable, str(self.script.parent / 'codex_model_watch.py')]}, f)
        return path

    def test_start_moves_old_login_agent_out_of_startup_directory(self):
        old = self.create_legacy_agent()
        original = old.read_bytes()
        self.run_script('start')
        self.assertFalse(old.exists())
        backups = list(Path(self.env['WATCH_STATE_DIR']).glob('legacy-login-agent-*.plist'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_stop_also_removes_old_login_entry_without_starting(self):
        old = self.create_legacy_agent()
        self.run_script('stop')
        self.assertFalse(old.exists())
        self.assertFalse(any(c[0] == 'bootstrap' for c in self.state()['calls']))

    def test_preserves_unrelated_login_agent(self):
        old = self.create_legacy_agent(label='com.other-service')
        self.run_script('start')
        self.assertTrue(old.exists())
        self.assertFalse(list(Path(self.env['WATCH_STATE_DIR']).glob('legacy-login-agent-*.plist')))


if __name__ == '__main__':
    unittest.main()
