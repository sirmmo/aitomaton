"""Tiny JSON-over-HTTP helpers (stdlib only), including HTTP over the Docker unix socket."""
from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.request


class HTTPError(Exception):
    def __init__(self, status, body, url=""):
        super().__init__("HTTP %s from %s: %s" % (status, url, (body or "")[:300]))
        self.status, self.body = status, body


def request(method, url, payload=None, headers=None, timeout=30):
    data = None
    hdrs = {"Accept": "application/json", "User-Agent": "aitomaton/0.1"}
    if payload is not None:
        data = json.dumps(payload).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise HTTPError(e.code, e.read().decode("utf-8", "replace"), url) from None
    return json.loads(body) if body.strip() else None


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def unix_get(socket_path, path, timeout=30):
    conn = _UnixConnection(socket_path, timeout)
    try:
        conn.request("GET", path, headers={"Host": "docker"})
        r = conn.getresponse()
        body = r.read().decode("utf-8", "replace")
        if r.status >= 400:
            raise HTTPError(r.status, body, "unix:" + path)
        return json.loads(body) if body else None
    finally:
        conn.close()
