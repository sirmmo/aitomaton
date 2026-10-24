"""Layered policies, emergencies, and the runbook of direct actions.

A policy has two layers. The normal one (aitomaton.policy) applies to everything; the emergency one
(aitomaton.policy.emergency) replaces it while an emergency lasts and may go further: push instead of a PR,
and "+act" for direct actions on the swarm. An emergency is never only observed: it is at least reported.

  aitomaton.policy: pr
  aitomaton.policy.emergency: push+act

Emergencies are detected by rules, not by a model: what counts as "production is down" must be predictable.
Direct actions are a fixed runbook that aitomaton runs itself through the Docker API (no model gets write
access to the swarm), one step at a time, each step once per episode, the next one only if the last did not
help within ACT_VERIFY_SECONDS:

  restart       force a new set of tasks (docker service update --force)
  rollback      back to the previous spec (docker service rollback)
  raise_memory  raise the memory limit by ACT_MEMORY_FACTOR, up to ACT_MEMORY_MAX_MB; the PR makes it stick
"""
from __future__ import annotations

import re

from .http import unix_request

DELIVERIES = ("observe", "report", "pr", "push")
RUNBOOK = ("restart", "rollback", "raise_memory")


def parse_policy(value):
    """'pr', 'push+act', 'report + act' -> {"deliver": "push" or None, "act": bool}."""
    parts = [p for p in re.split(r"[+,\s]+", (value or "").lower()) if p]
    return {"deliver": next((p for p in parts if p in DELIVERIES), None), "act": "act" in parts}


def layer(target, app, emergency, default_emergency=None):
    """The policy layer in force: {"deliver", "act", "layer", "label"}."""
    target = target or {}
    normal = parse_policy(target.get("policy"))["deliver"] or "observe"
    if not emergency:
        return {"deliver": normal, "act": False, "layer": "normal", "label": normal}
    em = parse_policy(target.get("policy_emergency") or default_emergency)
    deliver = em["deliver"] or normal
    if DELIVERIES.index(deliver) < DELIVERIES.index("report"):
        deliver = "report"      # somebody must hear about an emergency
    act = em["act"] and not (app and app.get("act") is False)
    label = deliver + ("+act" if act else "")
    return {"deliver": deliver, "act": act, "layer": "emergency", "label": label + " (emergency)"}


def detect(issue, f, app, now, cfg):
    """None, or {"reason", "runbook": [steps], "since"} for an issue whose episode is an emergency."""
    det, flags = issue.get("detail") or {}, issue.get("flags") or {}
    since = flags.get("fired_at") or issue.get("first_seen") or now
    kind = det.get("kind") if issue["source"] == "swarm" else None
    if kind in ("quorum_lost", "manager_unreachable"):
        return {"reason": "control plane: " + (issue.get("title") or kind), "runbook": [], "since": since}
    if now - since > cfg.emergency_window or flags.get("preexisting"):
        return None     # a long-standing state, or one already there when aitomaton started, is a report
    reps = (app or {}).get("replicas") or {}
    running = det.get("running", reps.get("running")) if kind == "under_replicated" else reps.get("running")
    desired = det.get("desired", reps.get("desired")) if kind == "under_replicated" else reps.get("desired")
    down = bool(desired) and running == 0
    short = bool(desired) and running is not None and running < desired
    deployed = (app or {}).get("updated_at") or 0
    after_deploy = bool(deployed) and deployed - 60 <= since <= deployed + cfg.rollback_window
    first = ["rollback"] if after_deploy else []
    if kind == "node_down":
        return {"reason": issue.get("title") or "a node is down", "runbook": [], "since": since}
    if kind == "under_replicated" and down and not det.get("cause"):
        return {"reason": "%s is down: 0 of %s replicas" % (issue["service"], desired),
                "runbook": first + ["restart"], "since": since}
    if kind == "oom" and short:
        return {"reason": "%s is OOM-killed and short of replicas" % issue["service"], "runbook": ["raise_memory"],
                "since": since}
    if kind == "crash_loop" and down:
        return {"reason": "%s crashes on start%s" % (issue["service"], " after a deploy" if after_deploy else ""),
                "runbook": first or ["restart"], "since": since}
    if kind in ("image_pull", "update_paused") and short:
        return {"reason": "%s: %s" % (issue["service"], (issue.get("title") or kind)), "runbook": first, "since": since}
    if kind in ("unschedulable", "port_conflict") and down:
        return {"reason": "%s is down: %s" % (issue["service"], kind.replace("_", " ")), "runbook": [], "since": since}
    if issue["source"] != "swarm" and f["burst"]["burst"] and after_deploy and \
            (f.get("service_burst") or f["level"] in ("fatal", "critical")):
        return {"reason": "error burst on %s right after a deploy" % issue["service"], "runbook": ["rollback"],
                "since": since}
    if issue["source"] == "user" and det.get("severity") == "critical":
        # a person says production is broken: page humans, but never act on someone's word alone
        return {"reason": "reported as critical: %s" % issue.get("title"), "runbook": [], "since": since}
    if issue["source"] == "prometheus" and f["level"] == "fatal" and app:
        return {"reason": "critical alert: %s" % issue.get("title"), "runbook": [], "since": since}
    return None


class SwarmActions:
    """The runbook, through the Docker API of a manager. Every step returns a sentence for the log."""

    def __init__(self, cfg):
        self.cfg = cfg

    def _inspect(self, sid):
        return unix_request(self.cfg.docker_socket, "GET", "/services/%s" % sid)

    def _update(self, s, spec, rollback=False):
        path = "/services/%s/update?version=%d" % (s["ID"], s["Version"]["Index"])
        unix_request(self.cfg.docker_socket, "POST", path + ("&rollback=previous" if rollback else ""), spec)

    def run(self, step, sid):
        if step not in RUNBOOK:
            raise ValueError("not a runbook step: %s" % step)
        return getattr(self, step)(sid)

    def restart(self, sid):
        s = self._inspect(sid)
        spec = s["Spec"]
        tt = spec.setdefault("TaskTemplate", {})
        tt["ForceUpdate"] = int(tt.get("ForceUpdate") or 0) + 1
        self._update(s, spec)
        return "forced a restart of %s" % spec.get("Name")

    def rollback(self, sid):
        s = self._inspect(sid)
        if not s.get("PreviousSpec"):
            raise RuntimeError("no previous spec to roll back to")
        self._update(s, s["Spec"], rollback=True)
        return "rolled %s back to its previous spec" % s["Spec"].get("Name")

    def raise_memory(self, sid):
        s = self._inspect(sid)
        spec = s["Spec"]
        limits = spec.setdefault("TaskTemplate", {}).setdefault("Resources", {}).setdefault("Limits", {})
        old = int(limits.get("MemoryBytes") or 0)
        if not old:
            raise RuntimeError("no memory limit: the host ran out of memory, not the service")
        new = min(int(old * self.cfg.act_memory_factor), self.cfg.act_memory_max_mb * 2 ** 20)
        if new <= old:
            raise RuntimeError("memory limit already at the ceiling (ACT_MEMORY_MAX_MB)")
        limits["MemoryBytes"] = new
        self._update(s, spec)
        return "raised %s's memory limit from %d to %d MiB" % (spec.get("Name"), old >> 20, new >> 20)
