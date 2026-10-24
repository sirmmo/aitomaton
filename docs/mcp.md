# MCP server

aitomaton is also an [MCP](https://modelcontextprotocol.io) server. People, and the agents they work with
(Claude Code, Claude Desktop, any MCP client), can use it to:

- **ask what is going on**: emergencies, works in progress, a service's state, an issue's decisions;
- **tell it what no log shows**: "checkout takes 20 s", "the map shows yesterday's tiles", "the site is down
  from outside". aitomaton **investigates** each report against everything else it sees, and decides on it like
  any other issue.

## Setup

```bash
AITOMATON_MCP_TOKEN=marco:<long random>,ci-bot:<another>    # "name:token", comma separated
```

The endpoint is `POST /mcp` (streamable HTTP with JSON responses). It is off without a token. The caller's
name becomes the reporter of its reports, unless the call names someone else.

```bash
claude mcp add --transport http aitomaton https://aitomaton.example.org/mcp \
  --header "Authorization: Bearer <token>"
```

For clients that only speak stdio, use a bridge such as `npx mcp-remote https://…/mcp --header
"Authorization: Bearer <token>"`. claude.ai custom connectors need OAuth, which aitomaton does not implement
(yet).

## Tools

| tool | what it does |
|---|---|
| `status` | emergencies, works in progress, active findings, swarm health (managers, nodes not ready, conditions, advisories), sources |
| `service_status` | one service: repo, policies (both layers), infrastructure outlet, replicas, failing tasks, metrics, open issues, findings, emergencies |
| `list_issues` | issues, filtered by service, architecture, source (`sentry`, `prometheus`, `swarm`, `user`) or title words |
| `get_issue` | an issue with its analysis, investigation, the last decisions with each tier's reasoning, and its works |
| `report_issue` | report a problem: `title`, `description`, optionally `service` or `architecture`, `kind`, `severity`, `reporter` |
| `add_observation` | it happened again, got worse, or there is more to say. Can raise the severity, and reopens a resolved report |
| `resolve_report` | the problem is over |

`kind` is `slowness`, `errors`, `outage`, `data`, `security` or `other`. `severity` is `low`, `medium`,
`high` or `critical`, which map to the levels `info`, `warning`, `error` and `fatal`.

## What happens to a report

1. **It becomes an issue** with source `user` and key `user:<service>:<n>`. Every further report is an
   occurrence, and the distinct reporters are its "users". A report of the same kind on the same service
   within the hour is **merged** into the open one (`merge: false` opens a new one). Several people saying the
   same thing weigh more than one.
2. **It is investigated.** aitomaton looks at:
   - the service's replicas, failing tasks and last task error;
   - whether it was deployed in the last 6 hours;
   - its open errors and alerts with activity in the last hour (with their bursts);
   - active findings on it or its architecture;
   - swarm-wide conditions (a node down);
   - emergencies;
   - Prometheus metrics.
   The result is stored on the issue, returned to the caller, and shown on the page. If it finds anything,
   the report is **corroborated**.
3. **It is decided**, by the same chain as every other issue:
   - The rules add weight for a person's report, more when it is corroborated or when several people report it.
   - The code-defect estimate follows the evidence: high when a related exception has in-app frames, low for an
     outage with nothing else to go on.
   - laya, JEV and the LLM receive the report **quoted as data**, together with the investigation.
   - The service's policy caps the result as usual: a report on a service with `observe` is recorded and
     watched.
4. **The answer** to `report_issue` carries the investigation and, when the loop decides within
   `MCP_DECISION_WAIT_SECONDS` (20 s), the decision. Otherwise `get_issue` has it later.

A **`critical`** report on a service declares an [emergency](emergencies.md). That means the emergency
layer, a report and notifications, but **never direct actions**: nobody's word alone restarts or rolls back a
service.

Example answer:

```json
{
 "key": "user:newsroom_api:1", "merged": false, "reports": 1, "reporters": 1,
 "investigation": {
  "corroborated": true,
  "summary": [
   "newsroom_api runs 0 of 1 replicas.",
   "4 tasks failed in the last hour (task: non-zero exit (1)).",
   "The service was updated 246 minutes ago.",
   "Related swarm issue: newsroom_api: 4 tasks failed in the last hour (crash loop) (4 events in the last hour)."
  ]
 },
 "decision": {"action": "report", "applied": "report", "policy": "report", "decider": "rules",
              "rationale": "severity 0.76, code defect 0.25 (new, reported (slowness), corroborated)"}
}
```

Reports can also come through the Telegram bot, which asks for what is missing before it files them. See
[telegram-intake.md](telegram-intake.md).

## Security

- A token can read everything `status.json` shows (never secrets) and file reports. Give each person or bot
  their own token so reports are attributed.
- Report text ends up in prompts and agent briefs. It is always marked as data, never as instructions. The
  same holds for error messages from production.
- `MCP_REPORTS_PER_HOUR` (60) caps reports and observations from all callers together.
- Reports never trigger direct actions, and they cannot change policies.
