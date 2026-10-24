# Emergencies

When production is down, a pull request waiting for review is not enough. Policies therefore have **two
layers**:

- the **normal** layer (`aitomaton.policy`) applies to everything;
- the **emergency** layer (`aitomaton.policy.emergency`) replaces it while an emergency lasts. It can go
  further: `push` instead of `pr`, and `+act` for direct actions on the swarm.

```yaml
deploy:
  labels:
    aitomaton.policy: pr                    # normally: a pull request
    aitomaton.policy.emergency: push+act    # production down: push when tests pass, and act now
```

| emergency layer | what happens during an emergency |
|---|---|
| not set | the normal layer, but at least `report`: an emergency is never only observed |
| `report` | an issue and a notification |
| `pr` / `push` | the fix is delivered as a PR, or pushed when the tests pass |
| `…+act` | the runbook runs as well: aitomaton itself restarts, rolls back or raises a limit |

The infrastructure outlet has the same two layers: `AITOMATON_INFRA_POLICY` and
`AITOMATON_INFRA_POLICY_EMERGENCY`, or `aitomaton.infra.policy.emergency` per service. Defaults:
`AITOMATON_EMERGENCY_POLICY` for enabled services. Services that are not enabled only get the
`report` layer, on the page.

The page and `status.json` show which layer each decision was taken under, e.g. `push+act (emergency)`.

## What counts as an emergency

Emergencies are decided **by rules, not by a model**: what counts as "production is down" must be predictable.

| emergency | first runbook steps |
|---|---|
| raft quorum lost, a manager unreachable | none: a human has to act |
| a node down | none: swarm reschedules its tasks by itself |
| a service at 0 replicas (not because a node died) | `rollback` if it was deployed just before, then `restart` |
| a service OOM-killed and short of replicas | `raise_memory` |
| a service crash-looping at 0 replicas | `rollback` after a recent deploy, else `restart` |
| image pull failures or a paused update, short of replicas | `rollback` after a recent deploy |
| unschedulable or port conflict at 0 replicas | none: the fix is a deployment change (the PR) |
| an error burst (service-wide, or fatal) within `ROLLBACK_WINDOW_SECONDS` of a deploy | `rollback` |
| a critical Prometheus alert on a known service | none |

Only **recent** episodes count: they must begin within `EMERGENCY_WINDOW_SECONDS` (6 h). They must also have
begun **while aitomaton was watching**. A service that was already down when aitomaton started, or that has
been down for weeks, is reported but is never an emergency, so the runbook never "revives" a service someone
abandoned. Losing the control plane is the exception: it is always an emergency.

## The runbook

Direct actions come from a fixed runbook that aitomaton runs itself through the Docker API. No model gets write
access to the swarm, and no free-form command is ever run.

| step | what it does | equivalent |
|---|---|---|
| `restart` | forces a new set of tasks | `docker service update --force` |
| `rollback` | returns to the previous spec | `docker service rollback` |
| `raise_memory` | multiplies the memory limit by `ACT_MEMORY_FACTOR` (1.5), up to `ACT_MEMORY_MAX_MB` | `docker service update --limit-memory` |

How it escalates:
- Each step runs **once per episode**.
- A step gets `ACT_VERIFY_SECONDS` (5 min) to work. If the emergency is still on after that, the next step runs.
- When no steps are left, aitomaton records "runbook exhausted: needs a human" and notifies again.
- Across the whole swarm, at most `MAX_ACTIONS_PER_HOUR` actions run.
- When the condition clears, the emergency closes with a final notification.

A direct action is a mitigation, not the fix. It changes the running service, not your stack file, so the next
`docker stack deploy` undoes it. That is why the emergency layer usually combines `+act` with `pr` or `push`:
for example, the raised memory limit becomes a PR to the stack file.

### Safety

Direct actions run only when all of these hold:
1. `AITOMATON_DISPATCH=1` and `AITOMATON_ACT=1`. Otherwise every step is recorded as a dry-run ("would restart
   shop_web") so you can see what it would have done.
2. The emergency layer in force includes `+act`.
3. The service does not have `aitomaton.act: "false"`. The stack file sets this on aitomaton and laya.
4. The emergency is recent and began while aitomaton was watching.

A `:ro` mount of `docker.sock` does **not** make the API read-only. For least privilege, put a socket proxy
in front, for example `tecnativa/docker-socket-proxy` with `SERVICES=1 NODES=1 TASKS=1`, plus `POST=1` only
when direct actions are on. Then set `DOCKER_SOCKET=tcp://socket-proxy:2375`.

For services that pull private images, the swarm keeps the registry credentials it got at deploy time.
`restart` and `rollback` do not send new ones, so if those credentials have expired, the nodes may fail to pull.

## Notifications

Set `AITOMATON_NOTIFY_URL` to a Slack-compatible incoming webhook. That covers Slack, Mattermost and
Rocket.Chat; for Discord, append `/slack` to the webhook URL. aitomaton posts:
- when an emergency is declared;
- after each direct action;
- when the runbook is exhausted;
- when the emergency is over.

Nothing is sent with `AITOMATON_DISPATCH=0`.

## In status.json

```jsonc
"emergencies": [{
  "issue": "swarm:edge_traefik:oom", "service": "edge_traefik", "since": "…",
  "reason": "edge_traefik is OOM-killed and short of replicas",
  "policy": "pr+act (emergency)", "runbook": ["raise_memory"],
  "actions": [{"step": "raise_memory", "status": "done", "at": "…",
               "note": "raised edge_traefik's memory limit from 128 to 192 MiB"}]
}]
```

Each emergency is also a work of kind `emergency` (running until it is over), and each step a work of kind
`act`.
