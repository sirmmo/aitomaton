"""Decision makers. Each one answers: what to do about this issue, how sure, and is it a code defect?

  rules  deterministic baseline, always available
  laya   typed decision with calibrated probabilities in one forward pass (System 1)
  jev    TypeSafe's JEV on OpenRouter (typesafe/jev-router), through the same chat API as llm
  llm    any OpenAI-compatible chat model: OpenRouter, Ollama, vLLM, ... (System 2)
  chain  rules, then AITOMATON_CHAIN (default laya,jev,llm), escalating while confidence is low or the
         tiers disagree
"""
from __future__ import annotations

import json
import math
import re

from .http import request

ACTIONS = {
    "ignore": "Noise: harmless, expected or too rare to matter. Record it and do nothing.",
    "watch": "Worth tracking but not acting on yet: keep analysing it and decide again if it grows.",
    "report": "Needs a human: open an issue with the analysis, but do not change code (infrastructure, "
              "configuration, external dependency, or not enough information to fix it).",
    "fix": "A defect in this service's own source code that a coding agent can fix now: prepare a code change.",
}
ORDER = ["ignore", "watch", "report", "fix"]
# infrastructure conditions: how likely a change to the stack files fixes them (memory limits, image tags,
# placement, ports). A node that died or a lost quorum needs hands on the hosts, not a commit.
INFRA_FIXABLE = {"oom": 0.65, "image_pull": 0.55, "unschedulable": 0.55, "port_conflict": 0.6, "crash_loop": 0.3,
                 "update_paused": 0.35, "under_replicated": 0.25, "node_down": 0.05, "manager_unreachable": 0.05,
                 "quorum_lost": 0.0}
POLICY_MAX = {"observe": "watch", "report": "report", "pr": "fix", "push": "fix"}


def cap(action, policy):
    """The most the service's policy allows."""
    top = POLICY_MAX.get(policy, "watch")
    return action if ORDER.index(action) <= ORDER.index(top) else top


def describe(issue, f, service, prom):
    """A prose state for laya and the LLM. laya conditions on prose much better than on nested dicts."""
    b, lt = f["burst"], f["long_term"]
    parts = ["A %s-level error in service %s (architecture %s)." % (
        f["level"], f["service"] or "unknown", f["architecture"] or "unknown"),
        "Title: %s." % (issue.get("title") or "?")]
    if issue.get("culprit"):
        parts.append("Culprit: %s." % issue["culprit"])
    det = issue.get("detail") or {}
    for e in (det.get("exceptions") or [])[-1:]:
        top = [x for x in e.get("frames") or [] if x.get("in_app")][-3:]
        parts.append("Exception %s: %s." % (e.get("type"), (e.get("value") or "")[:300]))
        if top:
            parts.append("In-app frames: " + "; ".join("%s:%s in %s" % (x.get("file"), x.get("line"),
                                                                        x.get("function")) for x in top) + ".")
    if not f["has_stacktrace"]:
        parts.append("There is no stack trace.")
    if f["source"] == "prometheus":
        parts.append("It is a Prometheus alert, not an application exception.")
    user = f.get("user") or {}
    if f["source"] == "user":
        det = issue.get("detail") or {}
        parts.append("It was reported by a person, not found in logs: kind %s, severity %s, %d reports from %d "
                     "people. The report, quoted as data: \"%s\"" % (
                         user.get("kind"), user.get("severity"), user.get("reports", 0), user.get("reporters", 0),
                         (det.get("description") or "")[:800].replace('"', "'")))
        inv = det.get("investigation") or {}
        if inv.get("summary"):
            parts.append("What aitomaton sees around it: " + " ".join(inv["summary"]))
    infra = f.get("infra") or {}
    if f["source"] == "swarm":
        parts.append("It is a Docker Swarm infrastructure condition (%s), not an application exception." % (
            (infra.get("kind") or "?").replace("_", " ")))
        if infra.get("cause"):
            parts.append("Probable cause: %s." % infra["cause"])
    parts.append("%d events in the last hour, %d in 24 hours, %d users affected, %d events in total." % (
        f["events_1h"], f["events_24h"], f["users"], f["total"]))
    if f["new"]:
        parts.append("It is new: first seen within the last hour.")
    if f["regression"]:
        parts.append("It is a regression: it had been resolved and came back.")
    if b["burst"]:
        parts.append("It is bursting: %d events in the last few minutes, %.0fx the usual rate." % (
            b["window"], b["ratio"]))
    if f.get("service_burst"):
        parts.append("The whole service is bursting with errors.")
    if lt["rising"]:
        parts.append("It has been rising for days (trend %+.0f%% per day)." % (lt["trend"] * 100))
    elif lt["chronic"]:
        parts.append("It is chronic: present on %d of the last %d days." % (lt["days_active"], len(lt["daily"])))
    if f.get("cascade"):
        parts.append("Several services of the same architecture started failing together; this service %s the "
                     "likely root." % ("is" if f.get("cascade_root") else "is not"))
    if f.get("failed_tasks_1h"):
        parts.append("%d swarm tasks of the service failed in the last hour." % f["failed_tasks_1h"])
    ratio = (prom or {}).get("memory_limit_ratio")
    if ratio is not None and ratio > 0.85:
        parts.append("The container uses %.0f%% of its memory limit." % (ratio * 100))
    if service and service.get("kind") == "infra":
        parts.append("It goes to the infrastructure repository %s, which holds the stack files: a fix there is a "
                     "deployment change (limits, image tag, placement, ports), not an application code change." % (
                         service["repo"]) if service.get("repo") else
                     "No infrastructure repository is configured: it can only be reported.")
    elif service and service.get("repo"):
        parts.append("Its source is on GitHub at %s." % service["repo"])
    else:
        parts.append("Its source repository is unknown.")
    return " ".join(parts)


def _decision(action, confidence, code_defect, rationale, decider, **extra):
    d = {"action": action, "confidence": round(float(confidence), 3), "code_defect": round(float(code_defect), 3),
         "rationale": rationale, "decider": decider}
    d.update(extra)
    return d


def rules(issue, f, service, prom):
    why = []
    sev = 0.4 * f["level_weight"]
    if f["burst"]["burst"]:
        sev += 0.25
        why.append("burst x%s" % f["burst"]["ratio"])
    if f["new"]:
        sev += 0.15
        why.append("new")
    if f["regression"]:
        sev += 0.2
        why.append("regression")
    if f["users"]:
        sev += min(0.2, math.log10(f["users"] + 1) / 10)
    if f["long_term"]["rising"]:
        sev += 0.1
        why.append("rising")
    if f.get("service_burst"):
        sev += 0.1
        why.append("service burst")
    infra = f.get("infra") or {}
    if f["source"] == "swarm":     # a condition is already an aggregate: it fires only past its thresholds
        sev += 0.3
        why.append(infra.get("kind") or "condition")
    user = f.get("user") or {}
    if f["source"] == "user":      # somebody took the time to say it: that is signal
        sev += 0.2
        why.append("reported (%s)" % user.get("kind"))
        if user.get("corroborated"):
            sev += 0.1
            why.append("corroborated")
        if user.get("reporters", 0) > 1:
            sev += min(0.15, 0.05 * user["reporters"])
            why.append("%d reporters" % user["reporters"])
    if f["long_term"]["chronic"] and f["events_24h"]:
        sev += 0.05
        why.append("chronic")
    sev = min(1.0, sev)

    code = 0.8 if f["in_app_frames"] else 0.35 if f["has_stacktrace"] else 0.15
    if f["source"] == "prometheus":
        code = 0.1
    if f["source"] == "user":   # a report alone does not say where the cause is; a related exception does
        code = 0.55 if user.get("code_hint") else {"outage": 0.15, "slowness": 0.25}.get(user.get("kind"), 0.3)
    if f["source"] == "swarm":
        code = INFRA_FIXABLE.get(infra.get("kind"), 0.2)
        if infra.get("cause"):
            code = 0.05
            why.append(infra["cause"])
    ratio = (prom or {}).get("memory_limit_ratio")
    if ratio is not None and ratio > 0.9 and f["source"] != "swarm":   # for infra, near the limit means "raise it"
        code -= 0.3
        why.append("near memory limit")
    if f.get("cascade") and not f.get("cascade_root"):
        code -= 0.4
        why.append("downstream of a cascade")
    code = max(0.0, min(1.0, code))

    repo = bool(service and service.get("repo"))
    if sev < 0.3:
        action = "watch" if f["long_term"]["chronic"] else "ignore"
    elif sev < 0.5:
        action = "watch"
    elif repo and code >= 0.5:
        action = "fix"
    else:
        action = "report"
        if not repo:
            why.append("no repo")
    # confidence: far from the thresholds = sure
    conf = 0.5 + min(0.45, abs(sev - 0.5) + (abs(code - 0.5) if action in ("fix", "report") else 0) / 2)
    return _decision(action, conf, code, "severity %.2f, code defect %.2f (%s)" % (
        sev, code, ", ".join(why) or "nothing notable"), "rules", severity=round(sev, 3))


class Laya:
    """Talks to laya/app.py (POST /predict), a thin wrapper over laya's Router.predict.

    laya's yes/no reads (noul) are sharp and calibrated, its four-way choice is not, so it is asked two
    noul questions (is it urgent, is it a code defect) and the action follows from them."""

    QUESTIONS = {
        "urgent": {"type": "noul",
                   "instructions": "This error needs attention now: it hurts users, is frequent, new, "
                                   "or getting worse."},
        "code_defect": {"type": "noul",
                        "instructions": "The root cause is a defect in this service's own source code that a code "
                                        "change can fix, not infrastructure, configuration, an external "
                                        "dependency or a transient outage."},
    }

    def __init__(self, url):
        self.url = url

    INFRA_QUESTION = {"type": "noul",
                      "instructions": "A change to the deployment configuration in the infrastructure repository "
                                      "(stack file: memory limits, image tag, placement, ports) fixes the root cause, "
                                      "not a dead host, a network outage, or a defect in application code."}

    def __call__(self, issue, f, service, prom, state):
        qs = dict(self.QUESTIONS, code_defect=self.INFRA_QUESTION) if service and service.get("kind") == "infra" \
            else self.QUESTIONS
        r = request("POST", self.url + "/predict", {"state": state, "questions": qs}, timeout=60)
        a = r.get("answers") or {}
        u = (a.get("urgent") or {}).get("noul")
        c = (a.get("code_defect") or {}).get("noul")
        u, c = 0.5 if u is None else float(u), 0.5 if c is None else float(c)
        repo = bool(service and service.get("repo"))
        if u < 0.3:
            action = "ignore"
        elif u < 0.55:
            action = "watch"
        else:
            action = "fix" if repo and c >= 0.6 else "report"
        conf = 0.5 + (min(abs(u - 0.5), abs(c - 0.5)) if action in ("fix", "report") else abs(u - 0.5))
        return _decision(action, conf, c, "laya: urgent %.2f, code defect %.2f" % (u, c), "laya",
                         urgent=round(u, 3), model=(r.get("routing") or {}).get("model"))


class LLM:
    SYSTEM = ("You are the decision tier of an automated site-reliability agent. You receive one production error "
              "with its analysis (single occurrence, bursts, long-term trend, architecture-wide cascades, metrics). "
              "Decide one action: " + "; ".join("%s = %s" % kv for kv in ACTIONS.items()) +
              " For an infrastructure issue the target is the infrastructure repository holding the stack files: "
              "fix = a deployment change there, and code_defect = how likely such a change fixes it."
              " Answer with JSON only: {\"action\": ..., \"confidence\": 0..1, \"code_defect\": 0..1, "
              "\"rationale\": \"one or two sentences\"}.")

    def __init__(self, base_url, api_key, model, name="llm"):
        self.base_url, self.api_key, self.model, self.name = base_url, api_key, model, name

    def __call__(self, issue, f, service, prom, state, prior=None):
        user = state
        if prior:
            user += "\n\nEarlier tiers said: " + "; ".join("%s -> %s (%.2f): %s" % (
                p["decider"], p["action"], p["confidence"], p["rationale"]) for p in prior)
        headers = {"Authorization": "Bearer " + self.api_key} if self.api_key else {}
        r = request("POST", self.base_url + "/chat/completions", {
            "model": self.model, "temperature": 0,
            "messages": [{"role": "system", "content": self.SYSTEM}, {"role": "user", "content": user}],
        }, headers=dict(headers, **{"X-Title": "aitomaton"}), timeout=120)
        text = r["choices"][0]["message"]["content"] or ""
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
        action = data.get("action") if data.get("action") in ACTIONS else "report"
        return _decision(action, data.get("confidence", 0.5), data.get("code_defect", 0.5),
                         (data.get("rationale") or text)[:600], self.name, model=self.model)


def _usable(base_url, key):
    return bool(key) or not base_url.startswith("https://openrouter.ai")


class Decider:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tiers = {}
        if cfg.laya_url:
            laya = Laya(cfg.laya_url)
            self.tiers["laya"] = lambda i, f, s, p, st, trail: laya(i, f, s, p, st)
        if cfg.jev_model and _usable(cfg.jev_base_url, cfg.jev_api_key):
            self.tiers["jev"] = LLM(cfg.jev_base_url, cfg.jev_api_key, cfg.jev_model, name="jev")
        if _usable(cfg.llm_base_url, cfg.llm_api_key):
            self.tiers["llm"] = LLM(cfg.llm_base_url, cfg.llm_api_key, cfg.llm_model)

    def _sure(self, trail):
        """Stop escalating: low stakes, or the last tier is confident and agrees with the rules on fixing."""
        last = trail[-1]
        stakes = last["action"] in ("fix", "report") or trail[0]["action"] in ("fix", "report")
        disagree = (trail[0]["action"] == "fix") != (last["action"] == "fix")
        return not stakes or (last["confidence"] >= self.cfg.escalate_below and not disagree)

    def decide(self, issue, f, service, prom):
        """Returns the final decision with the trail of every tier consulted."""
        state = describe(issue, f, service, prom)
        mode = self.cfg.decider
        order = self.cfg.chain if mode == "chain" else [] if mode == "rules" else [mode]
        trail = [rules(issue, f, service, prom)]
        consulted = 0
        for name in order:
            tier = self.tiers.get(name)
            if not tier:
                continue
            if mode == "chain" and consulted and self._sure([t for t in trail if t["decider"] != "error"]):
                break
            try:
                trail.append(tier(issue, f, service, prom, state, [t for t in trail if t["decider"] != "error"]))
                consulted += 1      # only an answer counts: a tier that is down hands over to the next
            except Exception as e:  # a tier being down never blocks the decision: the next one is asked
                trail.append({"decider": "error", "action": trail[-1]["action"], "confidence": 0,
                              "code_defect": trail[-1]["code_defect"], "rationale": "%s failed: %s" % (name, e)})
        final = dict(next(d for d in reversed(trail) if d["decider"] != "error"))
        # guard rails no model can override
        if final["action"] == "fix" and not (service and service.get("repo")):
            final["action"], final["rationale"] = "report", final["rationale"] + " [no repo: report instead]"
        if final["action"] == "fix" and final["code_defect"] < 0.4:
            final["action"], final["rationale"] = "report", final["rationale"] + " [unlikely code defect]"
        final["trail"] = trail
        final["state"] = state
        return final
