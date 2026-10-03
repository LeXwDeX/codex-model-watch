#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
codex-model-watch —— 本地监控 Codex 的模型使用、额度水位、容量拒单，并主动探测模型偷换。

原理（详见 README）：
  1. 日志侧：Codex 会把每一轮会话写入 ~/.codex/sessions/**/*.jsonl（rollout 文件）。
     本工具无侵入地增量解析这些文件，得到：每个 turn 实际生效的模型（turn_context.model）、
     token 用量（token_usage_record）、时长、容量拒单错误（task_complete.error，例如
     "Selected model is at capacity"）、以及额度水位（token_count.rate_limits 里的
     5h/7d 窗口用量百分比）。
     注意：实测 Codex 会把被偷换后的模型一致化写进日志（请求字段与实际字段相同），
     因此「被偷换成了什么」无法从日志还原 —— 这正是探针存在的意义。
  2. 探针侧：用你本地的 Codex 登录态（~/.codex/auth.json）向
     chatgpt.com/backend-api/codex/responses 发一条最小请求，读取 SSE
     response.created 事件里服务端实际派出的模型，即可即时验证「请求 X 会被派什么」。
     每次探针只消耗极少量额度，可手动触发也可定时执行。

所有数据只存在本机（SQLite），面板为本地网页，没有任何遥测。
"""
import argparse
import glob
import hashlib
import json
import os
import random
import re
import signal
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
APP_DIR = os.path.join(HOME, ".codex-model-watch")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
BACKEND_URL = "https://chatgpt.com/backend-api/codex/responses"
MODELS_URL = "https://chatgpt.com/backend-api/codex/models"

g_lock = threading.RLock()
g_last_scan = 0.0
g_state = {"demo": False}


def log(msg):
    # 后台运行时 stdout 被重定向到文件，必须 flush，否则进程被杀时缓冲内容全部丢失
    print("%s [codex-model-watch] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- db

def db_connect(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    # 单进程多线程，统一用 g_lock 串行化；check_same_thread 关掉以复用连接
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS files(
        path TEXT PRIMARY KEY, offset INTEGER DEFAULT 0, size INTEGER DEFAULT 0,
        mtime REAL DEFAULT 0, lines INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS turns(
        file TEXT, turn_id TEXT, ts TEXT, session_id TEXT, project TEXT,
        requested TEXT, served TEXT, effort TEXT,
        in_tokens INTEGER DEFAULT 0, cached_tokens INTEGER DEFAULT 0, out_tokens INTEGER DEFAULT 0,
        duration_ms INTEGER, ttft_ms INTEGER,
        error_kind TEXT, error_msg TEXT,
        UNIQUE(file, turn_id));
    CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);
    CREATE TABLE IF NOT EXISTS quota(
        ts TEXT PRIMARY KEY, primary_used REAL, secondary_used REAL, raw TEXT);
    CREATE TABLE IF NOT EXISTS probes(
        ts TEXT PRIMARY KEY, requested TEXT, served TEXT, swapped INTEGER,
        latency_ms INTEGER, safety_header TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS threads(
        thread_id TEXT PRIMARY KEY, requested TEXT);
    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY, value TEXT);
    """)
    return conn


# ---------------------------------------------------------------- 扫描解析

def iso_now():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)


def classify_error(msg, info):
    if info:
        if not isinstance(info, str):
            info = json.dumps(info, ensure_ascii=False)
        return info[:60]
    m = (msg or "").lower()
    if "at capacity" in m:
        return "capacity"
    if "rate limit" in m:
        return "rate_limit"
    if "usage limit" in m or "limit reached" in m:
        return "usage_limit"
    return "error"


def parse_lines(lines, file_key, conn, stats):
    """解析一批 rollout 行；返回需写入的行集合。"""
    turns, quota_rows = [], []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        ts = obj.get("timestamp") or ""
        t = obj.get("type")
        p = obj.get("payload")
        if not isinstance(p, dict):
            continue
        pt = p.get("type", "")
        if t == "turn_context":
            tid = p.get("turn_id")
            if not tid:
                continue
            collab = p.get("collaboration_mode") or {}
            settings = collab.get("settings") or {}
            requested = settings.get("model") or ""
            served = p.get("model") or ""
            cwd = p.get("cwd") or ""
            project = os.path.basename(cwd.rstrip("\\/")) if cwd else ""
            # turn 行必须遇到就立刻插入：同一文件里 token_usage/task_complete 的 UPDATE 在其后到达
            conn.execute("""INSERT INTO turns(file, turn_id, ts, session_id, project, requested, served, effort)
                            VALUES(?,?,?,?,?,?,?,?)
                            ON CONFLICT(file, turn_id) DO UPDATE SET
                              ts=COALESCE(excluded.ts, turns.ts),
                              project=CASE WHEN excluded.project!='' THEN excluded.project ELSE turns.project END,
                              requested=COALESCE(NULLIF(excluded.requested,''), turns.requested),
                              served=COALESCE(NULLIF(excluded.served,''), turns.served),
                              effort=COALESCE(NULLIF(excluded.effort,''), turns.effort)""",
                         (file_key, tid, ts or None, "", project, requested, served, p.get("effort") or ""))
            stats["turns"] += 1
        elif t == "event_msg" and pt == "thread_settings_applied":
            tid = p.get("thread_id")
            ts_model = ((p.get("thread_settings") or {}).get("model")) or ""
            if tid and ts_model:
                conn.execute("INSERT INTO threads(thread_id, requested) VALUES(?,?) "
                             "ON CONFLICT(thread_id) DO UPDATE SET requested=excluded.requested", (tid, ts_model))
        elif t == "token_usage_record":
            tid = p.get("turn_id")
            usage = p.get("turn_token_usage") or p.get("usage") or {}
            if tid and usage:
                conn.execute("UPDATE turns SET in_tokens=?, cached_tokens=?, out_tokens=? "
                             "WHERE file=? AND turn_id=?",
                             (usage.get("input_tokens") or 0, usage.get("cached_input_tokens") or 0,
                              usage.get("output_tokens") or 0, file_key, tid))
        elif t == "event_msg" and pt == "task_complete":
            tid = p.get("turn_id")
            if not tid:
                continue
            err = p.get("error") or None
            kind = msg = None
            if isinstance(err, dict):
                msg = err.get("message") or ""
                if not isinstance(msg, str):
                    msg = json.dumps(msg, ensure_ascii=False)
                msg = msg[:300]
                kind = classify_error(msg, err.get("codex_error_info"))
            conn.execute("UPDATE turns SET duration_ms=?, ttft_ms=?, error_kind=COALESCE(?,error_kind), "
                         "error_msg=? WHERE file=? AND turn_id=?",
                         (p.get("duration_ms"), p.get("time_to_first_token_ms"), kind, msg, file_key, tid))
            if kind:
                stats["errors"] += 1
        elif t == "event_msg" and pt == "token_count":
            rl = p.get("rate_limits") or {}
            prim = (rl.get("primary") or {}).get("used_percent")
            sec = (rl.get("secondary") or {}).get("used_percent")
            if ts and (prim is not None or sec is not None):
                conn.execute("INSERT INTO quota(ts, primary_used, secondary_used, raw) VALUES(?,?,?,?) "
                             "ON CONFLICT(ts) DO NOTHING", (ts, prim, sec, json.dumps(rl)))


def scan_sessions(conn, codex_home, max_age_days):
    """增量扫描 sessions 目录；返回统计。"""
    sessions_dir = os.path.join(codex_home, "sessions")
    if not os.path.isdir(sessions_dir):
        return {"files": 0, "turns": 0, "errors": 0, "note": "sessions 目录不存在: " + sessions_dir}
    pattern = os.path.join(sessions_dir, "**", "*.jsonl")
    files = glob.glob(pattern, recursive=True)
    cutoff_ts = 0
    if max_age_days > 0:
        cutoff = time.time() - max_age_days * 86400
        cutoff_ts = cutoff
    files = [f for f in files if os.path.getmtime(f) >= cutoff_ts] if max_age_days > 0 else files
    stats = {"files": 0, "turns": 0, "errors": 0}
    for fp in sorted(files):
        try:
            st = os.stat(fp)
        except OSError:
            continue
        row = conn.execute("SELECT offset, mtime, size, lines FROM files WHERE path=?", (fp,)).fetchone()
        # 续读策略：文件被截断/重写则从头解析，否则从上次 offset 续读新增部分
        start, resume, unchanged = 0, False, False
        if row:
            if st.st_size > row["offset"]:
                start, resume = row["offset"], True
            elif st.st_size == row["offset"] and row["mtime"] == st.st_mtime:
                unchanged = True
        if unchanged:
            continue  # 无新内容
        if not resume:
            conn.execute("DELETE FROM turns WHERE file=?", (fp,))
            start = 0
        with open(fp, "rb") as fh:
            fh.seek(start)
            consumed, lines = 0, []
            while True:
                chunk = fh.readline()
                if not chunk:
                    break
                consumed += len(chunk)
                lines.append(chunk)
            # 最后一行可能不完整，回退 offset 到最后一个完整换行
            if lines and not lines[-1].endswith(b"\n"):
                tail = lines.pop()
                consumed -= len(tail)
            if lines:
                text = b"".join(lines).decode("utf-8", errors="replace")
                parse_lines(text.splitlines(), fp, conn, stats)
        old_lines = row["lines"] if (row and resume) else 0
        conn.execute("""INSERT INTO files(path, offset, size, mtime, lines) VALUES(?,?,?,?,?)
                        ON CONFLICT(path) DO UPDATE SET offset=excluded.offset, size=excluded.size,
                          mtime=excluded.mtime, lines=excluded.lines""",
                     (fp, start + consumed, st.st_size, st.st_mtime, old_lines + len(lines)))
        stats["files"] += 1
    conn.commit()
    return stats


# ---------------------------------------------------------------- 探针

def load_auth(codex_home):
    path = os.path.join(codex_home, "auth.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            auth = json.load(handle)
        tokens = auth.get("tokens") or {}
        tok = tokens.get("access_token")
        if not tok:
            return None
        return {"token": tok, "account": tokens.get("account_id", "")}
    except Exception:
        return None


def auth_binding(codex_home, auth):
    account = (auth or {}).get("account") or (auth or {}).get("token") or "signed-out"
    return hashlib.sha256((os.path.realpath(codex_home) + "\0" + account).encode()).hexdigest()


def run_probe(codex_home, model, expected_binding=None):
    import urllib.request
    import urllib.error
    if g_state["demo"]:
        return {"error": "演示模式不发送真实探针"}
    auth = load_auth(codex_home)
    if not auth:
        return {"error": "未找到 Codex 登录态（~/.codex/auth.json），请先用 Codex 登录"}
    if expected_binding is not None and auth_binding(codex_home, auth) != expected_binding:
        return {"error": "Codex 登录账户已变化，跳过本次自动探针"}
    body = json.dumps({
        "model": model,
        "instructions": "You are a helpful assistant.",
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": "hi"}]}],
        "stream": True, "store": False, "reasoning": {"effort": "low"},
    }).encode()
    req = urllib.request.Request(BACKEND_URL, data=body, method="POST")
    for k, v in [("Authorization", "Bearer " + auth["token"]),
                 ("chatgpt-account-id", auth["account"]),
                 ("Content-Type", "application/json"),
                 ("Accept", "text/event-stream"),
                 ("originator", "codex_cli_rs"),
                 ("User-Agent", "codex_cli_rs/0.154.0")]:
        req.add_header(k, v)
    t0 = time.time()
    served, safety, error = "", "", None
    try:
        resp = urllib.request.urlopen(req, timeout=90)
        safety = resp.headers.get("x-codex-safety-buffering-enabled", "") or ""
        buf = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            if b"response.created" in buf:
                break
            if len(buf) > 200000:
                break
        text = buf.decode(errors="replace")
        i = text.find('"model":"')
        if i >= 0:
            served = text[i + 9:text.find('"', i + 9)]
        if not served:
            error = "响应里没有找到模型字段"
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(300).decode(errors="replace")
        except Exception:
            detail = ""
        error = "HTTP %d %s" % (e.code, detail[:200])
    except Exception as e:
        error = str(e)[:200]
    latency = int((time.time() - t0) * 1000)
    swapped = 1 if (served and model and served != model) else 0
    row = (iso_now(), model, served, swapped, latency, safety, error)
    with g_lock:
        c = conn()
        c.execute("INSERT INTO probes(ts, requested, served, swapped, latency_ms, safety_header, error) "
                  "VALUES(?,?,?,?,?,?,?)", row)
        c.commit()
    return {"ts": row[0], "requested": model, "served": served, "swapped": bool(swapped),
            "latency_ms": latency, "safety_header": safety, "error": error}


# ---------------------------------------------------------------- 自动探针调度

def normalize_models(rows):
    """Only public picker entries; retain no account or auth data."""
    models = {}
    if not isinstance(rows, list):
        return []
    for row in rows:
        if not isinstance(row, dict) or row.get("visibility") != "list":
            continue
        slug = row.get("slug") or row.get("id")
        if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", slug):
            continue
        name = row.get("display_name")
        priority = row.get("priority")
        models.setdefault(slug, {"slug": slug, "display_name": name[:128] if isinstance(name, str) else slug,
                                 "priority": priority if isinstance(priority, int) else 9999,
                                 "visibility": "list"})
    return sorted(models.values(), key=lambda m: (m["priority"], m["slug"]))


def latest_auto_models(models):
    targets = []
    for family in ("sol", "astra"):
        candidates = []
        for model in models:
            match = re.fullmatch(r"gpt-(\d+(?:\.\d+)*)-" + family, model["slug"])
            if match:
                version = tuple(int(x) for x in match.group(1).split("."))
                candidates.append((version, -model["priority"], model["slug"]))
        if candidates:
            targets.append(max(candidates)[2])
    return targets


class ModelCatalog:
    """Refresh metadata in a separate thread. Never send an inference request."""
    def __init__(self, codex_home, state_dir, demo=False):
        self.codex_home = os.path.realpath(codex_home)
        self.cache_path = os.path.join(state_dir, "models.json")
        self.demo = demo
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.data = {"models": [], "source": "none", "status": "unavailable", "updated_at": None,
                     "error": "", "refreshing": False, "verified": False}
        self.binding = None
        self.client_version = None
        self._bootstrap()

    def _context(self):
        auth = load_auth(self.codex_home)
        return auth, auth_binding(self.codex_home, auth)

    @staticmethod
    def _read(path):
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _bootstrap(self):
        if self.demo:
            self.data.update(models=normalize_models([
                {"slug": "gpt-6.1-sol", "visibility": "list", "priority": 0},
                {"slug": "gpt-6-astra", "visibility": "list", "priority": 1}]),
                source="demo", status="demo", updated_at=iso_now())
            return
        _, self.binding = self._context()
        local = self._read(os.path.join(self.codex_home, "models_cache.json"))
        version = local.get("client_version")
        self.client_version = version if isinstance(version, str) and re.fullmatch(r"\d+(?:\.\d+){1,3}", version) else None
        # Codex's opaque cache identity cannot establish account availability.
        models = normalize_models(local.get("models"))
        if models:
            self.data.update(models=models, source="codex-cache", status="unverified",
                             updated_at=local.get("fetched_at"), verified=False)
        saved = self._read(self.cache_path)
        if (saved.get("binding") == self.binding and isinstance(saved.get("updated_at"), str)
                and isinstance(saved.get("models"), list)):
            models = normalize_models(saved.get("models"))
            self.data.update(models=models, source="watch-cache", status="stale",
                             updated_at=saved["updated_at"], verified=True)

    def snapshot(self):
        with self.lock:
            # Switching accounts immediately revokes old availability, even before refresh.
            if not self.demo and self._context()[1] != self.binding:
                self.data = {"models": [], "source": "none", "status": "unavailable", "updated_at": None,
                             "error": "Codex 登录账户已变化，等待刷新目录", "refreshing": False, "verified": False}
                self.wake.set()
            return dict(self.data, models=[dict(m) for m in self.data["models"]])

    def auto_targets(self):
        return self.auto_plan()[0]

    def auto_plan(self):
        with self.lock:
            data = self.snapshot()
            targets = latest_auto_models(data["models"]) if data["verified"] and not self.demo else []
            return targets, self.binding

    def refresh(self):
        if self.demo:
            return self.snapshot()
        auth, binding = self._context()
        with self.lock:
            if binding != self.binding:
                self.data = {"models": [], "source": "none", "status": "unavailable", "updated_at": None,
                             "error": "", "refreshing": False, "verified": False}
                self._bootstrap()
            self.data["refreshing"] = True
        try:
            if not auth:
                raise ValueError("未找到 Codex 登录态")
            # Re-read version metadata; Codex upgrades require no application edit.
            local = self._read(os.path.join(self.codex_home, "models_cache.json"))
            version = local.get("client_version")
            if isinstance(version, str) and re.fullmatch(r"\d+(?:\.\d+){1,3}", version):
                self.client_version = version
            if not self.client_version:
                raise ValueError("缺少 Codex 模型缓存中的 client_version；请先启动 Codex")
            request = urllib.request.Request(MODELS_URL + "?" + urllib.parse.urlencode({"client_version": self.client_version}))
            for key, value in (("Authorization", "Bearer " + auth["token"]),
                               ("chatgpt-account-id", auth["account"]), ("originator", "codex_cli_rs"),
                               ("User-Agent", "codex_cli_rs/" + self.client_version), ("Accept", "application/json")):
                request.add_header(key, value)
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("模型目录响应过大")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
                raise ValueError("模型目录响应格式无效")
            models = normalize_models(payload["models"])
            updated_at = iso_now()
            # Do not publish a response fetched with a previous account's credentials.
            if self._context()[1] != binding:
                raise ValueError("刷新期间 Codex 登录账户发生变化")
            with self.lock:
                self.data.update(models=models, source="live", status="ready", updated_at=updated_at,
                                 error="", verified=True)
            try:
                os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
                temp = self.cache_path + ".tmp"
                with open(temp, "w", encoding="utf-8") as handle:
                    json.dump({"binding": binding, "models": models, "updated_at": updated_at}, handle)
                os.replace(temp, self.cache_path)
            except OSError:
                log("模型目录已更新，但持久缓存写入失败")
        except Exception as exc:
            # Do not expose backend bodies, account identifiers, or credential-bearing URLs.
            if isinstance(exc, urllib.error.HTTPError):
                error = "目录刷新 HTTP %d" % exc.code
            elif isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError):
                error = str(exc)[:160]
            else:
                error = "目录刷新失败（%s）；将保留已有目录" % type(exc).__name__
            with self.lock:
                self.data.update(status="stale" if self.data["verified"] else
                                 ("unverified" if self.data["models"] else "unavailable"), error=error)
        finally:
            with self.lock:
                self.data["refreshing"] = False
        return self.snapshot()

    def request_refresh(self):
        if not self.demo:
            self.wake.set()

    def loop(self):
        if self.demo:
            return
        while True:
            self.wake.clear()
            self.refresh()
            self.wake.wait(300)


g_catalog = None


def catalog_state():
    if g_catalog is not None:
        return g_catalog.snapshot()
    return {"models": [], "source": "none", "status": "unavailable", "updated_at": None,
            "error": "目录尚未初始化", "refreshing": False, "verified": False}


def auto_targets():
    return g_catalog.auto_targets() if g_catalog is not None else []


NO_QUOTA_KEYS = ("429", "capacity", "rate limit", "rate_limit", "rate-limit",
                 "quota", "usage limit", "limit reached", "overloaded", "insufficient")
g_auto: dict = {"next_ts": None}


def setting_get(key, default=None):
    with g_lock:
        row = conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def setting_set(key, value):
    with g_lock:
        c = conn()
        c.execute("""INSERT INTO settings(key,value) VALUES(?,?)
                     ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, str(value)))
        c.commit()


def auto_enabled():
    return setting_get("auto_enabled", "0") == "1"


def set_auto_enabled(on, reason=None):
    setting_set("auto_enabled", "1" if on else "0")
    if reason is not None:
        setting_set("auto_pause_reason", reason)
    elif on:
        setting_set("auto_pause_reason", "")


def auto_state():
    targets = auto_targets()
    return {"enabled": auto_enabled(), "models": targets,
            "next_ts": g_auto.get("next_ts"),
            "waiting_for_catalog": not targets,
            "pause_reason": setting_get("auto_pause_reason") or ""}


def is_no_quota(result):
    err = ((result or {}).get("error") or "").lower()
    return bool(err) and any(k in err for k in NO_QUOTA_KEYS)


def next_hour_target(now=None):
    now = time.time() if now is None else now
    lt = time.localtime(now)
    hour = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, 0, 0, 0, 0, -1))
    target = hour + 3600 + random.uniform(-5, 5)
    if target <= now:
        target += 3600
    return target


def auto_probe_loop():
    while True:
        try:
            if not auto_enabled():
                g_auto["next_ts"] = None
                time.sleep(5)
                continue
            target = next_hour_target()
            g_auto["next_ts"] = target
            cancelled = False
            while time.time() < target:
                if not auto_enabled():
                    cancelled = True
                    break
                time.sleep(min(2.0, max(0.05, target - time.time())))
            if cancelled:
                continue
            g_auto["next_ts"] = None
            targets, binding = g_catalog.auto_plan() if g_catalog is not None else ([], None)
            for m in targets:
                if not auto_enabled():
                    break
                res = run_probe(g_args.codex_home, m, expected_binding=binding)
                if is_no_quota(res):
                    set_auto_enabled(False, reason="自动暂停：%s 无量/容量（%s）" % (m, (res.get("error") or "")[:100]))
                    break
                time.sleep(random.uniform(1, 3))
        except Exception:
            log("自动探针线程异常（30 秒后重试）:\n" + traceback.format_exc())
            time.sleep(30)


# ---------------------------------------------------------------- 聚合输出

def api_data(conn, days=0):
    cutoff = ""
    if days and days > 0:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def q(sql, args=()):
        return [dict(r) for r in conn.execute(sql, args).fetchall()]

    cov = conn.execute("SELECT MIN(ts) a, MAX(ts) b, COUNT(*) n FROM turns WHERE ts IS NOT NULL").fetchone()
    win = conn.execute("""SELECT COUNT(*) turns,
                                 COALESCE(SUM(in_tokens),0) tin, COALESCE(SUM(out_tokens),0) tout,
                                 COALESCE(SUM(cached_tokens),0) tcached,
                                 SUM(CASE WHEN error_kind IS NOT NULL THEN 1 ELSE 0 END) errors,
                                 SUM(CASE WHEN error_kind IN ('capacity','server_overloaded') THEN 1 ELSE 0 END) capacity,
                                 AVG(duration_ms) avg_dur
                          FROM turns WHERE (?='' OR ts>=?)""", (cutoff, cutoff)).fetchone()
    hourly = q("""SELECT substr(ts,1,13)||':00' bucket, COUNT(*) turns,
                         SUM(CASE WHEN error_kind IS NOT NULL THEN 1 ELSE 0 END) errors
                  FROM turns WHERE ts IS NOT NULL GROUP BY bucket ORDER BY bucket""")
    models = q("""SELECT COALESCE(NULLIF(served,''),'(未知)') model, COUNT(*) turns,
                         COALESCE(SUM(in_tokens),0) tin, COALESCE(SUM(out_tokens),0) tout,
                         AVG(duration_ms) avg_dur,
                         SUM(CASE WHEN error_kind IS NOT NULL THEN 1 ELSE 0 END) errors
                  FROM turns WHERE (?='' OR ts>=?) GROUP BY model ORDER BY turns DESC""", (cutoff, cutoff))
    projects = q("""SELECT COALESCE(NULLIF(project,''),'(未知)') project, COUNT(*) turns,
                           COALESCE(SUM(in_tokens+out_tokens),0) tokens
                    FROM turns WHERE (?='' OR ts>=?) GROUP BY project ORDER BY tokens DESC LIMIT 15""",
                 (cutoff, cutoff))
    errors_recent = q("""SELECT ts, COALESCE(NULLIF(served,''),'(未知)') model, error_kind, error_msg
                         FROM turns WHERE error_kind IS NOT NULL AND (?='' OR ts>=?)
                         ORDER BY ts DESC LIMIT 50""", (cutoff, cutoff))
    quota_latest = q("SELECT * FROM quota ORDER BY ts DESC LIMIT 1")
    quota_hist = q("SELECT ts, primary_used, secondary_used FROM quota ORDER BY ts DESC LIMIT 48")
    probes = q("SELECT * FROM probes ORDER BY ts DESC LIMIT 100")
    probe_summary = conn.execute("""SELECT COUNT(*) n, COALESCE(SUM(swapped),0) swapped
                                     FROM probes""").fetchone()
    pstats = {r["requested"]: r for r in q(
        """SELECT requested, COUNT(*) total,
                  COALESCE(SUM(CASE WHEN swapped=0 AND served<>'' THEN 1 ELSE 0 END),0) ok,
                  COALESCE(SUM(swapped),0) swapped
           FROM probes GROUP BY requested""")}
    latest = {r["requested"]: r for r in q(
        """SELECT p.requested, p.served, p.latency_ms, p.ts FROM probes p
           JOIN (SELECT requested, MAX(ts) mts FROM probes GROUP BY requested) x
             ON x.requested=p.requested AND x.mts=p.ts""")}
    probe_stats = []
    targets = auto_targets()
    for m in targets + [k for k in pstats if k not in targets]:
        a, b = pstats.get(m, {}), latest.get(m, {})
        probe_stats.append({"model": m, "served": b.get("served") or "",
                            "ok": a.get("ok", 0), "swapped": a.get("swapped", 0),
                            "total": a.get("total", 0),
                            "last_latency_ms": b.get("latency_ms"), "last_ts": b.get("ts")})
    total_models = sum(m["turns"] for m in models) or 1
    for m in models:
        m["share"] = round(m["turns"] * 100.0 / total_models, 1)
    return {
        "meta": {"generated_at": iso_now(), "demo": g_state["demo"]},
        "coverage": {"first": cov["a"], "last": cov["b"], "turns_total": cov["n"]},
        "summary": {"turns": win["turns"], "tokens_in": win["tin"], "tokens_out": win["tout"],
                    "tokens_cached": win["tcached"], "errors": win["errors"] or 0,
                    "capacity": win["capacity"] or 0,
                    "avg_duration_ms": int(win["avg_dur"] or 0)},
        "hourly": hourly, "models": models, "projects": projects,
        "errors_recent": errors_recent,
        "quota": {"latest": quota_latest[0] if quota_latest else None,
                  "history": list(reversed(quota_hist))},
        "probes": probes,
        "probe_summary": {"total": probe_summary["n"], "swapped": probe_summary["swapped"]},
        "probe_stats": probe_stats,
        "auto": auto_state(),
        "catalog": catalog_state(),
    }


# ---------------------------------------------------------------- demo 数据

def seed_demo(conn):
    """生成两周的演示数据（用于 README 截图与功能体验）。"""
    import random
    random.seed(42)
    models = [("gpt-5.6-sol", 0.52), ("gpt-6-astra", 0.24), ("gpt-5.6-terra", 0.12),
              ("gpt-5.6-luna", 0.08), ("codex-auto-review", 0.04)]
    projects = ["my-app", "blog", "data-scripts", "learn-rust"]
    now = datetime.now(timezone.utc)
    turns, quota, probes = [], [], []
    for day in range(13, -1, -1):
        base = now - timedelta(days=day)
        n_turn = random.randint(25, 90)
        # 剧情线：第 5 天起 astra 探针开始被偷换成 luna
        swapped_day = day <= 5
        q5 = max(0.0, min(100.0, 100 - day * random.uniform(6, 14)))
        for i in range(n_turn):
            r = random.random()
            acc = 0.0
            model = models[-1][0]
            for m, w in models:
                acc += w
                if r <= acc:
                    model = m
                    break
            ts = base.replace(hour=random.randint(8, 23), minute=random.randint(0, 59),
                              second=random.randint(0, 59), microsecond=0)
            tin = random.randint(8, 180) * 1000
            tout = random.randint(1, 40) * 100
            err = None
            if random.random() < 0.03:
                err = "capacity" if random.random() < 0.7 else "rate_limit"
            turns.append((("demo-%d-%d" % (day, i)), ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "demo-session", random.choice(projects), model, model, "high",
                          tin, int(tin * 0.3), tout,
                          random.randint(8000, 180000), random.randint(1200, 9000),
                          err, "Selected model is at capacity. Please try a different model." if err == "capacity" else None))
        quota.append((base.strftime("%Y-%m-%dT%H:00:00Z"), round(q5, 1),
                      round(min(100.0, q5 * 2.2), 1), "{}"))
        if day % 2 == 0 or swapped_day:
            for hm in (9, 15, 21):
                ts = base.replace(hour=hm, minute=random.randint(0, 59), second=0, microsecond=0)
                probes.append((ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "gpt-6-astra",
                               "gpt-5.6-luna" if swapped_day else "gpt-6-astra",
                               1 if swapped_day else 0, random.randint(900, 4000),
                               "true" if swapped_day else "", None))
    conn.executemany("""INSERT INTO turns(file, turn_id, ts, session_id, project, requested, served, effort,
                        in_tokens, cached_tokens, out_tokens, duration_ms, ttft_ms, error_kind, error_msg)
                        VALUES('demo', ?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(file, turn_id) DO NOTHING""",
                     [(t[0],) + t[1:] for t in turns])
    conn.executemany("INSERT INTO quota VALUES(?,?,?,?) ON CONFLICT(ts) DO NOTHING", quota)
    conn.executemany("INSERT INTO probes VALUES(?,?,?,?,?,?,?) ON CONFLICT(ts) DO NOTHING", probes)
    conn.commit()


# ---------------------------------------------------------------- HTTP 服务

WEB_HTML = None


def load_index():
    global WEB_HTML
    if WEB_HTML is None:
        with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
            WEB_HTML = f.read()
    return WEB_HTML


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log("GET 请求失败:\n" + traceback.format_exc())
            self._json({"error": "本地服务处理请求失败，请查看服务日志"}, 500)

    def _get(self):
        import urllib.parse
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            self._json({"service": "codex-model-watch", "status": "ok", "pid": os.getpid()})
            return
        if path == "/api/models":
            self._json(catalog_state())
            return
        if path in ("/", "/index.html"):
            data = load_index()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/data":
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                days = int((qs.get("days") or ["0"])[0])
            except ValueError:
                days = 0
            with g_lock:
                data = api_data(conn(), days)
            self._json(data)
            return
        self._json({"error": "接口不存在"}, 404)

    def do_POST(self):
        try:
            self._post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log("POST 请求失败:\n" + traceback.format_exc())
            self._json({"error": "本地服务处理请求失败，请查看服务日志"}, 500)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > 4096:
                raise ValueError("请求体过大")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return body
        except (ValueError, TypeError):
            self._json({"error": "请求体必须是小于 4KB 的 JSON 对象"}, 400)
            return None

    def _post(self):
        path0 = self.path.split("?")[0]
        if path0 == "/api/models/refresh":
            if g_catalog is not None:
                g_catalog.request_refresh()
            self._json(catalog_state(), 202)
            return
        if path0 == "/api/auto":
            body = self._body()
            if body is None:
                return
            action = body.get("action")
            if action == "start":
                set_auto_enabled(True)
            elif action == "pause":
                set_auto_enabled(False, reason="手动暂停")
            else:
                self._json({"error": "action 必须是 start 或 pause"}, 400)
                return
            self._json(auto_state())
            return
        if path0 == "/api/probe":
            body = self._body()
            if body is None:
                return
            model = body.get("model")
            if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model.strip()):
                self._json({"error": "model 必须是有效模型名"}, 400)
                return
            result = run_probe(g_args.codex_home, model.strip())
            self._json(result)
            return
        self._json({"error": "接口不存在"}, 404)


# ---------------------------------------------------------------- 入口

conn_inst = None
g_args = None


def conn():
    global conn_inst
    if conn_inst is None:
        conn_inst = db_connect(db_path())
        if g_state["demo"]:
            seed_demo(conn_inst)
    return conn_inst


def db_path():
    return os.path.join(APP_DIR, "demo.db" if g_state["demo"] else "state.db")


def scan_loop():
    """Use a separate WAL writer: slow filesystem scans do not hold the API lock."""
    global g_last_scan
    scanner = None
    while True:
        try:
            if scanner is None:
                scanner = db_connect(db_path())
            stats = scan_sessions(scanner, g_args.codex_home, g_args.max_age_days)
            g_last_scan = time.time()
            if stats.get("note"):
                log(str(stats["note"]))
        except Exception:
            if scanner is not None:
                scanner.rollback()
            log("日志扫描异常（20 秒后重试）:\n" + traceback.format_exc())
        time.sleep(20)


def main():
    global g_args, g_last_scan, g_catalog
    ap = argparse.ArgumentParser(description="codex-model-watch —— 本地监控 Codex 模型使用/额度/拒单，并探测模型偷换")
    ap.add_argument("--port", type=int, default=8787, help="本地网页端口（默认 8787）")
    ap.add_argument("--codex-home", default=os.path.join(HOME, ".codex"), help="Codex 主目录（默认 ~/.codex）")
    ap.add_argument("--max-age-days", type=int, default=30, help="只解析最近 N 天的会话日志，0=全部（默认 30）")
    ap.add_argument("--demo", action="store_true", help="使用内置演示数据（不读取真实日志）")
    ap.add_argument("--scan-only", action="store_true", help="只扫描解析并打印摘要，不启动网页")
    ap.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    g_args = ap.parse_args()
    g_state["demo"] = g_args.demo

    conn_ = conn()
    if g_args.scan_only:
        with g_lock:
            if not g_args.demo:
                scan_sessions(conn_, g_args.codex_home, g_args.max_age_days)
        top = conn_.execute("""SELECT served, COUNT(*) n FROM turns GROUP BY served
                               ORDER BY n DESC LIMIT 5""").fetchall()
        for r in top:
            print("  %-24s %d 轮" % (r[0], r[1]))
        return

    server = ThreadingHTTPServer(("127.0.0.1", g_args.port), Handler)
    g_catalog = ModelCatalog(g_args.codex_home, APP_DIR, demo=g_args.demo)
    threading.Thread(target=g_catalog.loop, daemon=True).start()
    if not g_args.demo:
        threading.Thread(target=scan_loop, daemon=True).start()
        threading.Thread(target=auto_probe_loop, daemon=True).start()
    n_turn = conn_.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    n_probe = conn_.execute("SELECT COUNT(*) FROM probes").fetchone()[0]
    log("数据库累计 %d 轮会话、%d 次探针；后台扫描已启动" % (n_turn, n_probe))
    url = "http://127.0.0.1:%d" % g_args.port
    log("面板地址: %s  （PID %d，Ctrl+C 退出）" % (url, os.getpid()))
    if not g_args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    # 把"被谁杀的"写进日志：之前进程无声消失、日志为空，死因无从查起
    def on_signal(signum, _frame):
        name = signal.Signals(signum).name
        if signum == signal.SIGHUP:
            log("收到 SIGHUP（终端关闭/会话结束），忽略并继续运行")
            return
        log("收到 %s，退出" % name)
        raise SystemExit(128 + signum)

    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, on_signal)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出")
    except SystemExit:
        raise
    except BaseException:
        log("服务主循环异常退出:\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
