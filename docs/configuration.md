# Configuration

Every setting is an environment variable, usually set in `.env` (see [`.env.example`](../.env.example)) or in
the stack file. Per-service behaviour comes from [service labels](labels.md).

## Server

| variable | default | meaning |
|---|---|---|
| `AITOMATON_HOST` / `AITOMATON_PORT` | `0.0.0.0` / `8790` | listen address |
| `AITOMATON_PUBLIC_URL` | `http://localhost:8790` | base URL used in links |
| `AITOMATON_DB` | `./state/aitomaton.db` (`/state/…` in the image) | SQLite state |
| `AITOMATON_STATIC` | `./static` | folder served at `/` |
| `AITOMATON_TICK_SECONDS` | `30` | loop period. Webhooks wake the loop earlier |
| `AITOMATON_STATUS_TOKEN` | none | protects `/` and `/status.json` (`?token=` or `Authorization: Bearer`) |
| `AITOMATON_MCP_TOKEN` | none | enables the [MCP server](mcp.md) at `/mcp`: `name:token`, comma separated |
| `MCP_DECISION_WAIT_SECONDS` | `20` | how long `report_issue` waits for the decision before answering |
| `MCP_REPORTS_PER_HOUR` | `60` | reports and observations per hour, MCP and Telegram together, all callers together |

## Inputs

| variable | default | meaning |
|---|---|---|
| `SENTRY_URL` | none | `https://sentry.io`, or your urgentry / self-hosted Sentry. Enables polling with the two below |
| `SENTRY_TOKEN` | none | API token (Bearer) with read access to projects and issues |
| `SENTRY_ORG` | none | organization slug |
| `SENTRY_PROJECTS` | all projects | comma-separated project slugs to poll |
| `SENTRY_POLL_SECONDS` | `120` | poll period |
| `SENTRY_HOOK_SECRET` | none | the integration's client secret; verifies `Sentry-Hook-Signature` on `/hooks/sentry` |
| `PROMETHEUS_URL` | none | enables metric context for decisions |
| `PROMETHEUS_DEFAULT_SELECTOR` | `{container_label_com_docker_swarm_service_name="{service}"}` | selector when a service has no `aitomaton.prometheus.selector` (cAdvisor labels) |
| `PROMETHEUS_QUERIES` | memory, memory/limit, CPU | JSON `{name: promql}`; `{selector}` is replaced per service. `memory_limit_ratio` feeds the rules |
| `ALERTMANAGER_HOOK_SECRET` | none | required `?token=` on `/hooks/alertmanager` |
| `DOCKER_SOCKET` | `/var/run/docker.sock` | a manager's socket, or `tcp://host:port` for a socket proxy. Reads only, unless `AITOMATON_ACT=1` |
| `AITOMATON_LABEL_PREFIX` | `aitomaton` | label namespace |
| `SWARM_REFRESH_SECONDS` | `60` | how often services, labels and tasks are re-read |

## Infrastructure

See [infrastructure.md](infrastructure.md).

| variable | default | meaning |
|---|---|---|
| `AITOMATON_INFRA` | `1` | watch the swarm itself: nodes, managers, replicas, task errors |
| `AITOMATON_INFRA_NAME` | `swarm` | name of the infrastructure as an architecture |
| `AITOMATON_INFRA_REPO` | none | the infrastructure outlet: the repo with the stack files. Without it, infrastructure reports stay on the page |
| `AITOMATON_INFRA_REF` / `AITOMATON_INFRA_PATH` | `main` / none | its base branch, and where the stack files are |
| `AITOMATON_INFRA_POLICY` | `report` | `observe`, `report`, `pr` or `push` for infrastructure issues |
| `AITOMATON_INFRA_POLICY_EMERGENCY` | the above | its emergency layer, e.g. `pr+act` ([emergencies.md](emergencies.md)) |
| `AITOMATON_INFRA_CREDENTIAL` | none | name of its git credential (as `aitomaton.git.credential`) |
| `AITOMATON_INFRA_TEST` / `AITOMATON_INFRA_NOTES` | none | validation command and notes for the agent |
| `INFRA_GRACE_SECONDS` | `300` | how long a service may run below its replicas before it is a condition |
| `INFRA_TASK_FAILURES` | `3` | failed tasks of one kind per hour that make a condition |

## Emergencies

See [emergencies.md](emergencies.md).

| variable | default | meaning |
|---|---|---|
| `AITOMATON_EMERGENCY_POLICY` | the normal layer | emergency layer of enabled services without `aitomaton.policy.emergency`, e.g. `push` or `pr+act` |
| `AITOMATON_INFRA_POLICY_EMERGENCY` | the outlet's normal layer | the infrastructure outlet's emergency layer |
| `EMERGENCY_WINDOW_SECONDS` | `21600` | only episodes that began this recently are emergencies |
| `ROLLBACK_WINDOW_SECONDS` | `3600` | trouble this soon after a deploy puts `rollback` first |
| `AITOMATON_ACT` | `0` | master switch for direct actions. Off: they are recorded as dry-runs |
| `ACT_VERIFY_SECONDS` | `300` | time a step gets to work before the next one |
| `MAX_ACTIONS_PER_HOUR` | `3` | direct actions on the whole swarm |
| `ACT_MEMORY_FACTOR` / `ACT_MEMORY_MAX_MB` | `1.5` / `8192` | how `raise_memory` raises a limit, and its ceiling |

## Notifications

See [notifications.md](notifications.md).

| variable | default | meaning |
|---|---|---|
| `AITOMATON_TELEGRAM_TOKEN` | the Docker secret `aitomaton_telegram_token` | bot token from @BotFather |
| `AITOMATON_TELEGRAM_CHAT` | none | chat ids, comma separated. `<chat>/<topic>` posts in a forum topic |
| `AITOMATON_TELEGRAM_API` | `https://api.telegram.org` | or a self-hosted Bot API server |
| `AITOMATON_NOTIFY_URL` | none | Slack-compatible incoming webhook (Slack, Mattermost, Rocket.Chat, Discord's `/slack`) |
| `AITOMATON_NOTIFY_EVENTS` | `emergency,fix` | what is notified: `emergency`, `fix` (PR opened, pushed, failed), `report` (GitHub issue opened), `reported` (a person's new report, from MCP or a Telegram reporter) |
| `AITOMATON_TELEGRAM_INTAKE` | `off` | the bot takes [reports](telegram-intake.md): `on` (operators and listed reporters) or `open` (anyone) |
| `AITOMATON_TELEGRAM_OPERATORS` | none | user ids or `@usernames` treated as operators, besides the members of the notification chats |
| `AITOMATON_TELEGRAM_REPORTERS` | none | user ids, `@usernames`, or group chat ids whose members may report |
| `TELEGRAM_REPORTS_PER_HOUR` | `5` | reports and additions per reporter |

## Analysis

| variable | default | meaning |
|---|---|---|
| `BURST_WINDOW_SECONDS` | `300` | the "now" window |
| `BURST_MIN_EVENTS` | `10` | fewer events in the window are never a burst |
| `BURST_RATIO` | `5.0` | window vs the rate expected from the baseline |
| `BASELINE_WINDOW_SECONDS` | `86400` | the baseline before the window |
| `LONG_TERM_DAYS` | `14` | horizon for trends and chronic issues (and data retention) |
| `CASCADE_WINDOW_SECONDS` | `600` | service bursts starting this close together form a cascade |
| `NEW_ISSUE_SECONDS` | `3600` | first seen within this is "new" |

## Decision

| variable | default | meaning |
|---|---|---|
| `AITOMATON_DECIDER` | `chain` | `rules`, `laya`, `jev`, `llm` (rules + that one tier) or `chain` |
| `AITOMATON_CHAIN` | `laya,jev,llm` | tier order after the rules. Tiers that are not configured are skipped |
| `ESCALATE_BELOW` | `0.6` | the next tier is asked when the last is below this confidence (or disagrees with the rules about fixing) and the stakes are `report`/`fix` |
| `LAYA_URL` | none | the laya service (`http://laya:8000` in the stack) |
| `JEV_MODEL` | none | e.g. `typesafe/jev-router`. Empty means the tier is off |
| `JEV_BASE_URL` / `JEV_API_KEY` | the LLM's | where JEV is served (OpenRouter by default) |
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` | any OpenAI-compatible API: OpenRouter, Ollama (`http://ollama:11434/v1`), vLLM… |
| `LLM_API_KEY` | `$OPENROUTER_API_KEY` | not needed for local endpoints |
| `LLM_MODEL` | `deepseek/deepseek-v4.1-flash` | the System-2 model |
| `DECISION_COOLDOWN_SECONDS` | `3600` | minimum time before an issue is decided again (unless it escalates) |
| `AITOMATON_DEFAULT_POLICY` | `report` | policy of enabled services without `aitomaton.policy` |

## Execution

| variable | default | meaning |
|---|---|---|
| `AITOMATON_DISPATCH` | `0` | `0`: dry run (decisions and briefs only). `1`: start fixes, open issues and PRs |
| `MAX_RUNNING_WORKS` | `2` | concurrent fixes; more are queued |
| `MAX_WORKS_PER_DAY` | `5` | fixes started per 24 h |
| `STAIRWAY_URL` / `STAIRWAY_TOKEN` | none | stairway's MCP endpoint (`http://172.18.0.1:8765` from a swarm container) and token |
| `STAIRWAY_AGENT` | `claude` | `claude`, `opencode`, `crush` or `openclaude` (overridable per service) |
| `STAIRWAY_MODEL` | stairway's default | model for non-Claude agents (an OpenRouter id) |
| `STAIRWAY_PERMISSION_MODE` | `acceptEdits` | permission mode of fix sessions |
| `STAIRWAY_PREFIX` | `aitomaton` | experiments are named `<prefix>-<service>-<issue>` |
| `STAIRWAY_EXPERIMENTS_ROOT` | none | stairway's root as aitomaton sees it (mounted at the same path). Set: aitomaton clones the repo for the agent |
| `AITOMATON_CALLBACK_URL` | `http://localhost:8790/hooks/stairway` | where stairway posts replies (https, or http on localhost) |
| `AITOMATON_CALLBACK_SECRET` | none | 16+ characters. Enables callbacks; without it aitomaton polls `experiment_info` |

## Git

| variable | default | meaning |
|---|---|---|
| `AITOMATON_GIT_TOKEN` | `$GITHUB_TOKEN` | the fallback token |
| `AITOMATON_GIT_TOKEN_<NAME>` | none | a named token (`aitomaton.git.credential`) or a per-owner one (`…_ACME_CORP`) |
| `AITOMATON_SECRETS_DIR` | `/run/secrets` | where Docker secrets `aitomaton_git_<name>` are read |
| `AITOMATON_GIT_AUTHOR` | `aitomaton <aitomaton@localhost>` | default commit identity |
| `AITOMATON_GIT_BASE` / `GITHUB_API` | `https://github.com` / `https://api.github.com` | for GitHub Enterprise |

See [labels.md](labels.md#git-credentials) for how a service picks its token.
