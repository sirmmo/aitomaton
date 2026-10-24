"""HTTP: webhooks in, status JSON and the static page out."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .mcp import MCP, caller
from .sources.sentry import verify_signature

log = logging.getLogger("aitomaton.http")
MAX_BODY = 5 * 1024 * 1024


def make_handler(engine):
    cfg = engine.cfg
    static = os.path.abspath(cfg.static_dir)
    mcp = MCP(engine)

    class Handler(BaseHTTPRequestHandler):
        server_version = "aitomaton"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else (
                json.dumps(body, indent=1, default=str) if not isinstance(body, str) else body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authorized_view(self, qs):
            if not cfg.status_token:
                return True
            auth = self.headers.get("Authorization", "")
            token = (qs.get("token") or [""])[0] or (auth[7:] if auth.startswith("Bearer ") else "")
            return hmac.compare_digest(token, cfg.status_token)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            if u.path == "/mcp":     # JSON responses only: no server-initiated stream
                return self._send(405 if cfg.mcp_tokens else 404, {"error": "POST JSON-RPC to /mcp"})
            if u.path == "/health":
                return self._send(200, {"status": "ok", "last_tick": engine.last_tick})
            if not self._authorized_view(qs):
                return self._send(401, {"error": "token required"})
            if u.path in ("/status.json", "/api/status"):
                return self._send(200, engine.status())
            path = "index.html" if u.path in ("", "/") else u.path.lstrip("/")
            full = os.path.abspath(os.path.join(static, path))
            if not full.startswith(static + os.sep) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            with open(full, "rb") as fh:
                self._send(200, fh.read(), mimetypes.guess_type(full)[0] or "application/octet-stream")

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(413, {"error": "too large"})
            body = self.rfile.read(n)
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                return self._send(400, {"error": "invalid json"})
            if u.path == "/mcp":
                if not cfg.mcp_tokens:
                    return self._send(404, {"error": "the MCP server is off (AITOMATON_MCP_TOKEN)"})
                who = caller(cfg, self.headers.get("Authorization"))
                if not who:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Bearer realm="aitomaton"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                out = mcp.handle(payload, who)
                return self._send(200, out) if out is not None else self._send(202, b"", "text/plain")
            try:
                if u.path == "/hooks/sentry":
                    if not verify_signature(cfg.sentry_hook_secret, body, self.headers.get("Sentry-Hook-Signature")):
                        return self._send(401, {"error": "bad signature"})
                    key = engine.sentry_webhook(dict(self.headers.items()), payload)
                    return self._send(202, {"accepted": bool(key), "issue": key})
                if u.path == "/hooks/alertmanager":
                    if cfg.alertmanager_secret and not hmac.compare_digest(
                            (qs.get("token") or [""])[0], cfg.alertmanager_secret):
                        return self._send(401, {"error": "bad token"})
                    return self._send(202, {"accepted": engine.alertmanager_webhook(payload)})
                if u.path == "/hooks/stairway":
                    sig = self.headers.get("X-Stairway-Signature", "")
                    want = "sha256=" + hmac.new((cfg.callback_secret or "").encode(), body,
                                                hashlib.sha256).hexdigest()
                    if not cfg.callback_secret or not hmac.compare_digest(sig, want):
                        return self._send(401, {"error": "bad signature"})
                    return self._send(200, {"matched": engine.stairway_callback(payload)})
            except Exception as e:
                log.exception("webhook %s", u.path)
                return self._send(500, {"error": str(e)})
            return self._send(404, {"error": "not found"})

    return Handler


def serve(engine):
    httpd = ThreadingHTTPServer((engine.cfg.host, engine.cfg.port), make_handler(engine))
    log.info("listening on %s:%s", engine.cfg.host, engine.cfg.port)
    httpd.serve_forever()
