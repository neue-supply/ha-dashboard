#!/usr/bin/env python3
"""
Neue Dashboard config server.

Runs on 127.0.0.1:8100 inside the addon. nginx proxies /api/dashboards* and
(legacy) /api/config* to this server.

Dashboards API (multi-dashboard):
  GET    /api/dashboards                  → index.json
  PATCH  /api/dashboards                  → merge-update index.json (body is partial)
  GET    /api/dashboards/:id              → dashboards/:id.json
  PUT    /api/dashboards/:id              → write :id.json; touch index
  POST   /api/dashboards                  → body { id, name, icon }; create empty dashboard
  DELETE /api/dashboards/:id              → delete file + remove from index
  GET    /api/dashboards/:id/stream       → SSE per-dashboard change events

Legacy single-config API (kept until app cutover):
  GET  /api/config                        → /data/dashboard-config.json
  POST /api/config                        → writes it
  GET  /api/config/stream                 → SSE for any legacy config change

Storage:
  /data/dashboards/index.json
  /data/dashboards/:id.json
  /data/dashboard-config.json  (legacy — never deleted by this server)

Auth handled upstream by HA ingress — binds 127.0.0.1 only.
"""

import hashlib
import json
import os
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DASHBOARDS_DIR = Path("/data/dashboards")
INDEX_PATH = DASHBOARDS_DIR / "index.json"
LEGACY_CONFIG_PATH = Path("/data/dashboard-config.json")

MAX_BODY_BYTES = 1 * 1024 * 1024
HEARTBEAT_SECONDS = 25
ID_RE = re.compile(r"^[a-z0-9-]+$")

_file_lock = threading.Lock()
_index_lock = threading.Lock()

# Per-dashboard SSE subscribers: dashboardId -> list of queues.
_sub_lock = threading.Lock()
_subs_by_dashboard: "dict[str, list[queue.Queue[str]]]" = {}
# Legacy single-config subscribers.
_legacy_subs: "list[queue.Queue[str]]" = []


def _ensure_dashboards_dir() -> None:
    DASHBOARDS_DIR.mkdir(parents=True, exist_ok=True)
    if not INDEX_PATH.exists():
        _write_json_atomic(INDEX_PATH, {"dashboards": [], "activeDashboardFallback": ""})


def _read_bytes(path: Path) -> bytes:
    with _file_lock:
        if path.exists():
            return path.read_bytes()
    return b""


def _write_bytes_atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _file_lock:
        tmp.write_bytes(body)
        os.replace(tmp, path)


def _write_json_atomic(path: Path, obj: object) -> None:
    _write_bytes_atomic(path, json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def _read_json(path: Path) -> object:
    raw = _read_bytes(path)
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def _etag(body: bytes) -> str:
    return '"' + hashlib.sha1(body).hexdigest() + '"'


def _valid_id(dashboard_id: str) -> bool:
    return bool(ID_RE.match(dashboard_id))


def _broadcast_dashboard(dashboard_id: str) -> None:
    payload = f"event: update\ndata: {int(time.time() * 1000)}\n\n"
    with _sub_lock:
        subs = list(_subs_by_dashboard.get(dashboard_id, ()))
    for q in subs:
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass


def _broadcast_legacy() -> None:
    payload = f"event: update\ndata: {int(time.time() * 1000)}\n\n"
    with _sub_lock:
        subs = list(_legacy_subs)
    for q in subs:
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass


def _register_dashboard_sub(dashboard_id: str) -> "queue.Queue[str]":
    q: "queue.Queue[str]" = queue.Queue(maxsize=16)
    with _sub_lock:
        _subs_by_dashboard.setdefault(dashboard_id, []).append(q)
    return q


def _unregister_dashboard_sub(dashboard_id: str, q: "queue.Queue[str]") -> None:
    with _sub_lock:
        lst = _subs_by_dashboard.get(dashboard_id)
        if lst is None:
            return
        try:
            lst.remove(q)
        except ValueError:
            pass
        if not lst:
            _subs_by_dashboard.pop(dashboard_id, None)


def _register_legacy_sub() -> "queue.Queue[str]":
    q: "queue.Queue[str]" = queue.Queue(maxsize=16)
    with _sub_lock:
        _legacy_subs.append(q)
    return q


def _unregister_legacy_sub(q: "queue.Queue[str]") -> None:
    with _sub_lock:
        try:
            _legacy_subs.remove(q)
        except ValueError:
            pass


def _index_upsert(dashboard_id: str, name: str, icon: str) -> None:
    with _index_lock:
        idx = _read_json(INDEX_PATH) or {"dashboards": [], "activeDashboardFallback": ""}
        entries = idx.get("dashboards", [])
        for e in entries:
            if e.get("id") == dashboard_id:
                e["name"] = name
                e["icon"] = icon
                break
        else:
            entries.append({"id": dashboard_id, "name": name, "icon": icon})
        idx["dashboards"] = entries
        if not idx.get("activeDashboardFallback"):
            idx["activeDashboardFallback"] = dashboard_id
        _write_json_atomic(INDEX_PATH, idx)


def _index_remove(dashboard_id: str) -> None:
    with _index_lock:
        idx = _read_json(INDEX_PATH) or {"dashboards": [], "activeDashboardFallback": ""}
        entries = [e for e in idx.get("dashboards", []) if e.get("id") != dashboard_id]
        idx["dashboards"] = entries
        if idx.get("activeDashboardFallback") == dashboard_id:
            idx["activeDashboardFallback"] = entries[0]["id"] if entries else ""
        _write_json_atomic(INDEX_PATH, idx)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    # Dispatching ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/dashboards":
            return self._get_index()
        m = re.match(r"^/api/dashboards/([a-z0-9-]+)$", path)
        if m:
            return self._get_dashboard(m.group(1))
        m = re.match(r"^/api/dashboards/([a-z0-9-]+)/stream$", path)
        if m:
            return self._stream_dashboard(m.group(1))
        if path == "/api/config":
            return self._legacy_get_config()
        if path == "/api/config/stream":
            return self._legacy_stream()
        self.send_error(404, "Not Found")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/dashboards":
            return self._post_dashboard()
        if path == "/api/config":
            return self._legacy_post_config()
        self.send_error(404, "Not Found")

    def do_PUT(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        m = re.match(r"^/api/dashboards/([a-z0-9-]+)$", path)
        if m:
            return self._put_dashboard(m.group(1))
        self.send_error(404, "Not Found")

    def do_PATCH(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/dashboards":
            return self._patch_index()
        self.send_error(404, "Not Found")

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        m = re.match(r"^/api/dashboards/([a-z0-9-]+)$", path)
        if m:
            return self._delete_dashboard(m.group(1))
        self.send_error(404, "Not Found")

    # Body helpers --------------------------------------------------------
    def _read_body(self) -> "bytes | None":
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            self.send_error(400, "Empty body")
            return None
        if length > MAX_BODY_BYTES:
            self.send_error(413, "Payload too large")
            return None
        return self.rfile.read(length)

    def _send_json(self, status: int, body: bytes) -> None:
        etag = _etag(body)
        if self.headers.get("If-None-Match") == etag and status == 200:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # Multi-dashboard handlers --------------------------------------------
    def _get_index(self) -> None:
        _ensure_dashboards_dir()
        body = _read_bytes(INDEX_PATH) or b'{"dashboards":[],"activeDashboardFallback":""}'
        self._send_json(200, body)

    def _patch_index(self) -> None:
        body = self._read_body()
        if body is None:
            return
        try:
            patch = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return
        if not isinstance(patch, dict):
            self.send_error(400, "Body must be an object")
            return
        # Whitelist and type-check. Only the two keys the spec lists are accepted.
        allowed_keys = {"dashboards", "activeDashboardFallback"}
        unknown = set(patch.keys()) - allowed_keys
        if unknown:
            self.send_error(400, f"Unknown keys: {sorted(unknown)}")
            return
        if "activeDashboardFallback" in patch and not isinstance(
            patch["activeDashboardFallback"], str
        ):
            self.send_error(400, "activeDashboardFallback must be a string")
            return
        if "dashboards" in patch:
            entries = patch["dashboards"]
            if not isinstance(entries, list):
                self.send_error(400, "dashboards must be an array")
                return
            for e in entries:
                if not (
                    isinstance(e, dict)
                    and isinstance(e.get("id"), str)
                    and isinstance(e.get("name"), str)
                    and isinstance(e.get("icon"), str)
                ):
                    self.send_error(400, "dashboards entries must be {id,name,icon} strings")
                    return
        with _index_lock:
            _ensure_dashboards_dir()
            current = _read_json(INDEX_PATH) or {"dashboards": [], "activeDashboardFallback": ""}
            if not isinstance(current, dict):
                current = {"dashboards": [], "activeDashboardFallback": ""}
            current.update(patch)
            _write_json_atomic(INDEX_PATH, current)
        self.send_response(204)
        self.end_headers()

    def _get_dashboard(self, dashboard_id: str) -> None:
        _ensure_dashboards_dir()
        if not _valid_id(dashboard_id):
            self.send_error(400, "Invalid id")
            return
        path = DASHBOARDS_DIR / f"{dashboard_id}.json"
        body = _read_bytes(path)
        if not body:
            self.send_error(404, "Dashboard not found")
            return
        self._send_json(200, body)

    def _put_dashboard(self, dashboard_id: str) -> None:
        if not _valid_id(dashboard_id):
            self.send_error(400, "Invalid id")
            return
        body = self._read_body()
        if body is None:
            return
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return
        if not isinstance(obj, dict):
            self.send_error(400, "Body must be an object")
            return
        required = ("pages", "pageOrder", "pageCards", "layout")
        for k in required:
            if k not in obj:
                self.send_error(400, f"Missing required key: {k}")
                return
        obj["id"] = dashboard_id
        name = obj.get("name") or dashboard_id
        icon = obj.get("icon") or ""
        _ensure_dashboards_dir()
        _write_bytes_atomic(
            DASHBOARDS_DIR / f"{dashboard_id}.json",
            json.dumps(obj, separators=(",", ":")).encode("utf-8"),
        )
        _index_upsert(dashboard_id, name, icon)
        _broadcast_dashboard(dashboard_id)
        self.send_response(204)
        self.end_headers()

    def _post_dashboard(self) -> None:
        body = self._read_body()
        if body is None:
            return
        try:
            meta = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return
        dashboard_id = str(meta.get("id", "")).strip()
        name = str(meta.get("name", dashboard_id)).strip()
        icon = str(meta.get("icon", "")).strip()
        if not _valid_id(dashboard_id):
            self.send_error(400, "Invalid id")
            return
        _ensure_dashboards_dir()
        path = DASHBOARDS_DIR / f"{dashboard_id}.json"
        if path.exists():
            self.send_error(409, "Dashboard already exists")
            return
        empty = {
            "id": dashboard_id,
            "name": name,
            "icon": icon,
            "pages": [],
            "pageOrder": [],
            "pageCards": {},
            "deviceIcons": {},
            "defaultPage": "",
            "layout": {},
        }
        _write_json_atomic(path, empty)
        _index_upsert(dashboard_id, name, icon)
        _broadcast_dashboard(dashboard_id)
        self._send_json(201, json.dumps(empty, separators=(",", ":")).encode("utf-8"))

    def _delete_dashboard(self, dashboard_id: str) -> None:
        if not _valid_id(dashboard_id):
            self.send_error(400, "Invalid id")
            return
        path = DASHBOARDS_DIR / f"{dashboard_id}.json"
        with _file_lock:
            if path.exists():
                path.unlink()
        _index_remove(dashboard_id)
        _broadcast_dashboard(dashboard_id)
        self.send_response(204)
        self.end_headers()

    def _stream_dashboard(self, dashboard_id: str) -> None:
        if not _valid_id(dashboard_id):
            self.send_error(400, "Invalid id")
            return
        self._stream_loop(_register_dashboard_sub(dashboard_id),
                          lambda q: _unregister_dashboard_sub(dashboard_id, q))

    # Legacy handlers (kept transitionally) -------------------------------
    def _legacy_get_config(self) -> None:
        body = _read_bytes(LEGACY_CONFIG_PATH) or b"null"
        self._send_json(200, body)

    def _legacy_post_config(self) -> None:
        body = self._read_body()
        if body is None:
            return
        try:
            json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return
        _write_bytes_atomic(LEGACY_CONFIG_PATH, body)
        _broadcast_legacy()
        self.send_response(204)
        self.end_headers()

    def _legacy_stream(self) -> None:
        self._stream_loop(_register_legacy_sub(), _unregister_legacy_sub)

    # SSE helper ---------------------------------------------------------
    def _stream_loop(self, q: "queue.Queue[str]", unregister) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            last_hb = time.monotonic()
            while True:
                timeout = max(0.5, HEARTBEAT_SECONDS - (time.monotonic() - last_hb))
                try:
                    payload = q.get(timeout=timeout)
                    self.wfile.write(payload.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_hb = time.monotonic()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            unregister(q)


def main() -> None:
    _ensure_dashboards_dir()
    server = ThreadingHTTPServer(("127.0.0.1", 8100), Handler)
    print("[config-server] listening on 127.0.0.1:8100", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
