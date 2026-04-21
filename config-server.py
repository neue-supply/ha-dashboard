#!/usr/bin/env python3
"""
Neue Dashboard config sync server.

Runs on 127.0.0.1:8100 inside the addon. nginx proxies /api/config* here.
Stores the dashboard config at /data/dashboard-config.json so it persists
across addon restarts.

Endpoints:
  GET  /api/config         → current config JSON (or `null` if empty)
  POST /api/config         → validate + atomic write + broadcast SSE update
  GET  /api/config/stream  → Server-Sent Events; pushes `event: update` to
                             every connected client on each successful POST,
                             plus `: ping` heartbeats every 25s to keep
                             connections alive through proxies.

Auth is handled upstream by HA ingress — this server only binds to 127.0.0.1.
"""

import hashlib
import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG_PATH = Path("/data/dashboard-config.json")
MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB guard
HEARTBEAT_SECONDS = 25

_file_lock = threading.Lock()
_subscribers_lock = threading.Lock()
_subscribers: "list[queue.Queue[str]]" = []


def _read_config_bytes() -> bytes:
    """Return current config bytes, or the JSON literal `null` if absent."""
    with _file_lock:
        if CONFIG_PATH.exists():
            return CONFIG_PATH.read_bytes()
    return b"null"


def _write_config_bytes(body: bytes) -> None:
    """Atomic write: tmp file + os.replace."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with _file_lock:
        tmp.write_bytes(body)
        os.replace(tmp, CONFIG_PATH)


def _broadcast_update() -> None:
    """Enqueue an `update` event to every SSE subscriber."""
    payload = f"event: update\ndata: {int(time.time() * 1000)}\n\n"
    with _subscribers_lock:
        for q in list(_subscribers):
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass  # dropped — slow consumer


def _register_subscriber() -> "queue.Queue[str]":
    q: "queue.Queue[str]" = queue.Queue(maxsize=16)
    with _subscribers_lock:
        _subscribers.append(q)
    return q


def _unregister_subscriber(q: "queue.Queue[str]") -> None:
    with _subscribers_lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


class ConfigHandler(BaseHTTPRequestHandler):
    # Keep logs quiet; nginx already logs
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/config":
            self._handle_get_config()
        elif self.path == "/api/config/stream":
            self._handle_stream()
        else:
            self.send_error(404, "Not Found")

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/config":
            self.send_error(404, "Not Found")
            return
        self._handle_post_config()

    def _handle_get_config(self) -> None:
        body = _read_config_bytes()
        etag = '"' + hashlib.sha1(body).hexdigest() + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _handle_post_config(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            self.send_error(400, "Empty body")
            return
        if length > MAX_BODY_BYTES:
            self.send_error(413, "Payload too large")
            return
        body = self.rfile.read(length)
        try:
            json.loads(body)  # validate JSON
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return
        try:
            _write_config_bytes(body)
        except OSError as exc:
            self.send_error(500, f"Write failed: {exc}")
            return
        _broadcast_update()
        self.send_response(204)
        self.end_headers()

    def _handle_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q = _register_subscriber()
        # Initial retry hint so EventSource reconnects promptly
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            last_heartbeat = time.monotonic()
            while True:
                timeout = max(
                    0.5, HEARTBEAT_SECONDS - (time.monotonic() - last_heartbeat)
                )
                try:
                    payload = q.get(timeout=timeout)
                    self.wfile.write(payload.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_heartbeat = time.monotonic()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            _unregister_subscriber(q)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8100), ConfigHandler)
    print("[config-server] listening on 127.0.0.1:8100", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
