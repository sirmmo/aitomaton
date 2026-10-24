"""Docker Swarm discovery: services, their aitomaton.* descriptor labels, and recent task failures.

Descriptor labels (service labels, or container labels as a fallback), with prefix `aitomaton`:

  aitomaton.enable              true/false (default: on when the service has a repo)
  aitomaton.repo                GitHub repo: https://github.com/o/r, git@github.com:o/r.git or o/r
                                (falls back to holodocker.repo; the image's org.opencontainers.image.source
                                is used only when aitomaton.enable=true)
  aitomaton.ref                 branch to base fixes on (holodocker.repo_ref, default: main)
  aitomaton.path                subdirectory in a monorepo (holodocker.repo_path)
  aitomaton.policy              observe | report | pr | push   (the most aitomaton may do)
  aitomaton.policy.emergency    the layer in force during an emergency, e.g. push+act (see emergency.py)
  aitomaton.act                 false: never run direct actions on this service, whatever the policies say
  aitomaton.notify.telegram     more Telegram chats for this service's notifications ("<chat>[/<topic>]", ...)
  aitomaton.sentry.project      Sentry/urgentry project slug(s) for this service, comma separated
  aitomaton.architecture        group name for architecture-wide analysis (default: the stack namespace)
  aitomaton.depends             services this one depends on, comma separated (helps find cascade roots)
  aitomaton.prometheus.selector PromQL label selector, e.g. {job="api"}
  aitomaton.agent / .model      the coding agent and model stairway should use for this service
  aitomaton.test                command that runs the tests, passed to the agent
  aitomaton.notes               free text handed to the agent (conventions, where to look, ...)
  aitomaton.git.credential      NAME of the git credential: Docker secret aitomaton_git_<name> or env
                                AITOMATON_GIT_TOKEN_<NAME> (never the token itself; see credentials.py)
  aitomaton.git.author          commit identity, "Name <email>"
  aitomaton.infra.repo          where this service's deployment lives (stack files): infrastructure
                                conditions (OOM kills, image pulls, replicas...) go there instead of the
                                global infrastructure outlet. Also .ref, .path, .policy, .credential
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from datetime import datetime

from .http import unix_get
from .sources import swarm_health

POLICIES = ("observe", "report", "pr", "push")


def normalize_repo(value):
    """'o/r', 'https://github.com/o/r(.git)', 'git@github.com:o/r.git' -> 'o/r' (None when not GitHub)."""
    if not value:
        return None
    v = value.strip()
    m = re.match(r"^(?:https?://|git@|ssh://git@)?(?:www\.)?github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", v)
    if m:
        return "%s/%s" % m.groups()
    if re.match(r"^[\w.-]+/[\w.-]+$", v):
        return v
    return None


def _ts(s):
    """Docker's RFC3339 with nanoseconds -> epoch."""
    if not s:
        return 0
    s = re.sub(r"\.(\d{6})\d*", r".\1", s.replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0


def descriptor(name, labels, prefix="aitomaton", default_policy="report"):
    """The aitomaton view of one service, from its labels."""
    def g(key, *fallbacks):
        for k in ("%s.%s" % (prefix, key),) + fallbacks:
            if labels.get(k):
                return labels[k]
        return None

    explicit = g("repo", "holodocker.repo")
    hint = labels.get("org.opencontainers.image.source")
    enable = g("enable")
    # an image's source label is only a hint: third-party images must never be "fixed" upstream,
    # so it is used only when the service opts in with aitomaton.enable=true
    enabled = (str(enable).lower() in ("1", "true", "yes", "on")) if enable is not None else bool(
        normalize_repo(explicit))
    raw_repo = explicit or (hint if enabled else None)
    repo = normalize_repo(raw_repo)
    policy = (g("policy") or default_policy).lower()
    if policy not in POLICIES:
        policy = "observe"
    stack = labels.get("com.docker.stack.namespace")
    short = name[len(stack) + 1:] if stack and name.startswith(stack + "_") else name
    projects = [p.strip() for p in (g("sentry.project") or "").split(",") if p.strip()]
    return {
        "name": name, "short": short, "stack": stack, "enabled": enabled,
        "repo": repo, "repo_raw": raw_repo, "repo_hint": normalize_repo(hint), "ref": g("ref", "holodocker.repo_ref") or "main",
        "path": g("path", "holodocker.repo_path") or "",
        "policy": policy if enabled else "observe",
        "policy_emergency": g("policy.emergency") if enabled else None,
        "act": False if str(g("act") or "").lower() in ("0", "false", "no", "off") else None,
        "sentry_projects": projects,
        "architecture": g("architecture") or stack or name,
        "depends": [d.strip() for d in (g("depends") or "").split(",") if d.strip()],
        "prometheus_selector": g("prometheus.selector"),
        "agent": g("agent"), "model": g("model"),
        "git_credential": g("git.credential"), "git_author": g("git.author"),
        "test": g("test"), "notes": g("notes"), "notify_telegram": g("notify.telegram"),
        "report_name": g("report.name"),       # what people outside the team call it (Telegram reporters)
        # the deployment's repo, independent of aitomaton.enable: third-party images cannot be fixed upstream,
        # but their limits, tags and placement in your stack files can
        "infra": {"repo": normalize_repo(g("infra.repo")), "ref": g("infra.ref"), "path": g("infra.path"),
                  "policy": (g("infra.policy") or "").lower() or None, "credential": g("infra.credential"),
                  "policy_emergency": g("infra.policy.emergency")},
    }


class Swarm:
    def __init__(self, cfg):
        self.cfg = cfg
        self.services = {}      # name -> descriptor (+ runtime facts)
        self.error = None
        self.refreshed = 0
        self.health = {"conditions": [], "advisories": [], "nodes": [], "managers": {}, "error": None}
        self._image_labels = {}  # image ref -> labels (org.opencontainers.image.source names the repo)

    def refresh(self):
        try:
            services = unix_get(self.cfg.docker_socket, "/services") or []
            tasks = unix_get(self.cfg.docker_socket, "/tasks?filters=" + urllib.parse.quote(
                json.dumps({"desired-state": ["running", "shutdown"]}))) or []
        except Exception as e:  # no socket, not a manager, ...
            self.error = str(e)
            return self.services
        now = time.time()
        if self.cfg.infra:
            try:    # /nodes answers on managers only
                nodes = unix_get(self.cfg.docker_socket, "/nodes") or []
                self.health = dict(swarm_health.check(nodes, services, tasks, now, _ts, self.cfg.infra_grace,
                                                      self.cfg.infra_task_failures), error=None)
            except Exception as e:
                self.health = dict(self.health, conditions=[], error=str(e))
        by_service = {}
        horizon = time.time() - 3600
        for t in tasks:
            st = t.get("Status") or {}
            if st.get("State") in ("failed", "rejected") and _ts(st.get("Timestamp")) >= horizon:
                agg = by_service.setdefault(t.get("ServiceID"), {"failed_tasks_1h": 0, "last_task_error": None})
                agg["failed_tasks_1h"] += 1
                agg["last_task_error"] = st.get("Err") or st.get("Message")
        out = {}
        for s in services:
            spec = s.get("Spec") or {}
            labels = dict(((spec.get("TaskTemplate") or {}).get("ContainerSpec") or {}).get("Labels") or {})
            labels.update(spec.get("Labels") or {})
            image = ((spec.get("TaskTemplate") or {}).get("ContainerSpec") or {}).get("Image", "")
            for k, v in self._labels_of(image).items():
                labels.setdefault(k, v)
            d = descriptor(spec.get("Name", s.get("ID")), labels, self.cfg.label_prefix, self.cfg.default_policy)
            running, desired = s.get("_replicas") or (None, None)
            d.update(id=s.get("ID"), image=image.split("@")[0], updated_at=_ts(s.get("UpdatedAt")),
                     replicas={"running": running, "desired": desired} if desired is not None else None,
                     **by_service.get(s.get("ID"), {"failed_tasks_1h": 0, "last_task_error": None}))
            out[d["name"]] = d
        self.services, self.error, self.refreshed = out, None, now
        return out

    def _labels_of(self, image):
        """Image labels, when the image is present on this node (cached; failures are cached too)."""
        if not image:
            return {}
        if image not in self._image_labels:
            try:
                info = unix_get(self.cfg.docker_socket, "/images/%s/json" % urllib.parse.quote(image, safe=""))
                self._image_labels[image] = ((info or {}).get("Config") or {}).get("Labels") or {}
            except Exception:
                self._image_labels[image] = {}
        return self._image_labels[image]

    def for_project(self, project):
        """The service a Sentry project belongs to: an explicit label, else a name match."""
        if not project:
            return None
        for d in self.services.values():
            if project in d["sentry_projects"]:
                return d
        for d in self.services.values():
            if project in (d["name"], d["short"]):
                return d
        return None

    def get(self, name):
        if not name:
            return None
        if name in self.services:
            return self.services[name]
        for d in self.services.values():
            if name == d["short"]:
                return d
        return None
