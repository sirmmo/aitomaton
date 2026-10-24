"""Notifications: a Telegram bot and/or a Slack-compatible webhook.

  AITOMATON_TELEGRAM_TOKEN   the bot token from @BotFather (or the Docker secret aitomaton_telegram_token)
  AITOMATON_TELEGRAM_CHAT    chat ids, comma separated; "<chat>/<topic>" posts in a forum topic
  aitomaton.notify.telegram  label: more chats for one service's notifications
  AITOMATON_NOTIFY_URL       Slack-compatible incoming webhook (Slack, Mattermost, Rocket.Chat, Discord's /slack)
  AITOMATON_NOTIFY_EVENTS    which events notify: emergency, fix, report, reported (a person's new report)
                             (default emergency,fix)

The bot token is part of every Telegram API URL, so it is redacted from every error that is logged or shown.
"""
from __future__ import annotations

import os

from .credentials import redact
from .http import request

EVENTS = ("emergency", "fix", "report", "reported")


def telegram_token(cfg):
    if cfg.telegram_token:
        return cfg.telegram_token
    try:
        with open(os.path.join(cfg.secrets_dir, "aitomaton_telegram_token")) as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def parse_chats(value):
    """'-100123, -100456/7' -> [("-100123", None), ("-100456", 7)]"""
    out = []
    for c in (value or "").split(","):
        c = c.strip()
        if not c:
            continue
        chat, _, topic = c.partition("/")
        out.append((chat, int(topic) if topic.isdigit() else None))
    return out


class Notifier:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = telegram_token(cfg)

    def channels(self):
        return {"telegram": len(parse_chats(self.cfg.telegram_chats)) if self.token else 0,
                "webhook": bool(self.cfg.notify_url), "events": self.cfg.notify_events}

    def configured(self):
        return bool((self.token and self.cfg.telegram_chats) or self.cfg.notify_url)

    def send(self, event, text, service=None):
        """Sends to every channel; returns what happened, for the work's log (None when nothing is set up)."""
        if event not in self.cfg.notify_events:
            return None
        chats = parse_chats(self.cfg.telegram_chats) + parse_chats((service or {}).get("notify_telegram"))
        results = []
        if self.token:
            for chat, topic in dict.fromkeys(chats):
                results.append(self._try("telegram %s" % chat, self.telegram, chat, topic, text))
        if self.cfg.notify_url:
            results.append(self._try("webhook", self.webhook, text))
        return "; ".join(results) or None

    def _try(self, name, fn, *args):
        try:
            fn(*args)
            return "notified " + name
        except Exception as e:
            return redact("%s failed: %s" % (name, e), self.token)

    def telegram(self, chat, topic, text):
        payload = {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}
        if topic:
            payload["message_thread_id"] = topic
        request("POST", "%s/bot%s/sendMessage" % (self.cfg.telegram_api, self.token), payload, timeout=15)

    def webhook(self, text):
        request("POST", self.cfg.notify_url, {"text": text, "content": text[:1900], "username": "aitomaton"},
                timeout=15)

    def telegram_chats(self):
        """The chats that recently wrote to the bot (or added it), to find their ids."""
        r = request("GET", "%s/bot%s/getUpdates" % (self.cfg.telegram_api, self.token), timeout=15)
        seen = {}
        for u in (r or {}).get("result") or []:
            m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
            c = m.get("chat") or {}
            if c.get("id") is not None:
                topic = m.get("message_thread_id") if m.get("is_topic_message") else None
                seen["%s%s" % (c["id"], "/%s" % topic if topic else "")] = c.get("title") or c.get("username") or \
                    " ".join(filter(None, (c.get("first_name"), c.get("last_name"))))
        return seen
