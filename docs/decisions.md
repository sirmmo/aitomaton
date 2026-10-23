# Decisions

For every issue with new activity, aitomaton decides one **action**:

| action | meaning |
|---|---|
| `ignore` | noise: harmless, expected or too rare |
| `watch` | keep analysing; decide again if it grows |
| `report` | needs a human: infrastructure, configuration, an external dependency, or not enough to go on |
| `fix` | a defect in the service's own code that a coding agent can fix now |

Each tier also gives a **confidence** and a **code-defect probability**. All of them are recorded in the
decision's `trail`, so the page shows why each decision was made.

## Tiers

### rules (always)
Deterministic. Severity = 0.4 × level, +0.25 burst, +0.15 new, +0.2 regression, + up to 0.2 for affected
users, +0.1 rising, +0.1 service-wide burst, +0.05 chronic. Code defect = 0.8 with in-app frames, 0.35 with a
library-only trace, 0.15 without a trace, 0.1 for a Prometheus alert. Then −0.3 when the container is over
90 % of its memory limit, and −0.4 downstream of a cascade. Below 0.3 severity the action is `ignore` (or
`watch` if chronic); below 0.5 it is `watch`; above that, `fix` when there is a repo and the code-defect
estimate is ≥0.5, otherwise `report`.

### laya (System 1)
[laya](https://github.com/convaiinnovations/laya) answers typed questions about a state in one
non-autoregressive forward pass, with calibrated probabilities. aitomaton sends a prose description of the
issue and its analysis and asks two yes/no (*noul*) questions:

- *urgent*: does this need attention now?
- *code_defect*: is the root cause in this service's own code?

The action follows: urgent < 0.3 → `ignore`, < 0.55 → `watch`, otherwise `fix` if code defect ≥ 0.6 and
there is a repo, else `report`. A multiple-choice question was tried first and gave nearly flat
distributions. The yes/no reads are sharp: on test cases, code defect 0.96 for a `KeyError` with in-app frames
vs 0.11 for a full disk, and urgency 0.80 for a burst vs 0.22 for a rare info message. A decision takes 3–5 s
on CPU.

### jev
TypeSafe's JEV on OpenRouter (`JEV_MODEL=typesafe/jev-router`). The router picks a model and reasoning effort
per request. It goes through the same chat-completion path as `llm`, with its own model, URL and key, and
sees what the earlier tiers said.

### llm (System 2)
Any OpenAI-compatible chat model (`LLM_BASE_URL`, `LLM_MODEL`). It gets the same prose state plus the
earlier tiers' answers and returns JSON `{action, confidence, code_defect, rationale}`.

## The chain
With `AITOMATON_DECIDER=chain`, the rules run first, then the tiers of `AITOMATON_CHAIN` in order (default
`laya,jev,llm`; tiers that are not configured are skipped). The first configured tier is always asked. Each
following one is asked only when:

- the stakes are real (a tier said `report` or `fix`), **and**
- the last answer's confidence is below `ESCALATE_BELOW`, **or** it disagrees with the rules about whether to
  fix.

A tier that is down records an `error` step and hands over to the next. A decision is never blocked: at
worst it is the rules' decision. The last answering tier decides.

## Guard rails
After the chain, regardless of which tier decided:

- no repository → no `fix` (becomes `report`)
- code-defect estimate below 0.4 → no `fix`
- the service's [policy](labels.md#policies) caps the action (`applied` in the decision)

## When decisions happen
An issue is decided when it has had events in the last 24 hours (or regressed) and no work is open for it.
After that it is decided again only after `DECISION_COOLDOWN_SECONDS` with new events, or immediately when
it escalates: it starts bursting, regresses, or becomes the root of a cascade.
