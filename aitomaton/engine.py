"""The loop: ingest -> analyse -> decide -> act -> follow up, and the status JSON."""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

from . import __version__
from . import credentials, workspace
from .analysis import analyze
from .deciders import Decider, cap
from .executors import GitHub, Stairway, brief, experiment_name, fix_branch, parse_result, report_body
from .sources.prometheus import Prometheus, parse_alertmanager
from .sources.sentry import SentryClient, issue_key, parse_webhook, summarize_event
from .store import OPEN_WORK, Store
from .swarm import Swarm

log = logging.getLogger("aitomaton")


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


class Engine:
    def __init__(self, cfg, store=None, swarm=None):
        self.cfg = cfg
        self.store = store or Store(cfg.db)
        self.swarm = swarm or Swarm(cfg)
        self.sentry = SentryClient(cfg)
        self.prom = Prometheus(cfg)
        self.decider = Decider(cfg)
        self.stairway = Stairway(cfg)
        self.github = GitHub(cfg)
        self.analysis = {"issues": {}, "services": {}, "cascades": {}, "findings": []}
        self.last_tick = 0
        self.tick_error = None
        self.started = time.time()
        self._wake = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ ingest
    def _ingest(self, source, project, i, event=None, occurred=None):
        svc = self.swarm.for_project(project) if source == "sentry" else self.swarm.get(i.get("service"))
        key = i.get("key") or issue_key(project, i["id"])
        flags = {}
        if i.get("regression"):
            flags["regression"] = True
        if i.get("action") == "resolved":
            flags["regression"] = False
        fields = dict(source=source, project=project, external_id=i.get("id"),
                      service=svc["name"] if svc else i.get("service") or project,
                      architecture=svc["architecture"] if svc else None,
                      title=i.get("title"), culprit=i.get("culprit"), level=i.get("level"), status=i.get("status"),
                      first_seen=i.get("first_seen"), last_seen=i.get("last_seen"), total=i.get("total"),
                      users=i.get("users"), url=i.get("url"), flags=flags,
                      detail=event if event else None)
        cur = self.store.issue(key)
        if cur and fields["last_seen"] and cur["last_seen"]:
            fields["last_seen"] = max(fields["last_seen"], cur["last_seen"])
        if occurred:
            self.store.add_occurrence(key, occurred)
            if not fields.get("total"):
                fields["total"] = (cur["total"] if cur else 0) + 1
        return self.store.upsert_issue(key, **fields)

    def sentry_webhook(self, headers, payload):
        n = parse_webhook(headers, payload)
        if not n:
            return None
        i = n["issue"]
        out = self._ingest("sentry", i["project"], i, n["event"], n["occurred"])
        self._wake.set()
        return out["key"]

    def alertmanager_webhook(self, payload):
        keys = []
        now = time.time()
        for a in parse_alertmanager(payload):
            f = dict(a["fields"], key=a["key"], service=a["service"], first_seen=a["occurred"] or now,
                     last_seen=now)
            self._ingest("prometheus", f.pop("project"), f, f.pop("detail"),
                         occurred=now if a["firing"] else None)
            keys.append(a["key"])
        self._wake.set()
        return keys

    def stairway_callback(self, payload):
        w = self.store.work_by_ref(payload.get("experiment"))
        if not w:
            return False
        text = payload.get("text") or ""
        if payload.get("event") == "waiting":
            self.store.update_work(w["id"], note="waiting for permission: " + text[:300], status="waiting")
            return True
        res = parse_result(text)
        if res:
            self._finish(w, res)
        else:
            self.store.update_work(w["id"], note=text[-300:], status="running")
        return True

    def _finish(self, w, res):
        status = {"pr": "done", "pushed": "done", "no_fix": "no-fix"}.get(res.get("status"), "failed")
        url = res.get("url") or None
        note = "result: %s" % res.get("status")
        if res.get("status") == "branch":       # the agent pushed a branch; aitomaton opens the PR
            status, url, note = self._open_pr(w, res)
        self.store.update_work(w["id"], note=note, status=status, result_url=url, summary=res.get("summary"))
        self._cleanup(w)

    def _open_pr(self, w, res):
        issue = self.store.issue(w["key"]) or {}
        service = self.swarm.get(issue.get("service"))
        cred = self.credential(service)
        if not service or not cred["token"]:
            return "failed", None, "branch pushed but no credential to open the PR (%s)" % cred["source"]
        title = res.get("title") or "[aitomaton] fix: %s" % issue.get("title")
        body = (res.get("body") or res.get("summary") or "") + "\n\n---\nPrepared by aitomaton for %s" % (
            issue.get("url") or w["key"])
        try:
            url = self.github.open_pr(service["repo"], res.get("branch") or fix_branch(issue),
                                      service.get("ref") or "main", title, body, cred["token"])
            return "done", url, "opened PR"
        except Exception as e:
            return "failed", None, credentials.redact("opening the PR: %s" % e, cred["token"])

    def credential(self, service):
        return credentials.resolve(service, environ=self.cfg.environ, secrets_dir=self.cfg.secrets_dir)

    def _folder(self, w):
        return os.path.join(self.cfg.workspace_root, w["ref"]) if self.cfg.workspace_root and w.get("ref") else None

    def _cleanup(self, w):
        if w.get("executor") == "stairway":
            workspace.cleanup(self._folder(w))

    def _prepares(self, service):
        """aitomaton clones for the agent when it can see stairway's root and the service has a credential."""
        return bool(self.cfg.workspace_root and self.credential(service)["token"])

    # ------------------------------------------------------------ loop
    def run_forever(self):
        while True:
            try:
                self.tick()
            except Exception as e:  # keep going: one bad tick must not kill the service
                log.exception("tick failed")
                self.tick_error = str(e)
            self._wake.wait(self.cfg.tick)
            self._wake.clear()

    def tick(self, now=None):
        with self._lock:
            now = now or time.time()
            if now - self.swarm.refreshed >= self.cfg.swarm_refresh:
                self.swarm.refresh()
                self._remap()
            if self.sentry.enabled and now - self.sentry.last_poll >= self.cfg.sentry_poll:
                self.sentry.poll(self.store, lambda p, i: self._ingest("sentry", p, i))
            self.analysis = analyze(self.store, self.swarm, self.cfg, now)
            self.store.set_findings(self.analysis["findings"], now)
            self._decide_all(now)
            self._follow_works()
            self.store.prune(now - (self.cfg.long_days + 1) * 86400)
            self.last_tick, self.tick_error = now, None

    def _remap(self):
        """Issues stored before the swarm was known get their service and architecture now."""
        for i in self.store.q("SELECT * FROM issues WHERE architecture IS NULL"):
            svc = self.swarm.for_project(i["project"]) if i["source"] == "sentry" else self.swarm.get(i["service"])
            if svc:
                self.store.x("UPDATE issues SET service=?, architecture=? WHERE key=?",
                             (svc["name"], svc["architecture"], i["key"]))

    def _needs_decision(self, issue, f, now):
        if f["events_24h"] == 0 and not f["regression"]:
            return False
        if self.store.open_work(issue["key"]):
            return False
        last = self.store.last_decision(issue["key"])
        if not last:
            return True
        if (issue["last_seen"] or 0) <= last["at"]:
            return False
        lf = last["features"] or {}
        escalated = (f["burst"]["burst"] and not (lf.get("burst") or {}).get("burst")) or \
                    (f["regression"] and not lf.get("regression")) or \
                    (f.get("cascade_root") and not lf.get("cascade_root"))
        return escalated or now - last["at"] >= self.cfg.decision_cooldown

    def _decide_all(self, now):
        for key, f in self.analysis["issues"].items():
            issue = self.store.issue(key)
            if not issue or not self._needs_decision(issue, f, now):
                continue
            service = self.swarm.get(issue["service"])
            if issue["source"] == "sentry" and not issue["detail"].get("exceptions") and self.sentry.enabled:
                try:
                    ev = self.sentry.latest_event(issue["external_id"])
                    if ev:
                        issue = self.store.upsert_issue(key, detail=summarize_event(ev))
                        f["has_stacktrace"] = bool(issue["detail"].get("exceptions"))
                        f["in_app_frames"] = sum(1 for e in issue["detail"].get("exceptions") or []
                                                 for x in e.get("frames") or [] if x.get("in_app"))
                except Exception as e:
                    log.warning("latest event for %s: %s", key, e)
            prom = self.prom.context(service) if service else {}
            d = self.decider.decide(issue, f, service, prom)
            policy = service["policy"] if service else "observe"
            applied = cap(d["action"], policy)
            features = dict(f, metrics=prom)
            did = self.store.add_decision(key, d["action"], applied, policy, d["decider"], d["confidence"],
                                          d["rationale"], dict(features, trail=d["trail"]))
            log.info("decision %s: %s -> %s (%s, policy %s)", key, d["action"], applied, d["decider"], policy)
            if applied == "fix":
                self._start_fix(issue, f, d, service, prom, did, now)
            elif applied == "report":
                self._report(issue, f, d, service, prom, did)

    def _start_fix(self, issue, f, d, service, prom, did, now):
        text = brief(issue, f, d, service, prom, self.cfg, prepared=self._prepares(service))
        name = experiment_name(self.cfg.stairway_prefix, service["name"], issue["key"])
        if not self.cfg.dispatch or not self.stairway.enabled:
            why = "dispatch is off" if not self.cfg.dispatch else "stairway is not configured"
            wid = self.store.add_work(issue["key"], did, "fix", "dry-run", "stairway", text)
            self.store.update_work(wid, note="prepared, not sent: " + why, ref=name)
            return
        running = len(self.store.works(["dispatched", "running", "waiting"]))
        if running >= self.cfg.max_running or self.store.works_since(now - 86400, "fix") >= self.cfg.max_per_day:
            wid = self.store.add_work(issue["key"], did, "fix", "queued", "stairway", text)
            self.store.update_work(wid, note="queued: concurrency or daily limit", ref=name)
            return
        wid = self.store.add_work(issue["key"], did, "fix", "dispatched", "stairway", text)
        self._dispatch(wid, name, text, service)

    def _dispatch(self, wid, name, text, service):
        cred = self.credential(service)
        try:
            if not self._prepares(service):
                msg = self.stairway.create(name, text, service)
                self.store.update_work(wid, note=msg[:300] + " [git: %s]" % cred["source"], status="running",
                                       ref=name)
                return
            self.stairway.create(name, text, service, start=False)
            self.store.update_work(wid, note="folder created; cloning with %s" % cred["source"], ref=name)
            issue = self.store.issue(self.store.work(wid)["key"])
            workspace.prepare(os.path.join(self.cfg.workspace_root, name), service["repo"],
                              service.get("ref") or "main", fix_branch(issue), cred,
                              push_to_ref=service["policy"] == "push", base=self.cfg.git_base)
            msg = self.stairway.start(name)
            self.store.update_work(wid, note=msg[:300], status="running")
        except Exception as e:
            self.store.update_work(wid, note=credentials.redact("dispatch: %s" % e, cred["token"]), status="failed",
                                   ref=name)
            self._cleanup(self.store.work(wid))

    def _report(self, issue, f, d, service, prom, did):
        body = report_body(issue, f, d, prom)
        cred = self.credential(service) if service and service.get("repo") else {"token": None}
        if cred["token"] and self.cfg.dispatch:
            wid = self.store.add_work(issue["key"], did, "report", "dispatched", "github", body)
            try:
                url = self.github.open_issue(service["repo"], "[aitomaton] %s" % issue["title"], body, cred["token"])
                self.store.update_work(wid, status="done", result_url=url, ref=url)
            except Exception as e:
                self.store.update_work(wid, note=credentials.redact("github: %s" % e, cred["token"]),
                                       status="failed")
        else:
            self.store.add_work(issue["key"], did, "report", "reported", "status", body)

    def _follow_works(self):
        """Poll experiments that did not call back, and start queued fixes when there is room."""
        if self.stairway.enabled:
            for w in self.store.works(["running", "waiting", "dispatched"]):
                if w["executor"] != "stairway" or not w["ref"]:
                    continue
                try:
                    info = self.stairway.info(w["ref"])
                except Exception as e:
                    log.debug("experiment_info %s: %s", w["ref"], e)
                    continue
                url = info.get("session_url") or info.get("editor_url")
                if url and url != w["url"]:
                    self.store.update_work(w["id"], url=url)
                if not info.get("running") and w["status"] in ("running", "waiting"):
                    self.store.update_work(w["id"], note="session ended without a result", status="ended")
                    self._cleanup(w)
            if self.cfg.dispatch:
                for w in reversed(self.store.works(["queued"])):
                    running = len(self.store.works(["dispatched", "running", "waiting"]))
                    if running >= self.cfg.max_running or \
                            self.store.works_since(time.time() - 86400, "fix") >= self.cfg.max_per_day:
                        break
                    issue = self.store.issue(w["key"])
                    service = self.swarm.get(issue["service"]) if issue else None
                    if service:
                        self.store.update_work(w["id"], status="dispatched")
                        self._dispatch(w["id"], w["ref"], w["brief"], service)

    # ------------------------------------------------------------ status JSON
    def status(self):
        a = self.analysis
        issues = []
        for i in self.store.issues(time.time() - self.cfg.long_days * 86400)[:200]:
            f = a["issues"].get(i["key"]) or {}
            last = self.store.last_decision(i["key"])
            issues.append({
                "key": i["key"], "source": i["source"], "project": i["project"], "service": i["service"],
                "architecture": i["architecture"], "title": i["title"], "culprit": i["culprit"],
                "level": i["level"], "status": i["status"], "url": i["url"],
                "first_seen": iso(i["first_seen"]), "last_seen": iso(i["last_seen"]),
                "total": i["total"], "users": i["users"],
                "analysis": {k: f.get(k) for k in ("new", "regression", "events_1h", "events_24h", "burst",
                                                   "long_term", "cascade", "cascade_root", "service_burst",
                                                   "has_stacktrace", "in_app_frames", "failed_tasks_1h")} if f else None,
                "decision": {"action": last["action"], "applied": last["applied"], "at": iso(last["at"]),
                             "decider": last["decider"]} if last else None,
            })
        decisions = [{
            "id": d["id"], "issue": d["key"], "at": iso(d["at"]), "action": d["action"], "applied": d["applied"],
            "policy": d["policy"], "decider": d["decider"], "confidence": d["confidence"], "rationale": d["rationale"],
            "trail": [{k: t.get(k) for k in ("decider", "action", "confidence", "code_defect", "rationale", "model")}
                      for t in (d["features"] or {}).get("trail") or []],
        } for d in self.store.decisions(100)]
        works = [{
            "id": w["id"], "issue": w["key"], "kind": w["kind"], "status": w["status"], "executor": w["executor"],
            "ref": w["ref"], "url": w["url"], "result_url": w["result_url"], "summary": w["summary"],
            "created": iso(w["created"]), "updated": iso(w["updated"]),
            "log": [dict(e, at=iso(e.get("at"))) for e in (w["log"] or [])[-10:]],
            "brief": w["brief"],
        } for w in self.store.works(limit=100)]
        services = [dict({k: s.get(k) for k in ("name", "stack", "architecture", "enabled", "repo", "repo_hint", "ref",
                                                "policy", "sentry_projects", "image", "failed_tasks_1h",
                                                "last_task_error")},
                         git=self.credential(s)["source"] if s.get("enabled") else None)
                    for s in sorted(self.swarm.services.values(), key=lambda s: s["name"])]
        archs = {}
        for s in self.swarm.services.values():
            arch = archs.setdefault(s["architecture"], {"name": s["architecture"], "services": 0, "issues": 0,
                                                        "events_24h": 0, "cascade": None})
            arch["services"] += 1
        for i in issues:
            if i["architecture"] in archs:
                archs[i["architecture"]]["issues"] += 1
                archs[i["architecture"]]["events_24h"] += (i["analysis"] or {}).get("events_24h") or 0
        for name, c in a["cascades"].items():
            archs.setdefault(name, {"name": name, "services": len(c["services"]), "issues": 0, "events_24h": 0})
            archs[name]["cascade"] = dict(c, onset=iso(c["onset"]))
        findings = [{"id": f["id"], "kind": f["kind"], "scope": f["scope"], "target": f["target"],
                     "severity": f["severity"], "active": bool(f["active"]), "first_at": iso(f["first_at"]),
                     "last_at": iso(f["last_at"]), "detail": f["detail"]} for f in self.store.findings(limit=200)]
        counts = {s: 0 for s in ("dry-run", "queued", "dispatched", "running", "waiting", "done", "no-fix",
                                 "failed", "ended", "reported")}
        for w in self.store.q("SELECT status, COUNT(*) n FROM works GROUP BY status"):
            counts[w["status"]] = w["n"]
        return {
            "aitomaton": __version__, "generated_at": iso(time.time()), "started_at": iso(self.started),
            "last_tick": iso(self.last_tick), "tick_error": self.tick_error,
            "config": self.cfg.summary(),
            "sources": {
                "sentry": {"enabled": self.sentry.enabled, "last_poll": iso(self.sentry.last_poll),
                           "error": self.sentry.error},
                "prometheus": {"enabled": self.prom.enabled, "error": self.prom.error},
                "swarm": {"services": len(self.swarm.services), "refreshed": iso(self.swarm.refreshed),
                          "error": self.swarm.error},
            },
            "summary": {
                "issues": len(issues),
                "active_findings": sum(1 for f in findings if f["active"]),
                "decisions_24h": self.store.q("SELECT COUNT(*) n FROM decisions WHERE at>=?",
                                              (time.time() - 86400,))[0]["n"],
                "works": counts,
                "in_progress": sum(counts[s] for s in OPEN_WORK),
            },
            "works": works, "findings": findings, "decisions": decisions, "issues": issues,
            "architectures": sorted(archs.values(), key=lambda x: -x["events_24h"]),
            "services": services,
        }
