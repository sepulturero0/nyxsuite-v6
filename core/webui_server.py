"""Local web server for the Nyx Suite bridge dashboard.

Serves:
  * the static ``webui/`` SPA (offline; ``sys._MEIPASS``-aware for frozen builds)
  * ``GET  /bridge/status``                 — aggregate Nyx + Nyxify snapshot
  * ``GET  /bridge/events``                 — Server-Sent Events real-time stream
  * ``POST /bridge/<product>/<action>``     — proxy to a controller action handler
  * ``POST /bridge/<action>``               — bridge-level action (check_update, rollback)

Real-time model: a single background *store-watcher* thread polls each
controller's ``status_snapshot()`` every ``watch_interval`` seconds, diffs the
per-profile rows (by profile_id / row_key), and broadcasts only the changed
rows + counts + bot-state to every connected SSE client. The browser opens one
``EventSource`` and never polls. The runners are untouched — the watcher only
*reads* the same store snapshots the product APIs expose.

Pure stdlib :mod:`http.server`, no third-party deps, fully offline. CORS is open
so the extension/browser tab can call it.
"""

import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core.process_utils import ROOT_DIR
from core.local_http import apply_cors, extract_token
from core.timing_diag import elapsed, log_timing, now

# How long a computed aggregate /bridge/status is reused. Several clients /
# tabs may poll the same status at once; short-circuiting them avoids building
# the full snapshot (both products, up to 500 rows) repeatedly within a window.
# The SSE watcher still diffs at watch_interval, so real-time updates are
# unaffected — this only dedupes identical, closely-spaced status reads.
STATUS_CACHE_TTL_SECONDS = 0.4

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
}

_FALLBACK_INDEX = """<!doctype html><html><head><meta charset="utf-8"><title>Nyx Suite</title></head>
<body style="font:14px system-ui;margin:24px;background:#0f1626;color:#e6ecf5">
<h2>Nyx Suite bridge is running</h2>
<p>The dashboard assets (webui/) were not found next to the app. Live status:</p>
<pre id="o">loading...</pre>
<script>async function t(){try{const r=await fetch('/bridge/status');document.getElementById('o').textContent=JSON.stringify(await r.json(),null,2)}catch(e){document.getElementById('o').textContent=e}}t();setInterval(t,1500)</script>
</body></html>"""


def _webui_root():
    """Locate the webui/ asset dir in dev (ROOT_DIR) or frozen (_MEIPASS)."""
    base = getattr(sys, "_MEIPASS", None)
    candidates = ([Path(base)] if base else []) + [ROOT_DIR, ROOT_DIR / "_internal"]
    for cand in candidates:
        try:
            if (cand / "webui" / "index.html").exists():
                return cand / "webui"
        except Exception:
            continue
    return None


def _row_key(product, row):
    if product == "nyx":
        return str(row.get("profile_id") or "")
    return str(row.get("row_key") or row.get("profile_id") or "")


def _row_signature(row):
    return (
        row.get("status"),
        row.get("last_step"),
        row.get("error"),
        row.get("username"),
        row.get("ip_address") or row.get("ip"),
        row.get("proxy_address") or row.get("proxy"),
        row.get("adspower_id") or row.get("adspower_profile_id") or row.get("profile_id"),
        row.get("adspower_open"),
        row.get("adspower_open_profile_id"),
    )


class WebDashboardServer:
    def __init__(self, controllers=None, host="127.0.0.1", port=8870,
                 bridge_actions=None, bridge_settings_provider=None,
                 version="", watch_interval=0.5, token=""):
        self.controllers = controllers or {}
        self.token = str(token or "")
        self.host = host
        self.port = int(port)
        self.bridge_actions = bridge_actions or {}
        self.bridge_settings_provider = bridge_settings_provider
        self.version = str(version or "")
        self.watch_interval = float(watch_interval)
        self._server = None
        self._thread = None
        self._clients = []                 # list[queue.Queue]
        self._clients_lock = threading.Lock()
        self._watch_thread = None
        self._stop_watch = threading.Event()
        self._last_rows = {}               # product -> {key: signature}
        self._last_meta = {}               # product -> (bot_state, counts_json)
        self._webui_root = _webui_root()
        self._status_cache = {"at": 0.0, "value": None}

    # ------------------------------------------------------------- status
    def status(self) -> dict:
        cache = self._status_cache
        t = time.monotonic()
        if cache["value"] and (t - float(cache.get("at") or 0.0)) < STATUS_CACHE_TTL_SECONDS:
            return cache["value"]
        start = now()
        products = {}
        for name, controller in self.controllers.items():
            try:
                products[name] = controller.status_snapshot()
            except Exception as exc:
                products[name] = {"error": str(exc)}
        log_timing("dashboard.status", start, "bridge")
        settings = {}
        if callable(self.bridge_settings_provider):
            try:
                settings = self.bridge_settings_provider() or {}
            except Exception:
                settings = {}
        value = {"ok": True, "products": products, "bridge": {"version": self.version, "settings": settings}}
        self._status_cache.update({"at": time.monotonic(), "value": value})
        return value

    def _inject_token(self, body: bytes) -> bytes:
        """Inject window.__NYX_TOKEN__ into index.html so the same-origin SPA has it."""
        if not self.token:
            return body
        try:
            text = body.decode("utf-8")
            script = "<script>window.__NYX_TOKEN__=%s;</script>" % json.dumps(self.token)
            if "</head>" in text:
                text = text.replace("</head>", script + "</head>", 1)
            else:
                text = script + text
            return text.encode("utf-8")
        except Exception:
            return body

    # ------------------------------------------------------------- SSE plumbing
    def _add_client(self, q):
        with self._clients_lock:
            self._clients.append(q)

    def _remove_client(self, q):
        with self._clients_lock:
            if q in self._clients:
                self._clients.remove(q)

    def _broadcast(self, event: dict):
        payload = json.dumps(event)
        with self._clients_lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass  # slow client; drop this delta (it re-syncs from the next snapshot)

    def _watch_loop(self):
        primed = False  # first pass populates last-state silently; the SSE snapshot already has it
        while not self._stop_watch.is_set():
            try:
                for name, controller in self.controllers.items():
                    with elapsed("watch.snapshot", name):
                        snap = controller.status_snapshot()
                    rows = snap.get("rows", []) or []
                    current = {}
                    for row in rows:
                        key = _row_key(name, row)
                        if key:
                            current[key] = row
                    last = self._last_rows.get(name, {})
                    changed = [row for key, row in current.items()
                               if key not in last or last[key] != _row_signature(row)]
                    removed = [key for key in last if key not in current]
                    self._last_rows[name] = {k: _row_signature(r) for k, r in current.items()}

                    meta = (
                        snap.get("bot", {}).get("state"),
                        json.dumps(snap.get("counts", {}), sort_keys=True),
                        json.dumps(snap.get("adspower_health") or {}, sort_keys=True),
                        json.dumps(snap.get("adspower_live") or {}, sort_keys=True),
                    )
                    meta_changed = self._last_meta.get(name) != meta
                    self._last_meta[name] = meta

                    if primed and (changed or removed or meta_changed):
                        self._broadcast({
                            "type": "update",
                            "product": name,
                            "counts": snap.get("counts"),
                            "bot": snap.get("bot"),
                            "adspower_usage": snap.get("adspower_usage"),
                            "adspower_health": snap.get("adspower_health"),
                            "adspower_live": snap.get("adspower_live"),
                            "rows": changed,
                            "removed": removed,
                        })
                primed = True
            except Exception:
                pass
            self._stop_watch.wait(self.watch_interval)

    def _sse_snapshot_bytes(self) -> bytes:
        event = {"type": "snapshot", "status": self.status()}
        return ("data: %s\n\n" % json.dumps(event)).encode("utf-8")

    # ------------------------------------------------------------- lifecycle
    def start(self):
        if self._server is not None:
            return
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _cors(self):
                apply_cors(self)

            def _authorized(self, payload=None, query_token=""):
                if not outer.token:
                    return True
                return extract_token(self, payload, query_token) == outer.token

            def _json(self, code, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)

            def _read_json(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    return json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    return {}

            def _serve_static(self, path):
                root = outer._webui_root
                rel = path.lstrip("/") or "index.html"
                if root is None:
                    if rel in ("", "index.html"):
                        body = outer._inject_token(_FALLBACK_INDEX.encode("utf-8"))
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Cache-Control", "no-store")
                        self._cors()
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    self._json(404, {"ok": False, "error": "Not found"})
                    return
                target = (root / rel).resolve()
                try:
                    target.relative_to(root.resolve())  # block path traversal
                except Exception:
                    self._json(403, {"ok": False, "error": "Forbidden"})
                    return
                if not target.is_file():
                    self._json(404, {"ok": False, "error": "Not found"})
                    return
                body = target.read_bytes()
                if target.name == "index.html":
                    body = outer._inject_token(body)
                self.send_response(200)
                self.send_header("Content-Type", _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self._json(200, {"ok": True})

            def do_HEAD(self):
                # Liveness probe used by the extension popup — 200, no body.
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()

            def do_GET(self):
                parsed = urlparse(self.path)
                route = parsed.path
                query_token = (parse_qs(parsed.query).get("token") or [""])[0]
                if route == "/bridge/status":
                    if not self._authorized(query_token=query_token):
                        self._json(401, {"ok": False, "error": "Unauthorized."})
                        return
                    self._json(200, outer.status())
                    return
                if route == "/bridge/events":
                    if not self._authorized(query_token=query_token):
                        self._json(401, {"ok": False, "error": "Unauthorized."})
                        return
                    self._serve_events()
                    return
                self._serve_static(route)

            def _serve_events(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self._cors()
                self.end_headers()
                client = queue.Queue(maxsize=200)
                outer._add_client(client)
                try:
                    self.wfile.write(outer._sse_snapshot_bytes())
                    self.wfile.flush()
                    while True:
                        try:
                            msg = client.get(timeout=15)
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        self.wfile.write(("data: %s\n\n" % msg).encode("utf-8"))
                        self.wfile.flush()
                except Exception:
                    pass
                finally:
                    outer._remove_client(client)

            def do_POST(self):
                payload = self._read_json()
                parts = [p for p in urlparse(self.path).path.split("/") if p]
                if not parts or parts[0] != "bridge":
                    self._json(404, {"ok": False, "error": "Not found"})
                    return
                if not self._authorized(payload):
                    self._json(401, {"ok": False, "error": "Unauthorized."})
                    return
                if len(parts) == 3:
                    _, product, action = parts
                    controller = outer.controllers.get(product)
                    if controller is None:
                        self._json(404, {"ok": False, "error": f"Unknown product '{product}'."})
                        return
                    handler = controller.action_handlers().get(action)
                    if handler is None:
                        self._json(404, {"ok": False, "error": f"Unknown {product} action '{action}'."})
                        return
                    self._dispatch(handler, payload)
                    return
                if len(parts) == 2:
                    handler = outer.bridge_actions.get(parts[1])
                    if handler is None:
                        self._json(404, {"ok": False, "error": f"Unknown bridge action '{parts[1]}'."})
                        return
                    self._dispatch(handler, payload)
                    return
                self._json(404, {"ok": False, "error": "Not found"})

            def _dispatch(self, handler, payload):
                dispatch_start = now()
                try:
                    result = handler(payload or {})
                except Exception as exc:
                    log_timing("action.dispatch", dispatch_start)
                    self._json(500, {"ok": False, "error": str(exc) or "Action failed."})
                    return
                log_timing("action.dispatch", dispatch_start)
                if not isinstance(result, dict):
                    result = {"ok": True, "message": "Action completed."}
                result.setdefault("ok", True)
                self._json(200, result)

            def log_message(self, *args):
                return

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._stop_watch.clear()
        self._watch_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._watch_thread.start()

    def stop(self):
        self._stop_watch.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self._thread = None
