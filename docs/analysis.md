# Analysis

Every input is reduced to the same thing: **occurrences in minute buckets** per issue.

| input | becomes |
|---|---|
| Sentry/urgentry `event_alert` / `error` webhook | one occurrence at the event's time, plus the event's exception, in-app frames, tags and breadcrumbs |
| Sentry/urgentry `issue` webhook | issue metadata. `unresolved` marks a regression |
| poll of `/api/0/projects/{org}/{project}/issues/` | `count` deltas between polls, at `lastSeen`. The first sight seeds history from `stats` (14 daily + 24 hourly buckets) |
| Alertmanager webhook | issue `prom:<alertname>:<service>`, one occurrence per firing notification |

Issues are tied to a service (project label, or name match) and through it to an architecture. Every tick
(`AITOMATON_TICK_SECONDS`, or right after a webhook) the analysis recomputes everything from the buckets.

## Three horizons

### Single error
- **new**: first seen within `NEW_ISSUE_SECONDS`
- **regression**: it had been resolved and came back
- **level**: fatal 1.0 · error 0.7 · warning 0.4 · info 0.15
- **stack trace**: whether there is one and how many frames are *in app*. This is the main evidence that
  the code is at fault

### Bursts

```
window   = events in the last BURST_WINDOW_SECONDS
expected = events in the baseline window × window length / baseline length
burst    = window ≥ BURST_MIN_EVENTS  and  window / max(expected, 0.2) ≥ BURST_RATIO
```

The same test runs per issue and per service (all its issues together). An error that has happened 3 times
an hour for weeks is not a burst at 3 per hour; a brand-new one at 40 in five minutes is.

### Long term
Daily counts over `LONG_TERM_DAYS`:
- **trend**: least-squares slope divided by the mean, per day, over **complete** days only (today is partial,
  and a burst today is a burst, not a trend). **rising** when it is over 10 % a day on at least 3 active days
- **chronic**: present on at least half the days

## Three scopes

| scope | findings |
|---|---|
| issue | `new`, `regression`, `burst`, `rising`, `chronic` |
| service | `burst` (all issues), `task_failures` (≥3 failed or rejected swarm tasks in the last hour, with the last task error, e.g. OOMKilled) |
| architecture | `cascade` |

### Cascades
When two or more services of one architecture start bursting within `CASCADE_WINDOW_SECONDS`, it is
probably one cause showing up in several places. The **root** is chosen as:

1. the most upstream service: depended on by another bursting member (`aitomaton.depends`) and not itself
   depending on one;
2. otherwise any depended-on member;
3. otherwise the one whose burst started first.

Every issue gets `cascade` and `cascade_root` flags. The decision tiers then lower the "code defect" estimate
for downstream services: the front end timing out because the database is down needs no front-end fix.

## Findings
Findings are stored with `first_at` / `last_at` and an `active` flag, recomputed each tick. A finding that
has been quiet for over an hour starts a new episode when it comes back. They appear under *Active findings*
on the page and in `findings` in [status.json](status-json.md).
