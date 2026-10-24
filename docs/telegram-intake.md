# Reports through Telegram

The [notification bot](notifications.md) can also take reports: people tell it what is wrong (slowness, odd
behaviour, wrong data, a site down) and aitomaton handles each one like a report through the [MCP server](mcp.md).
It is investigated, merged with reports of the same problem, and decided on.

The bot **manages** the report instead of filing a message as it comes:

1. **It asks what is missing.** It needs to know what is wrong, which service is affected, what kind of problem it
   is and how bad it is. Whatever it can guess from the text, it doesn't ask. A service named in the message, or
   words like "slow", "down", "wrong" or "error" (and their Italian equivalents: "lento", "non va", "sbagliato",
   "errore"), fill those fields in. Everything else is a tap on a button.
2. **It shows what it understood** and files the report only when the person taps *File it*. *Change something*
   edits any field, and *Cancel* drops the report.
3. **It follows the report up in the same chat**: the decision, the fix or report, and the resolution. Replying
   to its message about a report adds to that report. The buttons under that message cover *happened again*,
   *add details* and *solved*.

```
Ada:  shop_api checkout is very slow since 10:00
bot:  How bad is it?                [Low] [Medium] [High] [Critical: declare an emergency]
Ada:  (High)
bot:  I'll file this:
      shop_api checkout is very slow since 10:00
      Service: shop_api   Kind: Slow   Severity: High
                                    [File it] [Change something] [Cancel]
Ada:  (File it)
bot:  Filed user:shop_api:1.
      - shop_api runs 1 of 2 replicas.
      - Related sentry issue bursting: TimeoutError in cart (41 events in the last hour).
      I'll post the decision here.  [Happened again] [Add details] [Show] [Resolved]
bot:  Decision on user:shop_api:1: fix, applied pr (policy pr, rules). severity 0.81, code defect 0.7 …
```

## Two roles

| | operators | reporters |
|---|---|---|
| who | members of the notification chats (`AITOMATON_TELEGRAM_CHAT`), plus `AITOMATON_TELEGRAM_OPERATORS` | `AITOMATON_TELEGRAM_REPORTERS`, or anyone with `AITOMATON_TELEGRAM_INTAKE=open` |
| services offered | all of them, by name. A typed name gets "did you mean" suggestions | only services with an `aitomaton.report.name` label, under that name, plus *Not sure* |
| severity | low to **critical**, which declares an [emergency](emergencies.md) | up to *"I can't do what I need"* (high). They cannot page anyone |
| answers | the investigation, the decision with its reasoning, the works and their links | "recorded as #12", "passed on to the team", "a fix has been made", "solved". Never service names, errors or the investigation |
| commands | `/report`, `/status`, `/issue <n or key>`, `/mine`, `/cancel` | `/report`, `/mine`, `/cancel` |
| *solved* button | resolves the report | resolves it when they are its only reporter. Otherwise it records "solved for me" |
| limits | `MCP_REPORTS_PER_HOUR`, for everyone together | the same, plus `TELEGRAM_REPORTS_PER_HOUR` (5) each |

A reporter could be a customer in a support group, a colleague outside the team or a beta tester. What they write
is quoted to the deciders as data, like every report. Each report records that it came from Telegram, with the
person's role and their id (`tg:<id> @username`).

## Setup

```bash
AITOMATON_TELEGRAM_INTAKE=on                 # off (default) | on | open
AITOMATON_TELEGRAM_OPERATORS=@ada,1234567    # besides the members of AITOMATON_TELEGRAM_CHAT
AITOMATON_TELEGRAM_REPORTERS=-1009876543,98765   # people, or groups whose members may report
AITOMATON_NOTIFY_EVENTS=emergency,fix,reported   # "reported": operators hear of new reports
```

```yaml
labels:                                       # what reporters may choose from
  aitomaton.report.name: "Shop"
```

When several services share one `aitomaton.report.name` within one architecture (the shop's web, API and
workers are all "Shop"), the report goes to the architecture and the investigation looks at all of them.

- **Usernames can change hands**, so use numeric ids for anything that matters. When someone the bot doesn't
  know writes to it, it answers with their id ("ask the team to add your Telegram id 98765"), and it logs them
  too.
- **In groups**, the bot only reacts to `/report`, to replies to its own messages and to its buttons. Everything
  else in the group is ignored. Telegram's privacy mode can stay on.
- **One bot does both jobs.** With the intake on, aitomaton long-polls `getUpdates`, so it needs no public URL.
  Telegram then refuses a webhook on the bot. While the intake is running, `python -m aitomaton telegram-chats`
  sees nothing, because the running bot takes the updates first. Look in aitomaton's log for the chat ids instead.
- `MCP_REPORTS_PER_HOUR` caps every report together, whether it comes through MCP or Telegram.
- A report from Telegram never triggers a direct action, not even a critical one. That holds for every report.
- An unfinished conversation is forgotten after 30 minutes. A filed report is followed up for 7 days.
