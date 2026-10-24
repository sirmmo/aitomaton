# status.json

`GET /status.json` (also `/api/status`) is the whole state for people and programs. The page at `/` only
renders it. With `AITOMATON_STATUS_TOKEN` set, pass `?token=` or `Authorization: Bearer`. Times are ISO 8601
UTC.

```jsonc
{
  "aitomaton": "0.1.0",
  "generated_at": "…", "started_at": "…", "last_tick": "…", "tick_error": null,
  "config": {                       // no secrets
    "decider": "chain", "chain": ["rules", "laya", "jev", "llm"], "dispatch": false, "default_policy": "report",
    "sources": {"sentry": true, "prometheus": false, …},
    "laya": true, "jev": {"model": "typesafe/jev-router", "configured": true},
    "llm": {"base_url": "…", "model": "…", "configured": true},
    "executor": {"stairway": true, "agent": "claude", "prepares_clone": true, …},
    "limits": {…}, "analysis": {…}
  },
  "sources": {
    "sentry": {"enabled": true, "last_poll": "…", "error": null},
    "prometheus": {"enabled": false, "error": null},
    "swarm": {"services": 103, "refreshed": "…", "error": null}
  },
  "summary": {
    "issues": 8, "active_findings": 17, "decisions_24h": 7, "in_progress": 1,
    "works": {"dry-run": 0, "queued": 0, "dispatched": 0, "running": 1, "waiting": 0,
              "done": 3, "no-fix": 0, "failed": 0, "ended": 0, "reported": 0}
  },
  "works": [{
    "id": 4, "issue": "sentry:billing:5120", "kind": "fix", "status": "running", "executor": "stairway",
    "ref": "aitomaton-billing-api-5120", "url": "https://claude.ai/code/…", "result_url": null, "summary": null,
    "created": "…", "updated": "…",
    "log": [{"at": "…", "status": "running", "note": "…"}],     // last 10
    "brief": "You are aitomaton's fixer…"                       // or the report body
  }],
  "findings": [{
    "id": "cascade:architecture:shop", "kind": "cascade", "scope": "architecture", "target": "shop",
    "severity": 0.8, "active": true, "first_at": "…", "last_at": "…",
    "detail": {"services": ["shop_db", "shop_api", "shop_web"], "root": "shop_db", "reason": "…", "onset": 1.8e9}
  }],
  "decisions": [{
    "id": 7, "issue": "sentry:billing:5120", "at": "…",
    "action": "fix", "applied": "fix", "policy": "pr", "decider": "laya", "confidence": 0.8,
    "rationale": "laya: urgent 0.80, code defect 0.96",
    "trail": [{"decider": "rules", "action": "fix", "confidence": 0.93, "code_defect": 0.8, "rationale": "…"},
              {"decider": "laya", "action": "fix", "confidence": 0.8, "code_defect": 0.96, "rationale": "…"}]
  }],
  "issues": [{
    "key": "sentry:billing:5120", "source": "sentry", "project": "billing", "service": "billing_api",
    "architecture": "billing", "title": "KeyError: 'vat_rate'", "culprit": "…", "level": "error",
    "status": "unresolved", "url": "…", "first_seen": "…", "last_seen": "…", "total": 45, "users": 37,
    "analysis": {
      "new": true, "regression": false, "events_1h": 45, "events_24h": 45,
      "burst": {"window": 45, "expected": 0.0, "ratio": 225.0, "burst": true, "onset": 1.8e9},
      "long_term": {"daily": [0, …, 45], "days_active": 1, "trend": 0.0, "rising": false, "chronic": false},
      "cascade": false, "cascade_root": false, "service_burst": true,
      "has_stacktrace": true, "in_app_frames": 2, "failed_tasks_1h": 0
    },
    "decision": {"action": "fix", "applied": "fix", "at": "…", "decider": "laya"}
  }],
  "architectures": [{"name": "shop", "services": 4, "issues": 4, "events_24h": 480, "cascade": {…} }],
  "services": [{
    "name": "shop_api", "stack": "shop", "architecture": "shop", "enabled": true,
    "repo": "acme/shop-api", "repo_hint": null, "ref": "main", "policy": "pr", "sentry_projects": ["shop-api"],
    "image": "…", "failed_tasks_1h": 0, "last_task_error": null,
    "replicas": {"running": 2, "desired": 2},   // null for global services
    "git": "env AITOMATON_GIT_TOKEN_ACME",       // where the credential comes from, never the value
    "infra_repo": null                           // aitomaton.infra.repo, when set
  }],
  "infrastructure": {                            // see infrastructure.md
    "enabled": true, "error": null, "name": "swarm",
    "outlet": {"repo": "acme/infra", "ref": "main", "path": "stacks/", "policy": "report", "git": "env …"},
    "service_outlets": ["acme/search-deploy"],
    "managers": {"total": 3, "reachable": 3, "quorum": 2},
    "nodes": [{"hostname": "manager-1", "role": "manager", "state": "ready", "availability": "active",
               "leader": true, "reachability": "reachable", "engine": "27.3.1", "cpus": 4.0, "memory_gb": 15.6}],
    "advisories": [{"kind": "engine_skew", "target": "nodes", "text": "…"}],
    "conditions": [{"key": "swarm:edge_traefik:oom", "kind": "oom", "subject": "edge_traefik",
                    "service": "edge_traefik", "level": "error", "title": "…", "decision": {…}}]
  }
}
```

Lists are newest first: up to 100 works, 200 findings (active first), 100 decisions and 200 issues from the
long-term horizon.
