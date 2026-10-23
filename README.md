# aitomaton

**Errors in, decisions made, fixes out.**

aitomaton listens to Sentry or [urgentry](https://urgentry.com) (and Prometheus / Alertmanager). It
analyses every error as a single event, as part of a burst and as a long-term pattern, for one application or
across a whole architecture. Then it decides what to do with a decision model: rules,
[laya](https://github.com/convaiinnovations/laya), JEV on OpenRouter, or any LLM. When the answer is
*this is a code defect*, it opens a [stairway](https://github.com/sirmmo/stairway) space where a coding agent
fixes it and sends a pull request (or pushes, if the policy allows).

Which repo each service comes from, and what aitomaton may do about it, are read from **Docker Swarm service
labels**: the swarm is the configuration.

**Site and live demo:** https://ingmmo.com/aitomaton/

```
 Sentry / urgentry ─┐                      ┌──────────── aitomaton ────────────┐
 Alertmanager ──────┼──▶ webhooks / poll ─▶│ single · burst · long-term        │──▶ status.json ──▶ status page
 Prometheus ◀───────┤                      │ issue · service · architecture    │
 Swarm labels ◀─────┘                      │ rules → laya → jev → llm          │
                                           │ capped by the service's policy    │
                                           └──────┬─────────────────┬──────────┘
                                             fix  ▼                 ▼ report
                                   stairway: clone · fix · test     GitHub issue
                                   → branch → PR (or push)
```

## In one minute

```yaml
# on any service of your swarm
deploy:
  labels:
    aitomaton.repo: https://github.com/acme/shop-api
    aitomaton.policy: pr                 # observe | report | pr | push
    aitomaton.sentry.project: shop-api
    aitomaton.depends: db                # helps find the root of a cascade
    aitomaton.git.credential: acme       # the token's name, never the token
```

```bash
docker build -t aitomaton:0.1 . && docker build -t aitomaton-laya:0.1 laya
cp .env.example .env && $EDITOR .env     # urgentry/Sentry, stairway, OpenRouter, tokens
docker stack deploy -c stack.yml aitomaton
# point Sentry/urgentry at /hooks/sentry and Alertmanager at /hooks/alertmanager
open http://localhost:8790/
```

It starts in **dry-run** mode (`AITOMATON_DISPATCH=0`): it makes every decision and writes every agent brief
on the page, but sends nothing until you turn it on.

## What it does

- **Three horizons.** A single error (new, regression, an in-app stack trace); a burst (the last 5 minutes vs
  the 24 h baseline); long-term patterns (14-day trend, chronic issues).
- **Three scopes.** Issue, service (including failing swarm tasks such as OOM kills) and architecture. When
  several services of a stack fail together, aitomaton finds the root and does not "fix" the victims.
- **Cheap decisions first.** Deterministic rules always run. laya gives calibrated yes/no reads in one
  forward pass. JEV and a System-2 LLM are asked only when the earlier tiers are unsure or disagree. Guard
  rails and the service's policy cap every decision.
- **Prepared workspaces.** For a fix, aitomaton clones the repo into the stairway experiment with the
  service's own token, so the agent only commits and pushes. aitomaton then opens the PR. Tokens come from
  env vars or Docker secrets, chosen per label, per repo owner, or globally.
- **One JSON for everything.** `/status.json` lists works in progress, findings, every decision with each
  tier's reasoning, and the issues with their analysis. `/` renders it.

## Documentation

| | |
|---|---|
| [Architecture](docs/architecture.md) | components, loop, data model, design choices |
| [Service labels](docs/labels.md) | the descriptor labels, policies, git credentials |
| [Analysis](docs/analysis.md) | bursts, trends, cascades, findings |
| [Decisions](docs/decisions.md) | rules, laya, JEV, LLM, the chain, guard rails |
| [Works](docs/works.md) | the fix lifecycle, the agent's result protocol, git handling, reports |
| [Integrations](docs/integrations.md) | Sentry/urgentry, Prometheus/Alertmanager, stairway, laya, OpenRouter, GitHub |
| [Configuration](docs/configuration.md) | every environment variable |
| [status.json](docs/status-json.md) | the output format |
| [Deployment and security](docs/deployment.md) | stack, rollout, threat model |

## Development

```bash
python3 -m unittest discover -s tests      # no network or Docker needed
python3 scripts/demo_status.py > status.json   # the synthetic data behind the site's demo
```

Python 3.8+, standard library only (like stairway); `git` for prepared workspaces. The laya tier is a separate
FastAPI container.

aitomaton is meant to become part of holodocker, an infrastructure manager for Docker Swarm (not yet public). It already reads
`holodocker.repo*` labels; see [Architecture](docs/architecture.md#towards-holodocker).

## License

[MIT](LICENSE)
