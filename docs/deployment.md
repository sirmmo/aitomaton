# Deployment and security

## On Docker Swarm

```bash
git clone https://github.com/sirmmo/aitomaton.git && cd aitomaton
docker build -t aitomaton:0.1 .
docker build -t aitomaton-laya:0.1 laya          # optional laya tier (CPU torch, ~1 GB)
cp .env.example .env && chmod 600 .env && $EDITOR .env
docker stack deploy -c stack.yml aitomaton
```

[`stack.yml`](../stack.yml) runs:

- **aitomaton** on a manager, with the Docker socket read only, a `state` volume for SQLite, `/srv` (stairway's
  experiments root) at the same path, and port 8790 published so stairway on the host can call back on
  localhost. It is labelled `aitomaton.enable=false`, so it never tries to fix itself.
- **laya**, with its checkpoint in the `laya-cache` volume.

To put it behind Traefik, uncomment the labels in the stack file. Set `AITOMATON_STATUS_TOKEN` if the page is
reachable from outside.

## Without Docker
```bash
set -a; . ./.env; set +a
python3 -m aitomaton              # Python 3.8+, standard library only; git for prepared workspaces
```

## Rolling out
1. Deploy with `AITOMATON_DISPATCH=0`. Label one or two services and connect the webhooks.
2. Read the decisions and the briefs on the page for a few days. Tune `BURST_*`, the chain and the policies.
3. Add tokens, set `AITOMATON_DISPATCH=1`, start with `policy: pr` (not `push`) and small
   `MAX_RUNNING_WORKS` / `MAX_WORKS_PER_DAY`.

## Tests
```bash
python3 -m unittest discover -s tests
```
They run without network or Docker: fake stairway, laya, LLM and GitHub servers, and a local bare git repo
for clone and push.

## Security model

aitomaton turns production errors, which partly carry user input, into instructions for a coding agent that
can push code. Treat it accordingly.

- **What the agent can change is what its token allows.** Use fine-grained tokens per org or per service
  (Contents, Pull requests, Issues), not a personal all-repos token. A missing named credential never falls
  back to a broader one.
- **Prefer `pr` over `push`.** A pull request keeps a human in the loop; `push` is for repos with branch
  protection and CI you trust.
- **Error content is untrusted.** Exception messages and breadcrumbs go into the brief. The brief frames
  them as data about an error, but an attacker who can trigger errors with chosen messages can try to steer
  the agent. Keep the agent's permission mode conservative (`acceptEdits`, not `bypassPermissions`) and the
  tokens narrow.
- **Labels are public within the swarm**, so they only name credentials. Tokens live in env or Docker
  secrets. On disk a token sits only in the experiment's `.aitomaton/` (0600) while the work runs.
- **Webhooks are authenticated**: the Sentry HMAC (`SENTRY_HOOK_SECRET`), the Alertmanager token, and the
  stairway HMAC (`AITOMATON_CALLBACK_SECRET`). Set all three when the port is reachable.
- **Third-party images are never fixed**: an image's source label only becomes the repo when the service
  opts in.
- **Rate limits**: `MAX_RUNNING_WORKS`, `MAX_WORKS_PER_DAY` and `DECISION_COOLDOWN_SECONDS` bound what an error
  storm can trigger, and a cascade fixes only its root.
- **The Docker socket** gives read access to the whole swarm (including service env). aitomaton only reads,
  but whoever controls the container controls the socket: keep it on a trusted manager.
