"""Issues reported by people (or their agents, through MCP): what no log shows, such as slowness, odd behaviour,
wrong data. A report is an issue with source "user"; every further report on it is an occurrence, and the people
reporting are its "users".

aitomaton cannot see a report's cause directly, so it investigates: what else is going on with that service,
its architecture and the swarm, at the time of the report. The investigation is stored on the issue, shown to
the deciders, and returned to the reporter.
"""
from __future__ import annotations

import time

KINDS = ("slowness", "errors", "outage", "data", "security", "other")
SEVERITY_LEVEL = {"low": "info", "medium": "warning", "high": "error", "critical": "fatal"}
MAX_TEXT = 4000


def investigate(engine, issue, now=None):
    """Everything aitomaton knows that could explain or confirm a report."""
    now = now or time.time()
    name, arch = issue.get("service"), issue.get("architecture")
    app = engine.swarm.get(name)
    a = engine.analysis

    def near(f):
        if f.get("key") == issue["key"]:
            return False
        return f.get("service") == name if app else bool(arch) and f.get("architecture") == arch

    related = []
    for key, f in a["issues"].items():
        if near(f) and (f["events_1h"] or f["burst"]["burst"] or f.get("infra")):
            i = engine.store.issue(key) or {}
            if i.get("status") == "resolved":
                continue
            related.append({"key": key, "source": i.get("source"), "title": i.get("title"), "level": i.get("level"),
                            "events_1h": f["events_1h"], "burst": f["burst"]["burst"],
                            "in_app_frames": f.get("in_app_frames", 0)})
    related.sort(key=lambda r: (-r["burst"], -r["events_1h"]))
    targets = {name, arch} | {r["key"] for r in related}
    findings = [{"kind": f["kind"], "scope": f["scope"], "target": f["target"]}
                for f in engine.store.findings(active_only=True, limit=200) if f["target"] in targets]
    cluster = [{"key": i["key"], "title": i["title"]} for i in engine.store.q(
        "SELECT key, title FROM issues WHERE source='swarm' AND status!='resolved' AND service=?",
        (engine.cfg.infra_name,))]
    out = {"at": now, "service_known": bool(app), "related": related[:10], "findings": findings[:10],
           "cluster": cluster[:5],
           "emergencies": [dict(e, issue=k) for k, e in engine.emergencies.items()
                           if (engine.store.issue(k) or {}).get("service") == name][:3]}
    if app:
        out["replicas"] = app.get("replicas")
        out["failed_tasks_1h"] = app.get("failed_tasks_1h")
        out["last_task_error"] = app.get("last_task_error")
        if app.get("updated_at") and now - app["updated_at"] <= 6 * 3600:
            out["deployed_minutes_ago"] = int((now - app["updated_at"]) // 60)
        try:
            out["metrics"] = engine.prom.context(app)
        except Exception as e:      # metrics are a bonus, never a reason to lose the report
            out["metrics_error"] = str(e)
    out["code_hint"] = max([r["in_app_frames"] for r in related if r["source"] == "sentry"] or [0])
    out["summary"] = summarize(out, name)
    out["corroborated"] = bool(related or findings or cluster or out.get("emergencies") or
                               out.get("deployed_minutes_ago") is not None or out.get("failed_tasks_1h") or
                               ((out.get("metrics") or {}).get("memory_limit_ratio") or 0) > 0.9 or
                               short(out.get("replicas")))
    return out


def check_rate(engine, now=None):
    """MCP_REPORTS_PER_HOUR caps reports and additions from everyone together (MCP and Telegram)."""
    now = now or time.time()
    n = engine.store.q("SELECT COALESCE(SUM(count),0) n FROM occurrences WHERE key LIKE 'user:%' AND bucket>=?",
                       (int((now - 3600) // 60),))[0]["n"]
    if n >= engine.cfg.mcp_reports_per_hour:
        raise ValueError("too many reports in the last hour (MCP_REPORTS_PER_HOUR=%d): try again later"
                         % engine.cfg.mcp_reports_per_hour)


def short(reps):
    return bool(reps and reps.get("desired") and (reps.get("running") or 0) < reps["desired"])


def summarize(inv, name):
    """Plain sentences, for the reporter, the deciders and the brief."""
    s = []
    if not inv["service_known"]:
        s.append("No swarm service named %r: the report is tied to its architecture, or to nothing." % name)
    if short(inv.get("replicas")):
        s.append("%s runs %s of %s replicas." % (name, inv["replicas"]["running"], inv["replicas"]["desired"]))
    if inv.get("failed_tasks_1h"):
        s.append("%d tasks failed in the last hour (%s)." % (inv["failed_tasks_1h"], inv.get("last_task_error")))
    if inv.get("deployed_minutes_ago") is not None:
        s.append("The service was updated %d minutes ago." % inv["deployed_minutes_ago"])
    for e in inv.get("emergencies") or []:
        s.append("Emergency in progress: %s." % e["reason"])
    for r in inv["related"][:5]:
        s.append("Related %s issue%s: %s (%d events in the last hour)." % (
            r["source"], " bursting" if r["burst"] else "", r["title"], r["events_1h"]))
    for c in inv["cluster"]:
        s.append("Swarm: %s." % c["title"])
    m = inv.get("metrics") or {}
    if m.get("memory_limit_ratio") is not None:
        s.append("Memory at %.0f%% of its limit." % (m["memory_limit_ratio"] * 100))
    if m.get("cpu_cores") is not None:
        s.append("CPU %.2f cores." % m["cpu_cores"])
    if not s:
        s.append("Nothing else looks wrong with it right now: no errors, failing tasks, recent deploys or alerts.")
    return s
