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
    return unix_request(socket_path, "GET", path, timeout=timeout)


def unix_request(socket_path, method, path, payload=None, timeout=30):
    """socket_path: a unix socket, or tcp://host:port (e.g. a docker-socket-proxy)."""
    if socket_path.startswith(("tcp://", "http://")):
        conn = http.client.HTTPConnection(socket_path.split("://", 1)[1].rstrip("/"), timeout=timeout)
    else:
        conn = _UnixConnection(socket_path, timeout)
    try:
        body = json.dumps(payload).encode() if payload is not None else None
        conn.request(method, path, body=body, headers=dict({"Host": "docker"}, **(
            {"Content-Type": "application/json"} if body is not None else {})))
        r = conn.getresponse()
        body = r.read().decode("utf-8", "replace")
        if r.status >= 400:
            raise HTTPError(r.status, body, "unix:" + path)
        return json.loads(body) if body else None
    finally:
        conn.close()
