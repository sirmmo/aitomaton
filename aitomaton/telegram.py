"""Telegram intake: people report what no log shows by talking to the bot, and the bot manages the report.

The bot does not file a message as it comes. It leads a short conversation that fills in what aitomaton needs
(what is wrong, which service, what kind of problem, how bad), guessing what it can from the text, shows what
it understood, and files the report only when the person confirms. Then it follows the report up in the same
chat: the decision, the fix, the resolution. Replying to one of its messages about a report adds to that report.

Two roles:
  operators  members of the notification chats (AITOMATON_TELEGRAM_CHAT) and AITOMATON_TELEGRAM_OPERATORS.
             They choose among all services, can declare a report critical (an emergency), and see the
             investigation, the decision and the works. /status and /issue are theirs.
  reporters  AITOMATON_TELEGRAM_REPORTERS (user ids, @usernames, or ids of group chats whose members may report),
             or anyone with AITOMATON_TELEGRAM_INTAKE=open. They only see the services that have an
             aitomaton.report.name label, by that name. Severity stops at "high": nobody outside the team can
             page it. Answers tell them the report is recorded, handled, solved; never service names, errors or
             the investigation.

  AITOMATON_TELEGRAM_INTAKE   off (default), on, open
  TELEGRAM_REPORTS_PER_HOUR   reports and additions per reporter (default 5); MCP_REPORTS_PER_HOUR caps everyone

The bot long-polls getUpdates: it needs no public URL, and the bot must have no webhook set.
"""
from __future__ import annotations

import difflib
import logging
import re
import threading
import time

from . import reports
from .credentials import redact
from .http import request
from .notify import parse_chats

log = logging.getLogger("aitomaton.telegram")

CONV_TTL = 1800             # an unfinished report is forgotten after 30 minutes
FOLLOW_DAYS = 7             # a report is followed up in its chat for a week
KIND_WORDS = (              # first match wins; Italian too
    ("security", ("security", "leak", "phishing", "hacked", "breach", "password", "sicurezza", "violat")),
    ("outage", ("down", "unreachable", "not loading", "doesn't load", "does not load", "won't open", "offline",
                "502", "503", "504", "irraggiungibile", "non va", "non si apre", "non carica", "non funziona")),
    ("slowness", ("slow", "lag", "hangs", "hanging", "timeout", "timed out", "takes ages", "forever", "lent",
                  "ci mette")),
    ("data", ("wrong", "stale", "missing", "outdated", "old data", "yesterday", "duplicat", "dati", "sbagliat",
              "mancan", "vecchi")),
    ("errors", ("error", "exception", "500", "crash", "fails", "failed", "failing", "errore", "si blocca")),
)
KIND_LABEL = {"slowness": "Slow", "errors": "Errors", "outage": "Down or unreachable", "data": "Wrong or missing data",
              "security": "Security concern", "other": "Something else"}
SEVERITY_LABEL = {
    "operator": {"low": "Low", "medium": "Medium", "high": "High", "critical": "Critical: declare an emergency"},
    "reporter": {"low": "Minor", "medium": "Annoying, I can work around it", "high": "I can't do what I need"},
}
WORK_STATUS = {"reported": "written on the status page", "dry-run": "prepared (dry run, not sent)",
               "no-fix": "no fix found"}
HELP = {
    "operator": "Tell me what is wrong and I'll file it as a report: aitomaton investigates it and decides on it.\n"
                "/report <what you see>  start a report (or just write to me privately)\n"
                "/status  what is going on\n/issue <n or key>  one issue\n/mine  your open reports\n"
                "/cancel  drop the report you are writing\nReply to my message about a report to add to it.",
    "reporter": "Tell me what isn't working and I'll pass it on to the team.\n"
                "/report <what you see>  start a report (or just write to me privately)\n"
                "/mine  your open reports\n/cancel  drop the report you are writing\n"
                "Reply to my message about a report to add to it.",
}


def guess_kind(text):
    low = (text or "").lower()
    for kind, words in KIND_WORDS:
        if any(re.search(r"(?<![\w])" + re.escape(w), low) for w in words):
            return kind
    return None


def _ids(value):
    return {v.strip().lower() for v in (value or "").split(",") if v.strip()}


def _mentions(term, low):
    return bool(term) and re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(term.lower()), low) is not None


class TelegramBot:
    def __init__(self, engine, api=None):
        self.e, self.cfg = engine, engine.cfg
        self.token = engine.notifier.token
        self.api = api or self._api
        self.operators = _ids(self.cfg.telegram_operators)
        self.reporters = _ids(self.cfg.telegram_reporters)
        self.op_chats = {c for c, _ in parse_chats(self.cfg.telegram_chats)}
        self.convs = {}             # (chat, user) -> the report being written
        self.sent = {}              # user id -> times of their reports, for TELEGRAM_REPORTS_PER_HOUR
        self.seq = 0
        self.lock = threading.RLock()

    # ------------------------------------------------------------ transport
    def _api(self, method, payload, timeout=20):
        try:
            r = request("POST", "%s/bot%s/%s" % (self.cfg.telegram_api, self.token, method), payload, timeout=timeout)
        except Exception as e:      # the token is part of the URL
            raise RuntimeError(redact(str(e), self.token)) from None
        return (r or {}).get("result")

    def run_forever(self):
        offset = self.e.store.get("telegram_offset")
        log.info("telegram intake on (%s)", self.cfg.telegram_intake)
        while True:
            try:
                payload = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
                if offset:
                    payload["offset"] = offset
                ups = self.api("getUpdates", payload, timeout=40) or []
                for u in ups:
                    offset = u["update_id"] + 1
                    try:
                        self.handle(u)
                    except Exception:
                        log.exception("telegram update %s", u.get("update_id"))
                if ups:
                    self.e.store.put("telegram_offset", offset)
                self.follow_up()
            except Exception as ex:
                log.warning("telegram: %s", ex)
                time.sleep(10)

    def say(self, ctx, text, buttons=None, markup=None):
        payload = {"chat_id": ctx["chat"], "text": text[:4000], "disable_web_page_preview": True}
        if ctx.get("thread"):
            payload["message_thread_id"] = ctx["thread"]
        if ctx.get("msg"):
            payload["reply_to_message_id"] = ctx["msg"]
            payload["allow_sending_without_reply"] = True
        if buttons:
            markup = {"inline_keyboard": [[{"text": t[:60], "callback_data": d}] for t, d in buttons]}
        if markup:
            payload["reply_markup"] = markup
        return (self.api("sendMessage", payload) or {}).get("message_id")

    # ------------------------------------------------------------ who
    def role(self, chat, user):
        uid, uname = str(user.get("id")), "@" + (user.get("username") or "").lower()
        cid = str(chat.get("id"))
        if uid in self.operators or uname in self.operators or cid in self.op_chats:
            return "operator"
        if uid in self.reporters or uname in self.reporters or cid in self.reporters or \
                self.cfg.telegram_intake == "open":
            return "reporter"
        return None

    @staticmethod
    def who(user):
        return "tg:%s%s" % (user.get("id"), " @" + user["username"] if user.get("username") else "")

    # ------------------------------------------------------------ updates
    def handle(self, u, now=None):
        now = now or time.time()
        with self.lock:
            if u.get("callback_query"):
                return self._callback(u["callback_query"], now)
            m = u.get("message") or {}
            user, chat = m.get("from") or {}, m.get("chat") or {}
            text = (m.get("text") or m.get("caption") or "").strip()
            if not user or user.get("is_bot") or not text:
                return None
            private = chat.get("type") == "private"
            cmd, arg = None, text
            if text.startswith("/"):
                head, _, arg = text.partition(" ")
                cmd, arg = head[1:].split("@", 1)[0].lower(), arg.strip()
            reply_to = (m.get("reply_to_message") or {}).get("message_id")
            conv = self._conv(chat["id"], user["id"], now)
            answering = conv and conv.get("wait") and (private or (reply_to and reply_to == conv.get("prompt")))
            about = self._issue_of_message(chat["id"], reply_to) if reply_to else None
            if not (private or cmd or answering or about):
                return None         # group chatter: not for the bot
            role = self.role(chat, user)
            ctx = {"chat": chat["id"], "thread": m.get("message_thread_id") if m.get("is_topic_message") else None,
                   "msg": m.get("message_id"), "user": user, "role": role, "private": private}
            if not role:
                log.info("telegram: %s in chat %s is neither operator nor reporter", self.who(user), chat["id"])
                if private or cmd in ("start", "report", "help"):
                    self.say(ctx, "I don't know you yet. To report problems here, ask the team to add your Telegram "
                                  "id %s to AITOMATON_TELEGRAM_REPORTERS." % user["id"])
                return None
            if cmd in ("start", "help"):
                return self.say(ctx, HELP[role])
            if cmd == "cancel":
                self.convs.pop((chat["id"], user["id"]), None)
                return self.say(ctx, "Cancelled.")
            if cmd == "mine":
                return self.say(ctx, self._mine(user))
            if cmd == "status" and role == "operator":
                return self.say(ctx, self._status())
            if cmd == "issue" and role == "operator":
                return self.say(ctx, self._issue_text(arg))
            if cmd == "report":
                return self._start(ctx, arg, now)
            if cmd:
                return self.say(ctx, HELP[role])
            if answering:
                return self._answer(conv, ctx, text, now)
            if about:
                return self._add(ctx, about, text, now)
            return self._start(ctx, text, now)

    def _conv(self, chat, user, now):
        c = self.convs.get((chat, user))
        if c and now - c["at"] > CONV_TTL:
            self.convs.pop((chat, user), None)
            return None
        return c

    # ------------------------------------------------------------ the conversation
    def _start(self, ctx, text, now):
        self.seq += 1
        conv = {"id": self.seq, "chat": ctx["chat"], "user": ctx["user"]["id"], "role": ctx["role"], "at": now,
                "text": None, "target": None, "kind": None, "severity": None, "wait": None, "options": []}
        self.convs[(ctx["chat"], ctx["user"]["id"])] = conv
        if not text:
            return self._ask(conv, ctx, "What is wrong? Say what you see, where, and since when.", wait="text")
        return self._got_text(conv, ctx, text)

    def _got_text(self, conv, ctx, text):
        conv["text"] = text[:reports.MAX_TEXT]
        if conv["target"] is None:
            sure = [t for t, strong in self._targets(conv["role"], text) if strong]
            if len(sure) == 1:
                conv["target"] = sure[0]
        conv["kind"] = conv["kind"] or guess_kind(text)
        return self._next(conv, ctx)

    def _next(self, conv, ctx):
        if conv["target"] is None:
            return self._ask_service(conv, ctx)
        if conv["kind"] is None:
            return self._ask(conv, ctx, "What kind of problem is it?",
                             buttons=[(KIND_LABEL[k], "k|" + k) for k in reports.KINDS])
        if conv["severity"] is None:
            sev = SEVERITY_LABEL[conv["role"]]
            return self._ask(conv, ctx, "How bad is it?", buttons=[(sev[s], "v|" + s) for s in sev])
        warn = "\n\nCritical declares an emergency: people are notified and the emergency layer runs " \
               "(never direct actions, on a report)." if conv["severity"] == "critical" else ""
        return self._ask(conv, ctx, "I'll file this:\n\n%s\n\nService: %s\nKind: %s\nSeverity: %s%s" % (
            conv["text"][:600], conv["target"]["label"], KIND_LABEL[conv["kind"]],
            SEVERITY_LABEL[conv["role"]][conv["severity"]], warn),
            buttons=[("File it", "f|file"), ("Change something", "f|edit"), ("Cancel", "f|cancel")])

    def _ask_service(self, conv, ctx, typed=None):
        cands = [t for t, _ in self._targets(conv["role"], typed or conv["text"], typed is not None)]
        conv["options"] = cands[:8]
        buttons = [(t["label"], "s|%d" % n) for n, t in enumerate(conv["options"])]
        if conv["role"] == "operator":
            buttons.append(("Type the name", "s|type"))
        buttons.append(("Not sure / several", "s|none"))
        if typed is not None:
            q = "No service called %r.%s" % (typed, " Did you mean one of these?" if cands else "")
        elif conv["role"] == "operator":
            q = "Which service?" + ("" if cands else " None is named in the report.")
        else:
            q = "Where do you see it?"
        return self._ask(conv, ctx, q, buttons=buttons)

    def _targets(self, role, text, typed=False):
        """[(target, strong)]: services (or, for reporters, public names) the text names, best first."""
        low = (text or "").lower()
        if role == "reporter":
            names = {}
            for s in sorted(self.e.swarm.services.values(), key=lambda s: s["name"]):
                if s.get("report_name"):
                    names.setdefault(s["report_name"], []).append(s)
            out = []
            for label, svcs in sorted(names.items()):
                hit = _mentions(label, low)
                one = len(svcs) == 1 or len({s["architecture"] for s in svcs}) > 1
                out.append(({"label": label, "service": svcs[0]["name"] if one else None,
                             "architecture": svcs[0]["architecture"]}, hit))
            out.sort(key=lambda t: not t[1])
            return out
        scored = []
        for s in self.e.swarm.services.values():
            terms = {s["name"], s.get("report_name")} | ({s["short"]} if len(s["short"]) >= 4 else set())
            strong = any(_mentions(t, low) for t in terms if t)
            weak = strong or any(_mentions(t, low) for t in (s["short"], s.get("stack"), s["architecture"]) if t)
            if weak:
                scored.append((not strong, s["name"], {"label": s["name"], "service": s["name"]}, strong))
        if typed and not scored:
            known = {n: s for s in self.e.swarm.services.values() for n in (s["name"], s["short"])}
            for n in difflib.get_close_matches(text, list(known), 6, 0.5):
                scored.append((True, known[n]["name"], {"label": known[n]["name"], "service": known[n]["name"]}, False))
        seen, out = set(), []
        for _, name, t, strong in sorted(scored, key=lambda x: x[:2]):
            if name not in seen:
                seen.add(name)
                out.append((t, strong))
        return out

    def _ask(self, conv, ctx, question, wait=None, buttons=None):
        markup = None
        if wait and not ctx.get("private"):     # in groups the answer must be a reply, or the bot won't see it
            markup = {"force_reply": True, "selective": True}
        conv.update(wait=wait, question=question)
        conv["prompt"] = self.say(ctx, question, [(t, "c|%d|%s" % (conv["id"], d)) for t, d in buttons or []],
                                  markup)
        return conv["prompt"]

    def _answer(self, conv, ctx, text, now):
        conv["at"] = now
        wait, conv["wait"] = conv["wait"], None
        if wait == "text":
            return self._got_text(conv, ctx, text)
        if wait == "service":
            s = self.e.swarm.get(text.strip())
            if s:
                conv["target"] = {"label": s["name"], "service": s["name"]}
                return self._next(conv, ctx)
            return self._ask_service(conv, ctx, typed=text.strip())
        if wait == "add":
            self.convs.pop((ctx["chat"], ctx["user"]["id"]), None)
            return self._add(ctx, conv["about"], text, now)
        return None

    def _chose(self, conv, ctx, field, val, now):
        conv["at"] = now
        if field == "s":
            if val == "type":
                return self._ask(conv, ctx, "Type the service name.", wait="service")
            conv["target"] = {"label": "not sure"} if val == "none" else conv["options"][int(val)]
        elif field == "k" and val in reports.KINDS:
            conv["kind"] = val
        elif field == "v" and val in SEVERITY_LABEL[conv["role"]]:
            conv["severity"] = val
        elif field == "e":
            if val == "text":
                conv["text"] = None
                return self._ask(conv, ctx, "Write it again: what is wrong?", wait="text")
            conv[{"s": "target", "k": "kind", "v": "severity"}[val]] = None
        elif field == "f":
            if val == "cancel":
                self.convs.pop((ctx["chat"], ctx["user"]["id"]), None)
                return self.say(ctx, "Cancelled.")
            if val == "edit":
                return self._ask(conv, ctx, "What do you want to change?", buttons=[
                    ("The description", "e|text"), ("Service", "e|s"), ("Kind", "e|k"), ("Severity", "e|v")])
            if val == "file":
                return self._file(conv, ctx, now)
        return self._next(conv, ctx)

    # ------------------------------------------------------------ filing and adding
    def _rate(self, ctx, now):
        reports.check_rate(self.e, now)
        if ctx["role"] == "reporter":
            uid = ctx["user"]["id"]
            times = [t for t in self.sent.get(uid, []) if now - t < 3600]
            if len(times) >= self.cfg.telegram_reports_per_hour:
                raise ValueError("You have sent a lot in the last hour; please try again later.")
            self.sent[uid] = times + [now]

    def _file(self, conv, ctx, now):
        try:
            self._rate(ctx, now)
        except ValueError as ex:
            return self.say(ctx, str(ex))
        self.convs.pop((ctx["chat"], ctx["user"]["id"]), None)
        t, text = conv["target"], conv["text"]
        title = text.splitlines()[0][:120]
        i = self.e.report_issue(title, text, t.get("service"), t.get("architecture"), conv["kind"], conv["severity"],
                                self.who(ctx["user"]), origin={"via": "telegram", "role": ctx["role"]}, now=now)
        n = i["external_id"]
        if ctx["role"] == "operator":
            inv = (i["detail"] or {}).get("investigation") or {}
            head = "Filed %s%s." % (i["key"], " (merged: %d reports from %d people)" % (i["total"], i["users"])
                                    if i.get("merged") else "")
            msg = "%s\n%s\n\nI'll post the decision here." % (head, "\n".join(
                "- " + s for s in inv.get("summary") or []))
        else:
            msg = "Thanks, your report is recorded as #%s.%s I'll write here when there is news." % (
                n, " %d people reported this; yours has been added." % i["users"] if i.get("merged") else "")
        mid = self.say(ctx, msg, self._issue_buttons(n, ctx["role"]))
        self._track(ctx, i, mid, now)
        return mid

    def _add(self, ctx, n, text, now):
        i = self._issue(n)
        if not i:
            return self.say(ctx, "I can't find that report any more.")
        try:
            self._rate(ctx, now)
        except ValueError as ex:
            return self.say(ctx, str(ex))
        i = self.e.add_observation(i["key"], text, self.who(ctx["user"]), origin={"via": "telegram",
                                                                                  "role": ctx["role"]}, now=now)
        mid = self.say(ctx, "Added to #%s (%d reports from %d people)." % (n, i["total"], i["users"]),
                       self._issue_buttons(n, ctx["role"]))
        self._track(ctx, i, mid, now)
        return mid

    def _issue_action(self, ctx, n, what, now):
        i = self._issue(n)
        if not i:
            return self.say(ctx, "I can't find that report any more.")
        if what == "again":
            return self._add(ctx, n, "It happened again.", now)
        if what == "more":
            self.seq += 1
            conv = {"id": self.seq, "chat": ctx["chat"], "user": ctx["user"]["id"], "role": ctx["role"], "at": now,
                    "about": n, "options": []}
            self.convs[(ctx["chat"], ctx["user"]["id"])] = conv
            return self._ask(conv, ctx, "What else should I add to #%s?" % n, wait="add")
        if what == "show" and ctx["role"] == "operator":
            return self.say(ctx, self._issue_text(i["key"]))
        if what == "done":
            me = "tg:%s" % ctx["user"]["id"]
            said = [r for r in (i["detail"] or {}).get("reports") or [] if not (r.get("text") or "").startswith(
                "resolved:")]
            mine_only = all((r.get("reporter") or "").split(" ")[0] == me for r in said)
            if ctx["role"] == "operator" or mine_only:
                self.e.resolve_report(i["key"], "via Telegram", self.who(ctx["user"]))
                return self.say(ctx, "#%s marked solved." % n)
            return self._add(ctx, n, "Solved for me.", now)
        return None

    @staticmethod
    def _issue_buttons(n, role):
        if role == "operator":
            return [("Happened again", "i|%s|again" % n), ("Add details", "i|%s|more" % n),
                    ("Show", "i|%s|show" % n), ("Resolved", "i|%s|done" % n)]
        return [("Still happening", "i|%s|again" % n), ("Add details", "i|%s|more" % n),
                ("Solved for me", "i|%s|done" % n)]

    def _callback(self, q, now):
        user, m = q.get("from") or {}, q.get("message") or {}
        chat = m.get("chat") or {}
        parts = (q.get("data") or "").split("|")
        role = self.role(chat, user)
        ctx = {"chat": chat.get("id"), "thread": m.get("message_thread_id") if m.get("is_topic_message") else None,
               "msg": m.get("message_id"), "user": user, "role": role, "private": chat.get("type") == "private"}

        def answer(text=None):
            self.api("answerCallbackQuery", dict({"callback_query_id": q.get("id")},
                                                 **({"text": text} if text else {})))

        if not role:
            return answer("You can't report here.")
        if parts[0] == "c" and len(parts) == 4:
            conv = self._conv(chat.get("id"), user.get("id"), now)
            if not conv or str(conv["id"]) != parts[1]:
                return answer("Not your report, or it expired. /report starts a new one.")
            answer()
            self._settle(ctx, conv, m)
            return self._chose(conv, ctx, parts[2], parts[3], now)
        if parts[0] == "i" and len(parts) == 3:
            answer()
            return self._issue_action(ctx, parts[1], parts[2], now)
        return answer()

    def _settle(self, ctx, conv, m):
        """The question that was answered loses its buttons, so the chat reads as a record."""
        try:
            self.api("editMessageReplyMarkup", {"chat_id": ctx["chat"], "message_id": m.get("message_id"),
                                                "reply_markup": {"inline_keyboard": []}})
        except Exception as ex:
            log.debug("telegram: %s", ex)

    # ------------------------------------------------------------ follow-ups
    def _track(self, ctx, i, mid, now):
        msgs = self.e.store.get("telegram_messages", {})
        if mid:
            msgs["%s:%s" % (ctx["chat"], mid)] = i["external_id"]
            self.e.store.put("telegram_messages", dict(list(msgs.items())[-1000:]))
        fl = [f for f in self.e.store.get("telegram_followups", [])
              if not (f["key"] == i["key"] and f["chat"] == ctx["chat"])]
        fl.append({"key": i["key"], "n": i["external_id"], "chat": ctx["chat"], "thread": ctx.get("thread"),
                   "msg": mid, "role": ctx["role"], "since": now, "decided": False, "works": [],
                   "until": now + FOLLOW_DAYS * 86400})
        self.e.store.put("telegram_followups", fl[-500:])

    def _issue_of_message(self, chat, mid):
        return self.e.store.get("telegram_messages", {}).get("%s:%s" % (chat, mid))

    def _issue(self, n):
        r = self.e.store.q("SELECT * FROM issues WHERE source='user' AND external_id=?", (str(n),))
        return r[0] if r else None

    def follow_up(self, now=None):
        """Tell each chat what became of its reports: the decision, a fix, the resolution."""
        now = now or time.time()
        with self.lock:
            keep = []
            for f in self.e.store.get("telegram_followups", []):
                i = self.e.store.issue(f["key"])
                if not i or now > f["until"]:
                    continue
                ctx = {"chat": f["chat"], "thread": f.get("thread"), "msg": f.get("msg")}
                op = f["role"] == "operator"
                try:
                    if i["status"] == "resolved":
                        self.say(ctx, "%s is solved." % (i["key"] if op else "Report #%s" % f["n"]))
                        continue
                    d = self.e.store.last_decision(f["key"])
                    if not f["decided"] and d and d["at"] >= f["since"] - 1:
                        f["decided"] = True
                        text = self._decision_text(f, i, d)
                        if text:
                            self.say(ctx, text)
                    for w in self.e.store.q("SELECT * FROM works WHERE key=? AND created>=? AND kind IN "
                                            "('fix','report') AND status IN ('done','no-fix','failed','reported',"
                                            "'dry-run') ORDER BY id", (f["key"], f["since"] - 1)):
                        if w["id"] in f["works"]:
                            continue
                        f["works"].append(w["id"])
                        text = self._work_text(f, w)
                        if text:
                            self.say(ctx, text)
                except Exception as ex:
                    log.warning("telegram follow-up %s: %s", f["key"], ex)
                keep.append(f)
            self.e.store.put("telegram_followups", keep)

    @staticmethod
    def _decision_text(f, i, d):
        if f["role"] == "operator":
            return "Decision on %s: %s, applied %s (policy %s, %s).\n%s" % (
                i["key"], d["action"], d["applied"], d["policy"], d["decider"], d["rationale"] or "")
        text = {"fix": "Report #%s: a fix is being prepared.", "report": "Report #%s has been passed on to the team.",
                "act": "Report #%s: the team has been alerted."}.get(d["applied"])
        return text % f["n"] if text else None

    @staticmethod
    def _work_text(f, w):
        if f["role"] == "operator":
            url = w["result_url"] or w["url"]
            return "%s for %s: %s%s" % (w["kind"].capitalize(), f["key"], WORK_STATUS.get(w["status"], w["status"]),
                                        "\n" + url if url else "")
        if w["kind"] == "fix" and w["status"] == "done":
            return "Report #%s: a fix has been made. It may take a little while to reach you." % f["n"]
        return None

    # ------------------------------------------------------------ texts
    def _mine(self, user):
        me = "tg:%s" % user.get("id")
        rows = [i for i in self.e.store.q("SELECT * FROM issues WHERE source='user' AND status!='resolved' "
                                          "ORDER BY last_seen DESC LIMIT 200")
                if any((r.get("reporter") or "").split(" ")[0] == me for r in (i["detail"] or {}).get("reports") or [])]
        if not rows:
            return "You have no open reports."
        return "Your open reports:\n" + "\n".join("#%s %s" % (i["external_id"], i["title"]) for i in rows[:20])

    def _status(self):
        st = self.e.status()
        s, inf = st["summary"], st["infrastructure"]
        lines = ["%d open issues, %d emergencies, %d works in progress, %d decisions in 24 h." % (
            s["issues"], s["emergencies"], s["in_progress"], s["decisions_24h"])]
        lines += ["EMERGENCY %s: %s" % (e.get("service"), e["reason"]) for e in st["emergencies"]]
        down = [n["hostname"] for n in inf.get("nodes") or [] if n.get("state") != "ready"]
        if down:
            lines.append("Nodes not ready: " + ", ".join(down))
        lines += ["Swarm: " + c["title"] for c in (inf.get("conditions") or [])[:8]]
        user = [i for i in st["issues"] if i["source"] == "user" and i["status"] != "resolved"]
        if user:
            lines.append("Open reports:")
            lines += ["#%s %s (%s)" % (i["key"].rsplit(":", 1)[-1], i["title"], i["service"]) for i in user[:10]]
        if st["tick_error"]:
            lines.append("Last tick failed: " + st["tick_error"])
        return "\n".join(lines)

    def _issue_text(self, ref):
        ref = (ref or "").strip().lstrip("#")
        i = self._issue(ref) if ref.isdigit() else self.e.store.issue(ref)
        if not i:
            return "No issue %s. /status lists the open reports." % (ref or "given")
        inv = (i["detail"] or {}).get("investigation") or {}
        d = self.e.store.last_decision(i["key"])
        lines = ["%s [%s, %s]" % (i["key"], i["status"], i["level"]), i["title"],
                 "Service: %s. %d reports from %d people." % (i["service"], i["total"] or 0, i["users"] or 0)]
        lines += ["- " + x for x in inv.get("summary") or []]
        if d:
            lines.append("Decision: %s, applied %s (%s): %s" % (
                d["action"], d["applied"], d["decider"], d["rationale"]))
        return "\n".join(lines)
