"""The three horizons — single error, bursts, long-term patterns — at issue, service and architecture scope.

Everything works on minute buckets from the store, so webhook events, poll deltas and Alertmanager
alerts are analysed the same way.
"""
from __future__ import annotations

from collections import defaultdict

LEVEL_WEIGHT = {"fatal": 1.0, "critical": 1.0, "error": 0.7, "warning": 0.4, "info": 0.15, "debug": 0.05}


def _burst(buckets, now, cfg):
    """buckets: {minute: count}. Recent window vs the rate expected from the baseline window."""
    w0 = int((now - cfg.burst_window) // 60)
    b0 = int((now - cfg.baseline_window) // 60)
    window = sum(n for m, n in buckets.items() if m >= w0)
    baseline = sum(n for m, n in buckets.items() if b0 <= m < w0)
    expected = baseline * cfg.burst_window / max(cfg.baseline_window - cfg.burst_window, 1)
    ratio = window / max(expected, 0.2)
    onset = min((m for m, n in buckets.items() if m >= w0 and n > 0), default=None)
    return {"window": window, "expected": round(expected, 2), "ratio": round(ratio, 1),
            "burst": window >= cfg.burst_min and ratio >= cfg.burst_ratio,
            "onset": onset * 60 if onset is not None else None}


def _long_term(buckets, now, days):
    """Daily counts over the horizon, trend (least-squares slope / mean, per day) and persistence."""
    day0 = int(now // 86400) - days + 1
    daily = [0] * days
    for m, n in buckets.items():
        d = m * 60 // 86400 - day0
        if 0 <= d < days:
            daily[int(d)] += n
    active = sum(1 for n in daily if n)
    # the trend uses complete days only: today is partial, and a burst today is a burst, not a trend
    full = daily[:-1]
    n = len(full)
    mean = sum(full) / n if n else 0
    trend = 0.0
    if mean > 0 and sum(1 for x in full if x) >= 2:
        xm = (n - 1) / 2
        num = sum((i - xm) * (v - mean) for i, v in enumerate(full))
        den = sum((i - xm) ** 2 for i in range(n))
        trend = num / den / mean
    return {"daily": daily, "days_active": active, "trend": round(trend, 3),
            "rising": trend > 0.1 and sum(1 for x in full if x) >= 3, "chronic": active >= max(3, days // 2)}


def analyze(store, swarm, cfg, now):
    since = now - cfg.long_days * 86400
    issues = [i for i in store.issues(since) if i["status"] != "resolved" or i["last_seen"] >= now - 3600]
    series = store.series([i["key"] for i in issues], since)
    per_issue, findings = {}, []
    svc_buckets = defaultdict(lambda: defaultdict(int))
    h1, h24 = int((now - 3600) // 60), int((now - 86400) // 60)

    for i in issues:
        buckets = defaultdict(int)
        for m, n in series.get(i["key"], []):
            buckets[m] += n
            if i["service"] and i["source"] != "swarm":     # conditions are states, not error streams
                svc_buckets[i["service"]][m] += n
        b = _burst(buckets, now, cfg)
        lt = _long_term(buckets, now, cfg.long_days)
        detail = i["detail"] or {}
        frames = [f for e in detail.get("exceptions") or [] for f in e.get("frames") or []]
        f = {
            "key": i["key"], "service": i["service"], "architecture": i["architecture"],
            "level": i["level"] or "error", "level_weight": LEVEL_WEIGHT.get((i["level"] or "error").lower(), 0.5),
            "new": bool(i["first_seen"] and i["first_seen"] >= now - cfg.new_window),
            "regression": bool((i["flags"] or {}).get("regression")),
            "events_1h": sum(n for m, n in buckets.items() if m >= h1),
            "events_24h": sum(n for m, n in buckets.items() if m >= h24),
            "users": i["users"] or 0, "total": i["total"] or 0,
            "has_stacktrace": bool(frames), "in_app_frames": sum(1 for x in frames if x.get("in_app")),
            "source": i["source"],
            "infra": {"kind": detail.get("kind"), "cause": detail.get("cause")} if i["source"] == "swarm" else None,
            "burst": b, "long_term": lt,
        }
        per_issue[i["key"]] = f
        target = i["key"]
        if i["source"] == "swarm":
            findings.append({"kind": "infrastructure", "scope": "issue", "target": target,
                             "severity": f["level_weight"], "detail": {"title": i["title"], "service": i["service"],
                                                                       "condition": detail.get("kind")}})
        elif f["new"]:
            findings.append({"kind": "new", "scope": "issue", "target": target, "severity": f["level_weight"],
                             "detail": {"title": i["title"], "service": i["service"]}})
        if f["regression"]:
            findings.append({"kind": "regression", "scope": "issue", "target": target,
                             "severity": min(1, f["level_weight"] + 0.2),
                             "detail": {"title": i["title"], "service": i["service"]}})
        if b["burst"]:
            findings.append({"kind": "burst", "scope": "issue", "target": target,
                             "severity": min(1, 0.4 + b["ratio"] / 50),
                             "detail": dict(b, title=i["title"], service=i["service"])})
        if lt["rising"]:
            findings.append({"kind": "rising", "scope": "issue", "target": target,
                             "severity": min(1, 0.3 + lt["trend"]),
                             "detail": {"title": i["title"], "trend": lt["trend"], "daily": lt["daily"]}})
        elif lt["chronic"]:
            findings.append({"kind": "chronic", "scope": "issue", "target": target, "severity": 0.3,
                             "detail": {"title": i["title"], "days_active": lt["days_active"]}})

    # service scope: bursts across all of a service's issues, failing tasks
    per_service = {}
    for name in set(svc_buckets) | set(swarm.services):
        b = _burst(svc_buckets.get(name, {}), now, cfg)
        d = swarm.services.get(name) or {}
        per_service[name] = {"burst": b, "architecture": d.get("architecture"),
                             "failed_tasks_1h": d.get("failed_tasks_1h", 0),
                             "last_task_error": d.get("last_task_error")}
        if b["burst"]:
            findings.append({"kind": "burst", "scope": "service", "target": name,
                             "severity": min(1, 0.5 + b["ratio"] / 50), "detail": b})
        if d.get("failed_tasks_1h", 0) >= 3:
            findings.append({"kind": "task_failures", "scope": "service", "target": name, "severity": 0.6,
                             "detail": {"failed_tasks_1h": d["failed_tasks_1h"], "error": d.get("last_task_error")}})

    # architecture scope: services bursting together point at one cause
    by_arch = defaultdict(list)
    for name, s in per_service.items():
        arch = s["architecture"] or (swarm.services.get(name) or {}).get("architecture")
        if not arch:
            arch = next((i["architecture"] for i in issues if i["service"] == name and i["architecture"]), None)
        if arch and s["burst"]["onset"] is not None and s["burst"]["window"] >= cfg.burst_min:
            by_arch[arch].append((s["burst"]["onset"], name))
    cascades = {}
    for arch, hits in by_arch.items():
        hits.sort()
        group = [n for t, n in hits if t - hits[0][0] <= cfg.cascade_window]
        if len(group) < 2:
            continue
        def needs(a, b):        # does service a declare a dependency on service b?
            da, db = swarm.services.get(a) or {}, swarm.services.get(b) or {}
            return bool(set(da.get("depends", [])) & {b, db.get("short")})
        depended = [n for n in group if any(needs(o, n) for o in group if o != n)]
        # the most upstream one: depended on, and not itself depending on another failing member
        upstream = [n for n in depended if not any(needs(n, o) for o in group if o != n)]
        root = (upstream or depended or group)[0]
        cascades[arch] = {"services": group, "root": root, "onset": hits[0][0],
                          "reason": "depended on by the others" if depended else "earliest onset"}
        findings.append({"kind": "cascade", "scope": "architecture", "target": arch,
                         "severity": min(1, 0.5 + 0.1 * len(group)), "detail": cascades[arch]})
    for f in per_issue.values():
        c = cascades.get(f["architecture"] or (per_service.get(f["service"]) or {}).get("architecture"))
        f["cascade"] = bool(c and f["service"] in c["services"])
        f["cascade_root"] = bool(c and c["root"] == f["service"])
        s = per_service.get(f["service"]) or {}
        f["service_burst"] = bool((s.get("burst") or {}).get("burst"))
        f["failed_tasks_1h"] = s.get("failed_tasks_1h", 0)

    return {"issues": per_issue, "services": per_service, "cascades": cascades, "findings": findings}
