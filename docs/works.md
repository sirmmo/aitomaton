# Works

A **work** is what a decision sets in motion: a `fix` (a stairway experiment) or a `report` (a GitHub issue,
or just an entry on the status page).

## States

```
dry-run                          AITOMATON_DISPATCH=0, or stairway not configured: the brief is ready, nothing sent
queued ──▶ dispatched ──▶ running ──▶ done      PR opened, or pushed
                  │          │   └──▶ no-fix    the agent found it is not a code defect
                  │          ├──▶ waiting       the agent asks for a permission (answer it in stairway)
                  │          └──▶ ended         the session stopped without a result
                  └──▶ failed                   stairway, clone, or PR creation failed
reported                         a report kept on the status page (no GitHub token)
```

At most `MAX_RUNNING_WORKS` run at once and `MAX_WORKS_PER_DAY` start per day; the rest wait in `queued`.

## A fix, step by step

1. **Name**: `aitomaton-<service>-<issue>`, e.g. `aitomaton-billing-api-5120`.
2. **Brief**: a self-contained prompt with the repository and branch, how to deliver under the policy, the
   exception and in-app frames, breadcrumbs, release and environment, the analysis in prose and as JSON, and
   the Prometheus metrics. It is visible in the work's *brief* on the page, dry-runs included.
3. **Workspace**: when `STAIRWAY_EXPERIMENTS_ROOT` is set and the service has a git credential, aitomaton
   creates the experiment **without starting it**, clones the repo into `./repo` and checks out
   `aitomaton/fix-<issue>` (see [Git](#git)). Then it starts the agent. Otherwise the agent clones the repo
   itself with the host's `gh`.
4. **Agent**: stairway runs Claude Code, opencode, crush or openclaude in session mode
   (`aitomaton.agent`, `STAIRWAY_AGENT`). It shows up in claude.ai/code like any stairway experiment, so you
   can watch or take over.
5. **Result**: the agent ends with one line:
   ```
   AITOMATON-RESULT: {"status": "branch", "branch": "aitomaton/fix-5120", "title": "…", "body": "…", "summary": "…"}
   ```
   `status` is `branch` (pushed, aitomaton opens the PR), `pr` (it opened one: `url`), `pushed` (policy `push`),
   `no_fix` or `failed`.
6. **Follow-up**: stairway POSTs each finished reply to `/hooks/stairway`, signed with
   `AITOMATON_CALLBACK_SECRET`. Without a secret aitomaton polls `experiment_info` every tick. A `waiting`
   event (a permission prompt) shows on the page.
7. **PR**: for `branch`, aitomaton opens the pull request against `ref` with the service's token, adding a
   link to the error.

## Git

When aitomaton prepares the workspace:

- the token is written to `<experiment>/.aitomaton/git-credentials` (mode 0600, **outside** the clone);
- the clone's credential helper is reset (the host's own helpers are dropped) and pointed at that file, so
  pushes use this service's token only;
- `user.name` / `user.email` come from `aitomaton.git.author` / `AITOMATON_GIT_AUTHOR`;
- the token is never in a remote URL, the brief, a log line or `status.json`, and is masked in error
  messages;
- the credentials file is deleted when the work ends (done, no-fix, failed or ended). The clone stays, for
  inspection.

## Reports
A `report` with a credential and dispatch on opens a GitHub issue labelled `aitomaton` in the service's repo:
the error, the decision with its rationale, and the full analysis in a collapsed block. Without a token it
is kept as a `reported` work on the page.
