"""The swarm itself as a source: nodes, managers, replicas and task errors become conditions.

A condition is an infrastructure issue (source "swarm"), keyed swarm:<subject>:<kind>, that fires while the
state lasts. Cluster conditions (a node down, a lost quorum) belong to the infrastructure; service conditions
(OOM kills, images that do not pull, tasks no node accepts, too few replicas) belong to the service, but
their fix is in the deployment, not in the service's code, so they go to the infrastructure outlet.

  node_down            a node is not ready                                  cluster
  manager_unreachable  a manager is not reachable                           cluster
  quorum_lost          fewer reachable managers than the raft quorum        cluster
  oom                  tasks killed for memory (OOMKilled, exit 137)        service
  image_pull           the image cannot be pulled                           service
  unschedulable        no node satisfies the constraints or resources       service
  port_conflict        a published port is taken                            service
  crash_loop           tasks keep exiting with an error                     service
  under_replicated     fewer running tasks than replicas, past the grace    service
  update_paused        a rolling update paused or rolled back               service

Advisories (one manager, an even number of managers, engine version skew, drained nodes) are shown, not acted on.
"""
from __future__ import annotations

import re
from collections import defaultdict

TASK_ERRORS = [   # first match wins
    ("oom", re.compile(r"oomkilled|out of memory|exit(?:ed)? (?:code )?\(?137\)?|non-zero exit \(137\)", re.I)),
    ("image_pull", re.compile(r"no such image|pull access denied|manifest unknown|not found: manifest|"
                              r"error pulling|failed to resolve reference|unauthorized", re.I)),
    ("unschedulable", re.compile(r"no suitable node|insufficient resources|scheduling constraints", re.I)),
    ("port_conflict", re.compile(r"address already in use|port is already allocated|port already in use", re.I)),
]
LEVEL = {"node_down": "error", "manager_unreachable": "fatal", "quorum_lost": "fatal", "oom": "error",
         "image_pull": "error", "unschedulable": "error", "port_conflict": "error", "crash_loop": "error",
         "under_replicated": "warning", "update_paused": "error"}


def classify(err):
    for kind, rx in TASK_ERRORS:
        if rx.search(err or ""):
            return kind
    return "crash_loop"


def _host(n):
    return ((n.get("Description") or {}).get("Hostname")) or n.get("ID", "?")[:12]


def check(nodes, services, tasks, now, ts, grace=300, task_failures=3):
    """Raw Docker API lists -> {"conditions": [...], "advisories": [...], "nodes": [...], "managers": {...}}.
    ts parses Docker timestamps. Each condition: key, kind, subject, service (None for cluster), level,
    title, culprit, detail, failures (task ids, for counting occurrences)."""
    out, adv, rows = [], [], []
    down = {}
    managers = [n for n in nodes if (n.get("Spec") or {}).get("Role") == "manager"]
    reachable = sum(1 for n in managers if (n.get("ManagerStatus") or {}).get("Reachability") == "reachable")
    quorum = len(managers) // 2 + 1
    for n in nodes:
        spec, st, ms = n.get("Spec") or {}, n.get("Status") or {}, n.get("ManagerStatus") or {}
        host, role = _host(n), spec.get("Role")
        rows.append({"id": n.get("ID"), "hostname": host, "role": role, "state": st.get("State"),
                     "availability": spec.get("Availability"), "addr": st.get("Addr"),
                     "leader": bool(ms.get("Leader")), "reachability": ms.get("Reachability"),
                     "engine": ((n.get("Description") or {}).get("Engine") or {}).get("EngineVersion"),
                     "cpus": ((n.get("Description") or {}).get("Resources") or {}).get("NanoCPUs", 0) / 1e9,
                     "memory_gb": round(((n.get("Description") or {}).get("Resources") or {}).get("MemoryBytes", 0)
                                        / 2 ** 30, 1)})
        if st.get("State") not in (None, "ready"):
            down[n.get("ID")] = host
            out.append(_cond("node_down", host, None, "fatal" if role == "manager" else "error",
                             "Node %s (%s) is %s" % (host, role, st.get("State")), st.get("Addr"),
                             {"node": host, "role": role, "state": st.get("State"), "message": st.get("Message")},
                             since=ts(n.get("UpdatedAt")) or None))
        if role == "manager" and ms and ms.get("Reachability") != "reachable":
            out.append(_cond("manager_unreachable", host, None, "fatal", "Manager %s is %s" % (
                host, ms.get("Reachability")), ms.get("Addr"), {"node": host, "reachability": ms.get("Reachability")}))
        if spec.get("Availability") in ("drain", "pause"):
            adv.append({"kind": "node_%s" % spec["Availability"], "target": host,
                        "text": "node %s is set to %s" % (host, spec["Availability"])})
    if managers and reachable < quorum:
        out.append(_cond("quorum_lost", "managers", None, "fatal", "Raft quorum lost: %d of %d managers reachable, "
                         "%d needed" % (reachable, len(managers), quorum), None,
                         {"managers": len(managers), "reachable": reachable, "quorum": quorum}))
    if len(managers) == 1 and len(nodes) > 1:
        adv.append({"kind": "single_manager", "target": "managers",
                    "text": "one manager: if it goes, the swarm cannot be changed or heal itself (3 tolerate one loss)"})
    elif managers and len(managers) % 2 == 0:
        adv.append({"kind": "even_managers", "target": "managers", "text": "%d managers tolerate no more losses "
                    "than %d would" % (len(managers), len(managers) - 1)})
    majors = sorted({r["engine"].split(".")[0] for r in rows if r["engine"]}, key=lambda v: int(v) if v.isdigit() else 0)
    if len(majors) > 1:
        adv.append({"kind": "engine_skew", "target": "nodes", "text": "Docker engines differ across nodes: " + ", ".join(
            "%s %s" % (r["hostname"], r["engine"]) for r in rows)})

    by_service = defaultdict(list)
    for t in tasks:
        by_service[t.get("ServiceID")].append(t)
    for s in services:
        spec = s.get("Spec") or {}
        name = spec.get("Name", s.get("ID"))
        ts_ = by_service.get(s.get("ID"), [])
        running = [t for t in ts_ if (t.get("Status") or {}).get("State") == "running"
                   and t.get("DesiredState") == "running"]
        failed = [t for t in ts_ if (t.get("Status") or {}).get("State") in ("failed", "rejected")
                  and ts((t.get("Status") or {}).get("Timestamp")) >= now - 3600]
        desired = ((spec.get("Mode") or {}).get("Replicated") or {}).get("Replicas")
        s["_replicas"] = (len(running), desired)
        kinds = defaultdict(list)
        for t in failed:
            st = t.get("Status") or {}
            kinds[classify(st.get("Err") or st.get("Message"))].append(t)
        for kind, group in kinds.items():
            if len(group) < task_failures:
                continue
            last = max(group, key=lambda t: ts((t.get("Status") or {}).get("Timestamp")))
            err = (last.get("Status") or {}).get("Err") or (last.get("Status") or {}).get("Message")
            nodes_hit = sorted({down.get(t.get("NodeID")) or t.get("NodeID", "")[:12] for t in group if t.get("NodeID")})
            out.append(_cond(kind, name, name, LEVEL[kind], "%s: %d tasks failed in the last hour (%s)" % (
                name, len(group), kind.replace("_", " ")), err, {
                "service": name, "failed_1h": len(group), "error": err, "nodes": nodes_hit,
                "image": ((spec.get("TaskTemplate") or {}).get("ContainerSpec") or {}).get("Image", "").split("@")[0],
                "resources": (spec.get("TaskTemplate") or {}).get("Resources"),
                "placement": (spec.get("TaskTemplate") or {}).get("Placement")},
                failures=[t.get("ID") for t in group],
                since=min(ts((t.get("Status") or {}).get("Timestamp")) for t in group)))
        if desired and len(running) < desired and now - ts(s.get("UpdatedAt")) >= grace:
            missing_on = sorted({down[t["NodeID"]] for t in ts_ if t.get("NodeID") in down})
            d = {"service": name, "running": len(running), "desired": desired}
            if missing_on:
                d["cause"] = "node %s is down" % ", ".join(missing_on)
            # short since its last task changed state; an update that started no task does not count
            last = max([ts((t.get("Status") or {}).get("Timestamp")) for t in ts_] or [ts(s.get("UpdatedAt"))])
            out.append(_cond("under_replicated", name, name, "error" if not running else LEVEL["under_replicated"],
                             "%s runs %d of %d replicas" % (name, len(running), desired), d.get("cause"), d,
                             since=last or None))
        us = s.get("UpdateStatus") or {}
        stalled = us.get("State") in ("paused", "rollback_paused", "rollback_completed")
        if stalled and now - ts(us.get("StartedAt")) >= 86400 and not (desired and len(running) < desired):
            # an old pause on a service that runs fine: the new spec never landed, but nothing is down
            adv.append({"kind": "update_paused", "target": name, "text": "%s: update %s since %s" % (
                name, us["State"].replace("_", " "), (us.get("StartedAt") or "?")[:10])})
        elif stalled:
            out.append(_cond("update_paused", name, name, LEVEL["update_paused"], "%s: update %s" % (
                name, us["State"].replace("_", " ")), us.get("Message"), {"service": name, "state": us["State"],
                                                                          "message": us.get("Message")}))
    if down:    # services short of replicas because a node died are the node's problem, not theirs
        for c in out:
            if c["kind"] in ("under_replicated", "unschedulable") and c["service"]:
                c["detail"].setdefault("cause", "node %s is down" % ", ".join(sorted(down.values())))
    return {"conditions": out, "advisories": adv, "nodes": rows,
            "managers": {"total": len(managers), "reachable": reachable, "quorum": quorum if managers else 0}}


def _cond(kind, subject, service, level, title, culprit, detail, failures=None, since=None):
    """since: when the state probably began, from Docker's timestamps (None when unknown)."""
    return {"key": "swarm:%s:%s" % (subject, kind), "kind": kind, "subject": subject, "service": service,
            "level": level, "title": title, "culprit": culprit, "detail": dict(detail, kind=kind),
            "failures": failures or [], "since": since}
