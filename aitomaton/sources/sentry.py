"""Sentry and urgentry input: webhooks (integration platform and legacy plugin) and API polling.

Both speak the same `/api/0/` surface, so one client serves either. Polling turns the issue
`count` into occurrences (deltas between polls) and seeds history from the issue `stats`.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from datetime import datetime

from ..http import HTTPError, request


def parse_time(s):
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = re.sub(r"\.(\d{1,6})\d*", lambda m: "." + m.group(1).ljust(6, "0"), str(s).replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def issue_key(project, issue_id):
    return "sentry:%s:%s" % (project or "unknown", issue_id)


def verify_signature(secret, body, signature):
    """Sentry integration webhooks: Sentry-Hook-Signature = hex HMAC-SHA256(client secret, body)."""
    if not secret:
        return True
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(signature) and hmac.compare_digest(expected, signature)


def summarize_event(ev):
    """What a fixer needs from an event: the exception, in-app frames, tags, a few breadcrumbs."""
    if not ev:
        return {}
    excs = []
    entries = {e.get("type"): e.get("data") for e in ev.get("entries") or [] if isinstance(e, dict)}
    values = ((ev.get("exception") or entries.get("exception") or {}).get("values")) or []
    for v in values[-3:]:
        frames = ((v.get("stacktrace") or {}).get("frames")) or []
        keep = [f for f in frames if f.get("in_app") or f.get("inApp")] or frames
        excs.append({
            "type": v.get("type"), "value": (v.get("value") or "")[:1000],
            "frames": [{"file": f.get("filename") or f.get("abs_path") or f.get("absPath"),
                        "function": f.get("function"), "line": f.get("lineno") or f.get("lineNo"),
                        "context": (f.get("context_line") or "").strip()[:200] or None,
                        "in_app": bool(f.get("in_app") or f.get("inApp"))} for f in keep[-15:]],
        })
    tags = ev.get("tags") or []
    if isinstance(tags, list):
        tags = {(t[0] if isinstance(t, list) else t.get("key")): (t[1] if isinstance(t, list) else t.get("value"))
                for t in tags if t}
    crumbs = (ev.get("breadcrumbs") or entries.get("breadcrumbs") or {})
    crumbs = crumbs.get("values", []) if isinstance(crumbs, dict) else []
    release = ev.get("release")
    if isinstance(release, dict):
        release = release.get("version")
    return {
        "event_id": ev.get("event_id") or ev.get("eventID") or ev.get("id"),
        "message": (ev.get("message") or ev.get("title") or "")[:500],
        "exceptions": excs,
        "tags": {k: tags[k] for k in list(tags)[:30]},
        "release": release or tags.get("release"),
        "environment": ev.get("environment") or tags.get("environment"),
        "platform": ev.get("platform"),
        "breadcrumbs": [{"category": c.get("category"), "message": (c.get("message") or "")[:200],
                         "level": c.get("level")} for c in crumbs[-8:]],
    }


def _project_from_url(url):
    m = re.search(r"/projects/[^/]+/([^/]+)/", url or "")
    return m.group(1) if m else None


def parse_webhook(headers, payload):
    """Normalize a Sentry/urgentry webhook into {"issue": {...}, "event": summary|None, "occurred": ts|None}.
    Returns None for resources we do not act on (installation, metric alerts ...)."""
    resource = (headers.get("Sentry-Hook-Resource") or headers.get("sentry-hook-resource") or "").lower()
    data = payload.get("data") or {}
    action = payload.get("action")
    if resource == "issue" or "issue" in data and not resource:
        i = data.get("issue") or {}
        proj = (i.get("project") or {}).get("slug") if isinstance(i.get("project"), dict) else i.get("project")
        return {"issue": {
            "project": proj, "id": str(i.get("id")), "title": i.get("title"), "culprit": i.get("culprit"),
            "level": i.get("level"), "status": i.get("status"),
            "first_seen": parse_time(i.get("firstSeen")), "last_seen": parse_time(i.get("lastSeen")),
            "total": int(i.get("count") or 0), "users": int(i.get("userCount") or 0),
            "url": i.get("permalink") or i.get("web_url"),
            "regression": action == "unresolved" or i.get("substatus") == "regressed",
            "action": action,
        }, "event": None, "occurred": parse_time(i.get("lastSeen")) if action == "created" else None}
    if resource in ("event_alert", "error") or data.get("event") or data.get("error"):
        ev = data.get("event") or data.get("error") or {}
        proj = ev.get("project_slug") or _project_from_url(ev.get("url")) or _project_from_url(ev.get("web_url")) \
            or str(ev.get("project") or "")
        iid = ev.get("issue_id") or ev.get("groupID") or re.sub(r".*/issues/(\d+).*", r"\1", ev.get("issue_url") or "")
        ts = parse_time(ev.get("datetime") or ev.get("timestamp")) or time.time()
        return {"issue": {
            "project": proj, "id": str(iid or ev.get("event_id")), "title": ev.get("title"),
            "culprit": ev.get("culprit"), "level": ev.get("level"), "status": "unresolved",
            "first_seen": ts, "last_seen": ts, "url": ev.get("web_url") or ev.get("issue_url"),
            "rule": (data.get("triggered_rule") or None), "action": action,
        }, "event": summarize_event(ev), "occurred": ts}
    if "project" in payload and ("id" in payload or "event" in payload):   # legacy webhooks plugin
        ev = payload.get("event") or {}
        ts = parse_time(ev.get("timestamp") or ev.get("received")) or time.time()
        return {"issue": {
            "project": payload.get("project_slug") or payload.get("project"), "id": str(payload.get("id")),
            "title": payload.get("message") or ev.get("title"), "culprit": payload.get("culprit"),
            "level": payload.get("level"), "status": "unresolved", "first_seen": ts, "last_seen": ts,
            "url": payload.get("url"), "action": "triggered",
        }, "event": summarize_event(ev), "occurred": ts}
    return None


class SentryClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.error = None
        self.last_poll = 0

    @property
    def enabled(self):
        return bool(self.cfg.sentry_url and self.cfg.sentry_token and self.cfg.sentry_org)

    def _get(self, path):
        return request("GET", self.cfg.sentry_url + path, headers={"Authorization": "Bearer " + self.cfg.sentry_token})

    def projects(self):
        if self.cfg.sentry_projects:
            return self.cfg.sentry_projects
        return [p["slug"] for p in self._get("/api/0/organizations/%s/projects/" % self.cfg.sentry_org) or []]

    def issues(self, project, period="24h"):
        return self._get("/api/0/projects/%s/%s/issues/?statsPeriod=%s&query=is:unresolved&limit=100"
                         % (self.cfg.sentry_org, project, period)) or []

    def latest_event(self, issue_id):
        for path in ("/api/0/organizations/%s/issues/%s/events/latest/" % (self.cfg.sentry_org, issue_id),
                     "/api/0/issues/%s/events/latest/" % issue_id):
            try:
                return self._get(path)
            except HTTPError as e:
                if e.status not in (404, 405):
                    raise
        return None

    def poll(self, store, ingest):
        """Fetch unresolved issues, turn count deltas into occurrences. `ingest(project, issue_fields)`
        stores the issue (and resolves the service); returns the number of issues seen."""
        if not self.enabled:
            return 0
        now, seen = time.time(), 0
        try:
            for project in self.projects():
                history = {}
                first_run = not store.get("sentry:polled:" + project)
                if first_run:
                    history = {str(i["id"]): i for i in self.issues(project, "14d")}
                for i in self.issues(project, "24h"):
                    seen += 1
                    iid, total = str(i["id"]), int(i.get("count") or 0)
                    key = issue_key(project, iid)
                    fields = {"id": iid, "title": i.get("title"), "culprit": i.get("culprit"),
                              "level": i.get("level"), "status": i.get("status"),
                              "first_seen": parse_time(i.get("firstSeen")), "last_seen": parse_time(i.get("lastSeen")),
                              "total": total, "users": int(i.get("userCount") or 0),
                              "url": i.get("permalink"),
                              "regression": i.get("substatus") == "regressed" or bool(i.get("isRegression"))}
                    prev = store.get("sentry:count:" + key)
                    ingest(project, fields)
                    at = min(now, fields["last_seen"] or now)
                    if prev is None:
                        self._seed(store, key, i, history.get(iid), now, at)
                    elif total > prev:
                        store.add_occurrence(key, at, total - prev)
                    store.put("sentry:count:" + key, total)
                store.put("sentry:polled:" + project, now)
            self.error, self.last_poll = None, now
        except Exception as e:
            self.error = str(e)
        return seen

    @staticmethod
    def _seed(store, key, issue, daily, now, last_seen):
        """History for an issue seen for the first time: daily buckets (older than a day), hourly buckets
        (older than this hour), and what is left of the last hour at lastSeen."""
        hourly = ((issue.get("stats") or {}).get("24h")) or []
        seeded_hourly = 0
        if daily:
            for ts, n in ((daily.get("stats") or {}).get("14d")) or []:
                if ts < now - 86400:
                    store.add_occurrence(key, ts, n)
        for ts, n in hourly:
            if ts < now - 3600:
                store.add_occurrence(key, ts, n)
                seeded_hourly += n
        rest = sum(n for _, n in hourly) - seeded_hourly
        if rest > 0:
            store.add_occurrence(key, last_seen, rest)
        if not hourly and not daily:
            store.add_occurrence(key, last_seen, 1)
