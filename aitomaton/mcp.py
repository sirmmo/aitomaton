"""MCP server (streamable HTTP, JSON responses) at POST /mcp.

People and their agents can ask aitomaton what is going on, and tell it what no log shows: "checkout is slow",
"the map shows yesterday's data". Reports become issues (source "user") that aitomaton investigates against
everything else it sees, and decides on like any other issue.

  AITOMATON_MCP_TOKEN   comma-separated Bearer tokens; "name:token" names the caller, and the name becomes the
                        reporter of its reports. Without it the endpoint is off.
"""
from __future__ import annotations

import difflib
import hmac
import json
import time

from . import __version__
from .engine import iso
from .reports import KINDS, SEVERITY_LEVEL, check_rate

PROTOCOL = "2025-06-18"
INSTRUCTIONS = (
    "aitomaton watches a Docker Swarm: application errors (Sentry/urgentry), alerts, and the swarm itself. It analyses "
    "them, decides what to do, and fixes, reports or acts. Use status / service_status / list_issues / get_issue to see "
    "what is going on. Use report_issue for anything wrong that no log shows (slowness, odd behaviour, wrong data, an "
    "outage seen from outside): aitomaton investigates it against everything else it sees and decides on it. Use "
    "add_observation when the same thing happens again or you learn more, and resolve_report when it is over.")


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
TOOLS = [
    {"name": "status", "title": "Overview",
     "description": "What is going on now: emergencies, works in progress, active findings, swarm health, sources.",
     "inputSchema": _obj({}), "annotations": {"readOnlyHint": True}},
    {"name": "service_status", "title": "One service",
     "description": "A service's configuration (repo, policies), replicas, failing tasks, metrics, open issues, "
                    "findings and emergencies. Full name (stack_service) or short name.",
     "inputSchema": _obj({"service": S}, ["service"]), "annotations": {"readOnlyHint": True}},
    {"name": "list_issues", "title": "Issues",
     "description": "Issues, most recent first. Filter by service, architecture, source (sentry, prometheus, swarm, "
                    "user) or words in the title.",
     "inputSchema": _obj({"service": S, "architecture": S, "source": S, "query": S,
                          "open_only": dict(B, default=True), "limit": dict(I, default=20)}),
     "annotations": {"readOnlyHint": True}},
    {"name": "get_issue", "title": "One issue",
     "description": "An issue with its analysis, investigation (for reports), decisions with each tier's reasoning, "
                    "and works (fixes, reports, direct actions).",
     "inputSchema": _obj({"key": S}, ["key"]), "annotations": {"readOnlyHint": True}},
    {"name": "report_issue", "title": "Report something",
     "description": "Signal a problem that no log shows: slowness, strange behaviour, wrong data, an outage seen "
                    "from outside. aitomaton investigates it against errors, alerts, deploys, metrics and the swarm, "
                    "returns what it found, and decides on it. A report of the same kind on the same service within "
                    "the hour is merged into the open one (more reporters make it weigh more).",
     "inputSchema": _obj({
         "title": dict(S, description="one line, e.g. 'checkout takes 20 s'"),
         "description": dict(S, description="what happens, since when, for whom, how to see it"),
         "service": dict(S, description="the swarm service, if known"),
         "architecture": dict(S, description="the stack / architecture, when the service is not known"),
         "kind": dict(S, enum=list(KINDS)), "severity": dict(S, enum=list(SEVERITY_LEVEL)),
         "reporter": dict(S, description="who reports it (defaults to the token's name)"),
         "merge": dict(B, default=True)}, ["title", "description"])},
    {"name": "add_observation", "title": "Add to a report",
     "description": "It happened again, got worse, or there is more to say about a reported issue. Can raise the "
                    "severity, and reopens a resolved report.",
     "inputSchema": _obj({"key": S, "text": S, "severity": dict(S, enum=list(SEVERITY_LEVEL)), "reporter": S},
                         ["key", "text"])},
    {"name": "resolve_report", "title": "Close a report",
     "description": "The reported problem is over (say why).",
     "inputSchema": _obj({"key": S, "note": S, "reporter": S}, ["key"]), "annotations": {"idempotentHint": True}},
]


def caller(cfg, auth_header):
    """The caller's name for a valid Bearer token, else None."""
    token = auth_header[7:].strip() if (auth_header or "").startswith("Bearer ") else ""
    if not token:
        return None
    for entry in (cfg.mcp_tokens or "").split(","):
        name, _, tok = entry.strip().rpartition(":") if ":" in entry else ("", "", entry.strip())
        if tok and hmac.compare_digest(token, tok):
            return name or "mcp"
    return None


class MCP:
    def __init__(self, engine):
        self.engine = engine
        self.wait = engine.cfg.mcp_wait     # how long a report waits for the loop's decision before answering

    # ---- JSON-RPC
    def handle(self, msg, who):
        """One message or a batch -> the response (None for notifications)."""
        if isinstance(msg, list):
            out = [r for r in (self.handle(m, who) for m in msg) if r is not None]
            return out or None
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            return None                     # notifications/initialized and friends
        try:
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "aitomaton", "version": __version__},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self.call(params.get("name"), params.get("arguments") or {}, who)
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown method %s" % method}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def call(self, name, args, who):
        fn = getattr(self, "t_" + (name or ""), None)
        if not fn or name not in {t["name"] for t in TOOLS}:
            return self._result({"error": "unknown tool %s" % name}, True)
        try:
            return self._result(fn(who=who, **args))
        except (KeyError, TypeError, ValueError) as e:
            return self._result({"error": str(e).strip("'\"")}, True)

    @staticmethod
    def _result(data, error=False):
        return {"content": [{"type": "text", "text": json.dumps(data, indent=1, default=str)}],
                "structuredContent": data, "isError": error}

    # ---- tools
    def t_status(self, who):
        st = self.engine.status()
        inf = st["infrastructure"]
        return {
            "generated_at": st["generated_at"], "last_tick": st["last_tick"], "tick_error": st["tick_error"],
            "summary": st["summary"], "emergencies": st["emergencies"],
            "works_in_progress": [{k: w[k] for k in ("id", "issue", "kind", "status", "ref", "url", "updated")}
                                  for w in st["works"] if w["status"] in ("queued", "dispatched", "running", "waiting")],
            "active_findings": [{k: f[k] for k in ("kind", "scope", "target", "severity", "first_at")}
                                for f in st["findings"] if f["active"]][:25],
            "swarm": {"managers": inf.get("managers"), "nodes_not_ready": [n["hostname"] for n in inf.get("nodes") or []
                                                                         if n.get("state") != "ready"],
                      "conditions": [c["title"] for c in inf.get("conditions") or []][:25],
                      "advisories": [a["text"] for a in inf.get("advisories") or []], "error": inf.get("error")},
            "sources": st["sources"], "dispatch": st["config"]["dispatch"],
        }

    def t_service_status(self, service, who):
        e = self.engine
        s = e.swarm.get(service)
        if not s:
            raise KeyError("no service %s" % service)
        issues = [self._issue_row(i) for i in e.store.q(
            "SELECT * FROM issues WHERE service=? AND status!='resolved' ORDER BY last_seen DESC LIMIT 30",
            (s["name"],))]
        targets = {s["name"]} | {i["key"] for i in issues}
        return {
            "service": {k: s.get(k) for k in ("name", "stack", "architecture", "enabled", "repo", "ref", "path",
                                              "policy", "policy_emergency", "sentry_projects", "image", "depends",
                                              "replicas", "failed_tasks_1h", "last_task_error")},
            "infra_outlet": {k: v for k, v in (e.infra_outlet(s)).items() if k in ("repo", "path", "policy",
                                                                                  "policy_emergency", "outlet")},
            "git": e.credential(s)["source"] if s.get("enabled") else None,
            "updated_at": iso(s.get("updated_at")),
            "metrics": e.prom.context(s) if e.prom.enabled else None,
            "issues": issues,
            "findings": [{k: f[k] for k in ("kind", "scope", "target", "severity")}
                         for f in e.store.findings(active_only=True) if f["target"] in targets],
            "emergencies": [dict(v, issue=k, since=iso(v["since"])) for k, v in e.emergencies.items()
                            if k in targets],
        }

    def t_list_issues(self, who, service=None, architecture=None, source=None, query=None, open_only=True, limit=20):
        sql, args = "SELECT * FROM issues WHERE last_seen>=?", [time.time() - self.engine.cfg.long_days * 86400]
        if service:
            s = self.engine.swarm.get(service)
            sql += " AND service=?"
            args.append(s["name"] if s else service)
        for col, val in (("architecture", architecture), ("source", source)):
            if val:
                sql += " AND %s=?" % col
                args.append(val)
        if query:
            sql += " AND title LIKE ?"
            args.append("%" + query + "%")
        if open_only:
            sql += " AND status!='resolved'"
        sql += " ORDER BY last_seen DESC LIMIT ?"
        args.append(max(1, min(int(limit), 200)))
        return {"issues": [self._issue_row(i) for i in self.engine.store.q(sql, tuple(args))]}

    def t_get_issue(self, key, who):
        e = self.engine
        i = e.store.issue(key)
        if not i:
            raise KeyError("no issue %s" % key)
        f = e.analysis["issues"].get(key) or {}
        decisions = e.store.q("SELECT * FROM decisions WHERE key=? ORDER BY id DESC LIMIT 5", (key,))
        works = e.store.q("SELECT * FROM works WHERE key=? ORDER BY id DESC LIMIT 10", (key,))
        det = i["detail"] or {}
        return dict(self._issue_row(i), culprit=i["culprit"], url=i["url"], total=i["total"], users=i["users"],
                    first_seen=iso(i["first_seen"]),
                    detail={k: v for k, v in det.items() if k not in ("breadcrumbs",)},
                    analysis={k: f.get(k) for k in ("new", "regression", "events_1h", "events_24h", "burst",
                                                    "long_term", "cascade", "cascade_root", "service_burst")} if f
                    else None,
                    emergency=e.emergencies.get(key),
                    target={k: v for k, v in (e.target(i) or {}).items() if k in ("name", "repo", "path", "policy",
                                                                                   "kind")},
                    decisions=[{"at": iso(d["at"]), "action": d["action"], "applied": d["applied"],
                                "policy": d["policy"], "decider": d["decider"], "confidence": d["confidence"],
                                "rationale": d["rationale"],
                                "trail": [{k: t.get(k) for k in ("decider", "action", "confidence", "rationale")}
                                          for t in (d["features"] or {}).get("trail") or []]} for d in decisions],
                    works=[{"id": w["id"], "kind": w["kind"], "status": w["status"], "ref": w["ref"],
                            "url": w["url"], "result_url": w["result_url"], "summary": w["summary"],
                            "updated": iso(w["updated"]),
                            "last_note": ((w["log"] or [{}])[-1]).get("note")} for w in works])

    def _rate(self):
        check_rate(self.engine)

    def t_report_issue(self, title, description, who, service=None, architecture=None, kind="other",
                       severity="medium", reporter=None, merge=True):
        if not (title or "").strip() or not (description or "").strip():
            raise ValueError("title and description are required")
        self._rate()
        if service and not self.engine.swarm.get(service) and not architecture:
            known = [n for s in self.engine.swarm.services.values() for n in (s["name"], s["short"])]
            near = sorted({self.engine.swarm.get(n)["name"] for n in difflib.get_close_matches(service, known, 5, 0.6)}
                          | {n for n in known if service.lower() in n.lower()})[:10]
            if near:
                raise ValueError("no service %s; did you mean one of: %s" % (service, ", ".join(near)))
        i = self.engine.report_issue(title, description, service, architecture, kind, severity, reporter or who,
                                     merge)
        return self._report_answer(i)

    def t_add_observation(self, key, text, who, severity=None, reporter=None):
        self._rate()
        return self._report_answer(self.engine.add_observation(key, text, reporter or who, severity))

    def t_resolve_report(self, key, who, note=None, reporter=None):
        i = self.engine.resolve_report(key, note, reporter or who)
        return {"key": i["key"], "status": i["status"]}

    # ---- helpers
    def _report_answer(self, i):
        """The investigation now, and the decision if the loop takes it within a few seconds."""
        e, deadline = self.engine, time.time() + self.wait
        while True:
            d = e.store.last_decision(i["key"])
            if (d and d["at"] >= i["last_seen"] - 1) or time.time() >= deadline:
                break
            time.sleep(0.5)
        if d and d["at"] < i["last_seen"] - 1:
            d = None
        merged = i.get("merged")
        i = dict(e.store.issue(i["key"]), merged=merged)      # deciding refreshes the investigation
        inv = (i["detail"] or {}).get("investigation") or {}
        return {
            "key": i["key"], "merged": bool(i.get("merged")), "service": i["service"], "status": i["status"],
            "reports": i["total"], "reporters": i["users"],
            "investigation": {"summary": inv.get("summary"), "corroborated": inv.get("corroborated"),
                              "related": inv.get("related"), "deployed_minutes_ago": inv.get("deployed_minutes_ago")},
            "decision": {"action": d["action"], "applied": d["applied"], "policy": d["policy"],
                         "decider": d["decider"], "rationale": d["rationale"]} if d else
            "pending: aitomaton decides at its next tick; ask get_issue later",
        }

    @staticmethod
    def _issue_row(i):
        return {"key": i["key"], "source": i["source"], "service": i["service"], "architecture": i["architecture"],
                "title": i["title"], "level": i["level"], "status": i["status"], "last_seen": iso(i["last_seen"]),
                "events": i["total"]}
