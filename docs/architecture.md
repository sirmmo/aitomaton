# Architecture

aitomaton is one Python process (standard library only) with two threads: an HTTP server and a loop.

```
                 webhooks                                   polls
 Sentry/urgentry ──┐  Alertmanager ──┐       Sentry API ◀──┐   Docker socket ◀──┐   Prometheus ◀──┐
                   ▼                 ▼                     │                    │                 │
              ┌────────────── server.py ──────────────┐   ┌┴────────── engine.py (loop) ─────────┴─┐
              │ /hooks/sentry  /hooks/alertmanager    │──▶│ 1 refresh swarm (labels, tasks)         │
              │ /hooks/stairway  /status.json  /      │   │ 2 poll Sentry (count deltas)            │
              └───────────────────────────────────────┘   │ 3 analyse   (analysis.py)               │
                                                          │ 4 decide    (deciders.py)               │
                     store.py (SQLite)  ◀────────────────▶│ 5 act       (executors.py, workspace.py)│
                     issues · occurrences · findings      │ 6 follow up (stairway info, queue)      │
                     decisions · works · kv               └─────────────────────────────────────────┘
```

A webhook writes to the store and wakes the loop. Each tick runs every step in order under one lock.

## Modules

| module | role |
|---|---|
| `config.py` | environment → settings, plus the redacted summary for the status JSON |
| `store.py` | SQLite schema and queries. Occurrences are `(issue key, epoch minute, count)` |
| `swarm.py` | services, descriptor labels, failed tasks, image labels (cached) |
| `sources/sentry.py` | webhook parsing (integration platform, legacy plugin), HMAC check, event summary, API poller |
| `sources/swarm_health.py` | the swarm itself: nodes, quorum, replicas, classified task errors → conditions and advisories |
| `sources/prometheus.py` | Alertmanager parsing, PromQL context queries |
| `emergency.py` | layered policies, emergency detection, the runbook of direct actions (Docker API writes) |
| `analysis.py` | bursts, long-term trends, cascades; produces per-issue features and findings |
| `deciders.py` | the prose state, rules, laya, JEV/LLM tiers, the chain, guard rails, policy cap |
| `executors.py` | the brief, report bodies, result parsing, stairway MCP client, GitHub client |
| `credentials.py` | per-service git token resolution, redaction |
| `workspace.py` | cloning into the experiment with the service's credential, cleanup |
| `engine.py` | the loop, ingestion, dispatch, follow-up, the status JSON |
| `server.py` | HTTP endpoints and the static page |
| `laya/app.py` | the separate laya service |

## Data model

| table | key | holds |
|---|---|---|
| `issues` | `sentry:<project>:<id>` / `prom:<alert>:<service>` | metadata, service, architecture, latest event summary (`detail`), flags (regression) |
| `occurrences` | (key, minute) | counts. Pruned after `LONG_TERM_DAYS` + 1 |
| `findings` | `<kind>:<scope>:<target>` | severity, detail, first/last seen, active |
| `decisions` | autoincrement | action, applied action, policy, decider, confidence, rationale, features + trail |
| `works` | autoincrement | kind, status, executor, ref (experiment / URL), links, brief, log |
| `kv` | | poll cursors and last seen counts |

## Design choices

- **Minute buckets for everything**, so webhooks, poll deltas and alerts are analysed by the same code.
- **The swarm is the configuration**: no registry of apps to keep in sync. A label change takes effect at the
  next refresh.
- **Cheap tiers first.** Rules are free, laya is milliseconds-to-seconds on CPU, LLMs cost money and time. Each
  tier is asked only when the previous one is unsure, and any tier can be down.
- **Prepared workspaces.** aitomaton, not the agent, handles credentials. The agent gets a clone that can push
  and nothing else.
- **Dry run by default**, with every brief visible, so you can judge the decisions before anything is sent.

## Towards holodocker
aitomaton reads `holodocker.repo*` labels and holodocker deploys urgentry, so they already fit together. To
fold it in: `swarm.py` → holodocker's `swarm/` mirror, the store → `observe` models, decisions and works →
`Release` / `ApprovalRequest` / `ExecutionLog`, the deciders → `llm_review`.
