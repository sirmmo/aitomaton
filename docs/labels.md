# Service labels

aitomaton's configuration per service lives on the service itself, as Docker Swarm labels. Deploy labels
(`deploy.labels`) are read first, container labels second. The prefix is `aitomaton` (`AITOMATON_LABEL_PREFIX`).

```yaml
services:
  api:
    image: ghcr.io/acme/shop-api:1.4
    deploy:
      labels:
        aitomaton.repo: https://github.com/acme/shop-api
        aitomaton.ref: main
        aitomaton.policy: pr
        aitomaton.sentry.project: shop-api
        aitomaton.depends: db
        aitomaton.test: pytest -q
        aitomaton.git.credential: acme
```

## Reference

| label | default | meaning |
|---|---|---|
| `aitomaton.enable` | on when a repo label is present | `false` opts out. `true` opts in, which also allows the image's source label as the repo |
| `aitomaton.repo` | `holodocker.repo` | GitHub repository: `owner/name`, `https://github.com/owner/name(.git)` or `git@github.com:owner/name.git` |
| `aitomaton.ref` | `holodocker.repo_ref`, else `main` | base branch for fixes |
| `aitomaton.path` | `holodocker.repo_path` | subdirectory, for monorepos |
| `aitomaton.policy` | `AITOMATON_DEFAULT_POLICY` | `observe`, `report`, `pr` or `push`: the most aitomaton may do |
| `aitomaton.policy.emergency` | `AITOMATON_EMERGENCY_POLICY`, else `aitomaton.policy` | the layer in force during an [emergency](emergencies.md), e.g. `push` or `pr+act` |
| `aitomaton.notify.telegram` | — | more Telegram chats for this service's notifications, e.g. the team's group (`-100123` or `-100123/7` for a topic) |
| `aitomaton.act` | — | `false`: never run direct actions on this service, whatever a policy says |
| `aitomaton.sentry.project` | the service's name, with or without the stack prefix | Sentry/urgentry project slugs, comma separated |
| `aitomaton.architecture` | the stack namespace | group for architecture-wide analysis. Several stacks can share one |
| `aitomaton.depends` | none | services this one needs (full or short names), used to find the root of a cascade |
| `aitomaton.prometheus.selector` | `PROMETHEUS_DEFAULT_SELECTOR` | PromQL selector, e.g. `{job="api"}` |
| `aitomaton.agent` | `STAIRWAY_AGENT` | coding agent for this service |
| `aitomaton.model` | `STAIRWAY_MODEL` | its model (non-Claude agents) |
| `aitomaton.test` | none | the command that runs the tests, given to the agent |
| `aitomaton.notes` | none | free text for the agent: conventions, what not to touch |
| `aitomaton.git.credential` | none | **name** of the git credential (below) |
| `aitomaton.git.author` | `AITOMATON_GIT_AUTHOR` | commit identity, `Name <email>` |
| `aitomaton.infra.repo` | the global outlet (`AITOMATON_INFRA_REPO`) | where this service's **deployment** lives. Infrastructure issues (OOM kills, image pulls, replicas) go here. It works without `aitomaton.enable`, even on third-party images |
| `aitomaton.infra.path` / `.ref` / `.policy` / `.policy.emergency` / `.credential` | the outlet's | stack file, branch, policy and credential name for those issues |

### Policies

| policy | `ignore` / `watch` | `report` | `fix` |
|---|---|---|---|
| `observe` | recorded | becomes `watch` | becomes `watch` |
| `report` | recorded | GitHub issue (or listed on the page) | becomes `report` |
| `pr` | recorded | GitHub issue | stairway fix, delivered as a pull request |
| `push` | recorded | GitHub issue | stairway fix, pushed to `ref` when the tests pass (PR otherwise) |

Infrastructure issues follow the outlet's policy, not the service's ([infrastructure.md](infrastructure.md)).

Services without labels are still discovered and analysed (their failing tasks, their part in a cascade),
but their policy is `observe`.

### Repos from images

`org.opencontainers.image.source` on the image is shown on the status page as a **hint**. It becomes the repo
only with `aitomaton.enable: "true"`. Otherwise every third-party image (traefik, grafana, zitadel…) would be
"fixed" upstream.

## Git credentials

Each service works with its own token, resolved most specific first:

1. `aitomaton.git.credential: <name>`: the Docker secret `aitomaton_git_<name>`, else the env var
   `AITOMATON_GIT_TOKEN_<NAME>` (upper case, non-alphanumerics as `_`). If neither exists, the service gets
   **no** token; it never falls back to a broader one.
2. The repo owner: `AITOMATON_GIT_TOKEN_<OWNER>`, e.g. `AITOMATON_GIT_TOKEN_ACME_CORP` for every
   `Acme-Corp/*` repo, without any label.
3. `AITOMATON_GIT_TOKEN` (or `GITHUB_TOKEN`).

The label holds a **name, never a token**. Labels are readable by anyone who can `docker service inspect`
and are copied by tools that mirror the swarm, so a label value that looks like a token is refused.

```bash
printf %s "$TOKEN" | docker secret create aitomaton_git_acme -
```
```yaml
services:
  aitomaton:
    secrets: [aitomaton_git_acme]
secrets:
  aitomaton_git_acme:
    external: true
```

Use fine-grained tokens limited to the service's repositories: *Contents* and *Pull requests* read/write,
plus *Issues* for `report`. What the token is used for is described in [works.md](works.md#git).
