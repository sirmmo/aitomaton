# Integrations

## Sentry and urgentry

Both expose the same `/api/0/` API, and either (or both) of these works:

**Webhooks** (immediate). In Sentry: *Settings → Developer Settings → Custom Integration (internal)*.
Webhook URL `https://<aitomaton>/hooks/sentry`; enable *issue* and *error* resources (or use an *alert rule*
whose action sends a notification via the integration: that is the `event_alert` resource). Put the
integration's *client secret* in `SENTRY_HOOK_SECRET`. The legacy *WebHooks* plugin payload is accepted too.
In urgentry, point its webhook/alert target at the same URL.

**Polling** (history, and a safety net for missed webhooks):
```
SENTRY_URL=https://urgentry.example.org   # or https://sentry.io
SENTRY_TOKEN=…                            # read scopes: project:read, event:read, org:read
SENTRY_ORG=acme
SENTRY_PROJECTS=shop-api,shop-web         # optional
```
The first poll of a project seeds 14 days of history from issue stats, so long-term analysis works from day
one. Before deciding, aitomaton fetches the issue's latest event for its stack trace, when a webhook did not
bring one.

Map projects to services with `aitomaton.sentry.project` (otherwise by name).

## Prometheus and Alertmanager

```
PROMETHEUS_URL=http://prometheus:9090
```
For each decision aitomaton runs `PROMETHEUS_QUERIES` for the service (default: memory, memory / limit, CPU
over cAdvisor's `container_label_com_docker_swarm_service_name`). `memory_limit_ratio` over 0.9 lowers the
code-defect estimate. All values reach the LLM tiers and the brief.

Alertmanager receiver:
```yaml
receivers:
  - name: aitomaton
    webhook_configs:
      - url: http://aitomaton:8790/hooks/alertmanager?token=<ALERTMANAGER_HOOK_SECRET>
        send_resolved: true
```
The alert's service comes from the `service`, `swarm_service`, `container_label_com_docker_swarm_service_name`
or `job` label; `severity` maps to the level (`critical`/`page` → fatal).

## Docker Swarm
aitomaton reads `/services`, `/tasks` and `/images/<ref>/json` through a manager's socket. Mounting it read
only is enough. See [labels.md](labels.md).

## stairway
[stairway](https://github.com/sirmmo/stairway) runs the coding agents. aitomaton calls its MCP endpoint
(`POST /mcp`, `Authorization: Bearer <STAIRWAY_TOKEN>`): `create_experiment`, `start_experiment` and
`experiment_info`. From a swarm container the host is usually `172.18.0.1` (docker_gwbridge). stairway
accepts callback URLs that are https or http on localhost, which is why the stack publishes port 8790 and
the default callback is `http://localhost:8790/hooks/stairway`.

## laya
`laya/` is a small FastAPI service around laya's `Router` (`POST /predict {state, questions}`). It uses CPU
torch, and the checkpoint is cached in the `laya-cache` volume.
```bash
docker build -t aitomaton-laya:0.1 laya
```

## JEV, OpenRouter, local models
```
JEV_MODEL=typesafe/jev-router               # the JEV tier, on OpenRouter by default
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-…
LLM_MODEL=deepseek/deepseek-v4.1-flash      # the System-2 tier
```
For a local model, set `LLM_BASE_URL=http://ollama:11434/v1` and leave the key empty. The coding agent's
model is separate: `STAIRWAY_AGENT=opencode` + `STAIRWAY_MODEL=<openrouter id>`, or per service with
`aitomaton.agent` / `aitomaton.model`.

## GitHub
Tokens per service ([labels.md](labels.md#git-credentials)) are used to clone, push, open PRs and open
issues. Set `AITOMATON_GIT_BASE` and `GITHUB_API` for GitHub Enterprise.
