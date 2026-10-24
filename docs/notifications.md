# Notifications

aitomaton can post to a **Telegram bot** and/or a **Slack-compatible webhook**. Both are optional, and both
can be used at once.

## What is sent

`AITOMATON_NOTIFY_EVENTS` picks the events (default `emergency,fix`):

| event | when |
|---|---|
| `emergency` | an [emergency](emergencies.md) is declared, after each direct action, when the runbook is exhausted, when it is over |
| `fix` | a fix ends: PR opened, pushed, no fix, failed |
| `report` | a GitHub issue is opened |
| `reported` | a person files a new report (through MCP, or a Telegram reporter) |

Nothing is sent while `AITOMATON_DISPATCH=0`. The work's log still says "notification not sent: dispatch is off".
Every work logs which channels were notified, or why a channel failed.

## Telegram

1. In Telegram, ask [@BotFather](https://t.me/BotFather) for `/newbot` and copy the token.
2. Give the token to aitomaton, preferably as a Docker secret:
   ```bash
   printf %s "$TOKEN" | docker secret create aitomaton_telegram_token -
   ```
   and add `aitomaton_telegram_token` to the service's `secrets` in `stack.yml`. Alternatively, set
   `AITOMATON_TELEGRAM_TOKEN` in `.env`.
3. Write to the bot, or add it to a group and post there. Then list the chats it can see:
   ```bash
   docker exec $(docker ps -qf name=aitomaton_aitomaton) python -m aitomaton telegram-chats
   ```
   ```
   42          Ada Lovelace
   -1001234567 ops
   -1001234567/12  ops        (a forum topic)
   ```
4. Set `AITOMATON_TELEGRAM_CHAT` to one or more of them, comma separated. `<chat>/<topic>` posts in a forum
   topic.
5. Check it: `python -m aitomaton notify-test`.

**Per service**: `aitomaton.notify.telegram: "-1009876543"` also sends that service's notifications to its
team's group. The same bot is used, so it must be a member of that group.

The token is part of every Telegram API URL, so aitomaton redacts it from every error it logs or shows.
`status.json` shows only how many chats are configured. For a self-hosted Bot API server, set
`AITOMATON_TELEGRAM_API`.

The same bot can also take reports: see [Reports through Telegram](telegram-intake.md).

## Webhook

`AITOMATON_NOTIFY_URL` receives `{"text": …, "content": …, "username": "aitomaton"}`. That works with Slack,
Mattermost and Rocket.Chat incoming webhooks, and with Discord through the webhook URL with `/slack` appended.
