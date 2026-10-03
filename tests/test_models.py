import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_model_watch as watch


def model(slug, priority=0, visibility="list"):
    return {"slug": slug, "display_name": slug.upper(), "priority": priority, "visibility": visibility}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.temp.name, "codex")
        self.state = os.path.join(self.temp.name, "watch")
        os.makedirs(self.home)
        self.auth("account-a")
        self.local([model("gpt-6.1-sol"), model("gpt-6-astra", 1)])
        self.original_catalog = watch.g_catalog
        self.original_conn = watch.conn_inst
        watch.conn_inst = watch.db_connect(os.path.join(self.state, "settings.db"))
        self.original_demo = watch.g_state["demo"]
        watch.g_state["demo"] = False

    def tearDown(self):
        watch.g_catalog = self.original_catalog
        watch.conn_inst.close()
        watch.conn_inst = self.original_conn
        watch.g_state["demo"] = self.original_demo
        self.temp.cleanup()

    def auth(self, account):
        Path(self.home, "auth.json").write_text(json.dumps({"tokens": {"access_token": "test-token", "account_id": account}}))

    def local(self, models):
        Path(self.home, "models_cache.json").write_text(json.dumps({"models": models, "client_version": "0.160.0", "fetched_at": "2026-01-01T00:00:00Z"}))

    def response(self, rows):
        return io.BytesIO(json.dumps({"models": rows}).encode())

    def test_filter_visibility_deduplicate_and_select_numeric_version(self):
        rows = [model("gpt-6.1-sol", 1), model("gpt-6.10-sol", 8), model("gpt-6.2-sol", 0),
                model("gpt-7-astra", 2), model("gpt-6-astra", 0), model("gpt-6.10-sol", 0),
                model("codex-auto-review", visibility="hide"), model("<script>"),
                model("gpt-reserve", visibility="hide"), None, {"slug": "unlisted"}]
        models = watch.normalize_models(rows)
        self.assertEqual(len(models), 5)
        self.assertEqual(watch.latest_auto_models(models), ["gpt-6.10-sol", "gpt-7-astra"])

    def test_new_versions_refresh_api_targets_and_preserve_history(self):
        catalog = watch.ModelCatalog(self.home, self.state)
        watch.g_catalog = catalog
        self.assertFalse(catalog.snapshot()["verified"])
        self.assertEqual(watch.auto_targets(), [])
        with patch.object(watch.urllib.request, "urlopen", return_value=self.response([model("gpt-7-sol"), model("gpt-7-astra")])) as request:
            catalog.refresh()
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_method(), "GET")
        self.assertIn("client_version=0.160.0", sent.full_url)
        db = watch.db_connect(os.path.join(self.state, "test.db"))
        self.addCleanup(db.close)
        db.execute("INSERT INTO probes VALUES('old', 'gpt-5.6-sol', 'gpt-5.6-sol', 0, 42, '', NULL)")
        db.commit()
        data = watch.api_data(db)
        self.assertEqual(data["auto"]["models"], ["gpt-7-sol", "gpt-7-astra"])
        self.assertEqual([m["slug"] for m in data["catalog"]["models"]], ["gpt-7-astra", "gpt-7-sol"])
        self.assertIn("gpt-5.6-sol", [m["model"] for m in data["probe_stats"]])
        with patch.object(watch.urllib.request, "urlopen", return_value=self.response([model("gpt-8.2-sol"), model("gpt-8-astra")])):
            catalog.refresh()
        self.assertEqual(watch.auto_targets(), ["gpt-8.2-sol", "gpt-8-astra"])
        frontend = Path(watch.WEB_DIR, "index.html").read_text()
        self.assertIn('list="modelOptions"', frontend)
        self.assertIn("renderCatalog(d.catalog)", frontend)
        self.assertIn("option.value=m.slug", frontend)

    def test_offline_preserves_success_and_bound_persistent_cache(self):
        catalog = watch.ModelCatalog(self.home, self.state)
        rows = [model("gpt-7-sol"), model("gpt-7-astra")]
        with patch.object(watch.urllib.request, "urlopen", return_value=self.response(rows)):
            catalog.refresh()
        stamp = catalog.snapshot()["updated_at"]
        with patch.object(watch.urllib.request, "urlopen", side_effect=TimeoutError):
            data = catalog.refresh()
        self.assertEqual(data["status"], "stale")
        self.assertEqual(data["updated_at"], stamp)
        self.assertEqual(catalog.auto_targets(), ["gpt-7-sol", "gpt-7-astra"])
        restored = watch.ModelCatalog(self.home, self.state)
        self.assertEqual(restored.snapshot()["source"], "watch-cache")
        self.assertEqual(restored.auto_targets(), catalog.auto_targets())
        saved = Path(self.state, "models.json").read_text()
        self.assertNotIn("test-token", saved)
        self.assertNotIn("account-a", saved)

    def test_account_change_revokes_availability(self):
        catalog = watch.ModelCatalog(self.home, self.state)
        with patch.object(watch.urllib.request, "urlopen", return_value=self.response([model("gpt-7-sol")])):
            catalog.refresh()
        self.auth("account-b")
        self.assertEqual(catalog.auto_targets(), [])
        self.assertFalse(catalog.snapshot()["verified"])
        restored = watch.ModelCatalog(self.home, self.state)
        self.assertEqual(restored.snapshot()["source"], "codex-cache")
        self.assertEqual(restored.auto_targets(), [])
        with patch.object(watch.urllib.request, "urlopen", side_effect=TimeoutError):
            catalog.refresh()
        self.assertFalse(catalog.snapshot()["verified"])

    def test_invalid_cache_and_invalid_response_are_recoverable(self):
        Path(self.home, "models_cache.json").write_text("{bad")
        os.makedirs(self.state, exist_ok=True)
        Path(self.state, "models.json").write_text("null")
        catalog = watch.ModelCatalog(self.home, self.state)
        with patch.object(watch.urllib.request, "urlopen") as request:
            data = catalog.refresh()
        request.assert_not_called()
        self.assertEqual(data["models"], [])
        self.assertEqual(data["status"], "unavailable")
        self.local([model("gpt-6-sol")])
        with patch.object(watch.urllib.request, "urlopen", return_value=io.BytesIO(b'{"models":"bad"}')):
            data = catalog.refresh()
        self.assertFalse(data["verified"])
        with patch.object(watch.urllib.request, "urlopen", return_value=self.response([model("gpt-8-sol")])):
            self.assertEqual(catalog.refresh()["status"], "ready")

    def test_empty_live_directory_survives_restart(self):
        catalog = watch.ModelCatalog(self.home, self.state)
        with patch.object(watch.urllib.request, "urlopen", return_value=self.response([])):
            self.assertEqual(catalog.refresh()["models"], [])
        restored = watch.ModelCatalog(self.home, self.state)
        self.assertEqual(restored.snapshot()["models"], [])
        self.assertEqual(restored.auto_targets(), [])

    def test_account_switch_during_refresh_does_not_publish_old_response(self):
        catalog = watch.ModelCatalog(self.home, self.state)
        def switch(*args, **kwargs):
            self.auth("account-b")
            return self.response([model("gpt-9-sol")])
        with patch.object(watch.urllib.request, "urlopen", side_effect=switch):
            catalog.refresh()
        self.assertFalse(catalog.snapshot()["verified"])
        self.assertEqual(catalog.auto_targets(), [])

    def test_auto_probe_plan_is_bound_to_catalog_account(self):
        catalog = watch.ModelCatalog(self.home, self.state)
        with patch.object(watch.urllib.request, "urlopen", return_value=self.response([model("gpt-7-sol")])):
            catalog.refresh()
        targets, binding = catalog.auto_plan()
        self.assertEqual(targets, ["gpt-7-sol"])
        self.auth("account-b")
        with patch.object(watch.urllib.request, "urlopen") as request:
            result = watch.run_probe(self.home, targets[0], expected_binding=binding)
        request.assert_not_called()
        self.assertIn("账户已变化", result["error"])

    def test_http_catalog_and_bad_request_json(self):
        catalog = watch.ModelCatalog(self.home, self.state)
        watch.g_catalog = catalog
        server = watch.ThreadingHTTPServer(("127.0.0.1", 0), watch.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_port
        try:
            with urllib.request.urlopen(base + "/api/models", timeout=1) as response:
                self.assertEqual(json.load(response)["source"], "codex-cache")
            for body in (b'[]', b'{"model": {}}', b'{"model":"<script>"}'):
                request = urllib.request.Request(base + "/api/probe", data=body, method="POST")
                with self.assertRaises(watch.urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=1)
                self.assertEqual(caught.exception.code, 400)
                self.assertIn("error", json.load(caught.exception))
                caught.exception.close()
            with urllib.request.urlopen(urllib.request.Request(base + "/api/models/refresh", method="POST"), timeout=1) as response:
                self.assertEqual(response.status, 202)
                self.assertTrue(catalog.wake.is_set())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(1)

    def test_demo_never_reads_auth_or_sends_network(self):
        with patch.object(watch, "load_auth", side_effect=AssertionError("real auth read")), patch.object(watch.urllib.request, "urlopen") as request:
            catalog = watch.ModelCatalog(self.home, self.state, demo=True)
            catalog.refresh()
            catalog.loop()
            catalog.request_refresh()
            watch.g_state["demo"] = True
            self.assertIn("error", watch.run_probe(self.home, "gpt-7-sol"))
        request.assert_not_called()
        self.assertEqual(catalog.auto_targets(), [])

    def test_health_is_fast_during_slow_network_and_held_database_lock(self):
        catalog = watch.ModelCatalog(self.home, self.state)
        watch.g_catalog = catalog
        started, release = threading.Event(), threading.Event()
        def slow_request(*args, **kwargs):
            started.set()
            release.wait(3)
            return self.response([model("gpt-7-sol")])
        server = watch.ThreadingHTTPServer(("127.0.0.1", 0), watch.Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with patch.object(watch.urllib.request, "urlopen", side_effect=slow_request):
                refresh = threading.Thread(target=catalog.refresh)
                refresh.start()
                self.assertTrue(started.wait(1))
                # HTTP client uses an unpatched opener; only the catalog's network is mocked.
                opener = urllib.request.build_opener()
                with watch.g_lock:
                    begin = time.monotonic()
                    with opener.open("http://127.0.0.1:%d/api/health" % server.server_port, timeout=1) as response:
                        data = json.load(response)
                    self.assertLess(time.monotonic() - begin, 0.5)
                    self.assertEqual(data, {"service": "codex-model-watch", "status": "ok", "pid": os.getpid()})
                release.set()
                refresh.join(2)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            server_thread.join(1)

    def test_wal_api_reads_during_uncommitted_scan_write(self):
        path = os.path.join(self.state, "scan.db")
        reader, writer = watch.db_connect(path), watch.db_connect(path)
        try:
            writer.execute("INSERT INTO turns(file, turn_id, ts, served) VALUES('a','b','now','gpt-7-sol')")
            begin = time.monotonic()
            data = watch.api_data(reader)
            self.assertLess(time.monotonic() - begin, 0.5)
            self.assertEqual(data["coverage"]["turns_total"], 0)
            writer.commit()
            self.assertEqual(watch.api_data(reader)["coverage"]["turns_total"], 1)
        finally:
            writer.rollback()
            writer.close()
            reader.close()


if __name__ == "__main__":
    unittest.main()
