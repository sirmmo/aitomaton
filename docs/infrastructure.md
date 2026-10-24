# Infrastructure

aitomaton also watches the **swarm itself**. Swarm health shows up the same way application errors do: as
issues, with findings and decisions. These issues go to an **infrastructure outlet**, a repository that holds
your stack files (your infrastructure as code), not to the service's application repo.

This matters most for third-party images. aitomaton must never "fix" traefik or postgres upstream, but when
traefik is OOM-killed, the fix (a higher memory limit) belongs in *your* stack file. aitomaton can make that
change.

## Conditions

Every swarm refresh (`SWARM_REFRESH_SECONDS`), aitomaton reads the nodes, services and tasks through the
manager's socket. A condition is an issue with source `swarm` and key `swarm:<subject>:<kind>`. It stays open
while the state lasts and is resolved when the state ends. If it comes back, it starts a new episode.

| condition | scope | when | can a stack-file change fix it? |
|---|---|---|---|
| `node_down` | cluster | a node is not `ready` | no: report |
| `manager_unreachable` | cluster | a manager's raft reachability is not `reachable` | no: report |
| `quorum_lost` | cluster | fewer reachable managers than `managers/2 + 1` | no: report |
| `oom` | service | ≥ `INFRA_TASK_FAILURES` tasks in 1 h killed for memory (OOMKilled, exit 137) | likely (raise the limit) |
| `image_pull` | service | … that fail to pull (no such image, manifest unknown, unauthorized) | often (the tag) |
| `unschedulable` | service | … that no node accepts (constraints, resources) | often (placement, reservations) |
| `port_conflict` | service | … whose published port is taken | often |
| `crash_loop` | service | … that exit with any other error | sometimes: usually the app |
| `under_replicated` | service | fewer running tasks than replicas for `INFRA_GRACE_SECONDS` | rarely |
| `update_paused` | service | a rolling update paused or rolled back recently, or the service is short of replicas | sometimes |

Service conditions are attached to their service and its architecture, so they appear next to that
service's errors. Cluster conditions belong to the `swarm` architecture (`AITOMATON_INFRA_NAME`).

**Causes are linked.** When a node is down, `under_replicated` and `unschedulable` conditions get
`cause: node X is down`. The deciders then treat them as the node's problem: nobody edits a stack file
because a host died.

**Advisories** are shown on the page and in `status.json` but are never acted on. They cover:
- a single manager;
- an even number of managers;
- Docker engine major versions that differ across nodes;
- drained or paused nodes;
- updates that paused more than a day ago on services that run fine.

Conditions are states, not error streams. They do not count toward a service's bursts or cascades. They
count one occurrence per refresh, or one per failed task for task errors, so their 14-day history shows how
long and how often they fired.

## The outlet

An infrastructure issue is acted on through an *infrastructure target* instead of the service:

1. If the service has `aitomaton.infra.repo`, that repo is the target: the deployment repo for that one stack.
   `aitomaton.infra.path`, `.ref`, `.policy` and `.credential` refine it.
2. Otherwise, the global outlet applies: `AITOMATON_INFRA_REPO`, with `AITOMATON_INFRA_PATH`, `_REF`,
   `_POLICY`, `_CREDENTIAL`, `_TEST` and `_NOTES`.
3. Without a repo, reports stay on the status page.

Alertmanager alerts that name no known service (disk full on a host, a node exporter alert) also go to the
global outlet.

`aitomaton.infra.*` labels do not need `aitomaton.enable`. They describe where the *deployment* lives, so they
are safe on third-party services.

```yaml
services:
  traefik:
    image: traefik:v3.1
    deploy:
      labels:
        aitomaton.infra.repo: acme/edge-deploy
        aitomaton.infra.path: stacks/edge.yml
        aitomaton.infra.policy: pr
```

```bash
AITOMATON_INFRA_REPO=acme/infra
AITOMATON_INFRA_PATH=stacks/
AITOMATON_INFRA_POLICY=report        # observe | report | pr | push
AITOMATON_INFRA_CREDENTIAL=infra     # Docker secret aitomaton_git_infra, or AITOMATON_GIT_TOKEN_INFRA
AITOMATON_INFRA_TEST="for f in stacks/*.yml; do docker stack config -c $f >/dev/null || exit 1; done"
```

Git credentials resolve exactly as they do for services ([labels.md](labels.md#git-credentials)). The
credential name comes from `.infra.credential` / `AITOMATON_INFRA_CREDENTIAL`, falling back to the owner token
and then the global token.

## Decisions

The same chain runs ([decisions.md](decisions.md)), with two differences:

- **Severity.** A condition fires only past its thresholds, so it starts higher than a single error does.
- **"Code defect" becomes "fixable in the infrastructure repo".** The rules take it from the table above, and
  lower it to near zero when a node is down. laya gets a different yes/no question for it. JEV and the LLM are
  told that the target is a repo of stack files, where a fix is a deployment change.

The same guard rails apply, and the policy is the outlet's. With the default `report` policy, aitomaton opens
one GitHub issue per episode in the infrastructure repo. With `pr`, an OOM kill becomes a stairway session that
gets this brief:
- find the service in the stack files;
- make the smallest change that removes the cause (a memory limit, an image tag, a placement constraint, a
  port);
- validate the files with `docker stack config`;
- never deploy and never run docker commands against the swarm.

The branch is `aitomaton/fix-<service>-<condition>`, and aitomaton opens the PR. **Deploying the merged change
is up to you** (or to holodocker).

## Reports, once per episode

This applies to every issue, not only infrastructure ones. A report is opened once per episode. When an issue
is decided again and still calls for a report, the existing one is noted ("still happening") instead of being
opened again. A new episode starts with a Sentry regression or when a swarm condition fires again after being
resolved.

## Requirements

- aitomaton must run on a **manager** (`/nodes` answers only there). The stack places it with
  `node.role == manager`. On a worker, the health checks report the error and the issues are left as they are.
- `AITOMATON_INFRA=0` turns it all off.
