"""Print a status.json for the demo on the project site: a synthetic swarm and synthetic errors, run
through the real engine (rules decider, no network). Usage: python scripts/demo_status.py > status.json"""
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.update({"AITOMATON_DB": ":memory:", "AITOMATON_DECIDER": "rules", "AITOMATON_DISPATCH": "1",
                   "STAIRWAY_URL": "", "LLM_API_KEY": "", "OPENROUTER_API_KEY": "", "SENTRY_URL": ""})

from aitomaton.config import Config  # noqa: E402
from aitomaton.engine import Engine  # noqa: E402
from aitomaton.store import Store  # noqa: E402
from aitomaton.swarm import Swarm, descriptor  # noqa: E402

random.seed(7)
now = time.time()
cfg = Config()


def svc(name, stack, **labels):
    d = descriptor(name, dict({"com.docker.stack.namespace": stack}, **{"aitomaton." + k: v for k, v in labels.items()}))
    d.update(failed_tasks_1h=0, last_task_error=None, image="ghcr.io/acme/%s:1.4" % d["short"])
    return d


services = [
    svc("shop_web", "shop", repo="acme/shop-web", policy="pr", **{"sentry.project": "shop-web", "depends": "api"}),
    svc("shop_api", "shop", repo="acme/shop-api", policy="pr", **{"sentry.project": "shop-api", "depends": "db"}),
    svc("shop_db", "shop", enable="true", policy="report", **{"sentry.project": "shop-db"}),
    svc("shop_worker", "shop", repo="acme/shop-worker", policy="push", **{"sentry.project": "shop-worker"}),
    svc("billing_api", "billing", repo="acme/billing", policy="pr", **{"sentry.project": "billing"}),
    svc("billing_cron", "billing", repo="acme/billing", policy="observe", path="cron",
        **{"sentry.project": "billing-cron"}),
    svc("blog_ghost", "blog", repo="acme/blog-theme", policy="report"),
]
services[3].update(failed_tasks_1h=4, last_task_error="task: non-zero exit (137): OOMKilled")
swarm = Swarm(cfg)
swarm.services = {s["name"]: s for s in services}
swarm.refreshed = now * 2
store = Store(":memory:")
engine = Engine(cfg, store, swarm)


def frames(*fs):
    return [{"file": f, "function": fn, "line": ln, "in_app": True, "context": ctx} for f, fn, ln, ctx in fs]


def issue(key, service, title, culprit, level="error", first=30, users=0, exc=None, regression=False, source="sentry"):
    d = swarm.services[service]
    store.upsert_issue(key, source=source, project=key.split(":")[1], external_id=key.split(":")[2], service=service,
                       architecture=d["architecture"], title=title, culprit=culprit, level=level,
                       status="unresolved", first_seen=now - first * 86400, last_seen=now - 30, total=0, users=users,
                       url="https://urgentry.example/organizations/acme/issues/%s/" % key.split(":")[2],
                       detail={"exceptions": [exc]} if exc else {}, flags={"regression": regression})


def spread(key, per_day, days=14, jitter=0.4):
    total = 0
    for d in range(days):
        n = max(0, int(per_day(d) * random.uniform(1 - jitter, 1 + jitter)))
        for _ in range(n):
            store.add_occurrence(key, now - (days - d) * 86400 + random.uniform(0, 86400 - 600))
        total += n
    return total


def burst(key, n, start=240):
    for k in range(n):
        store.add_occurrence(key, now - start + k * start / n)
    return n


# a cascade in "shop": the database starts failing, the API and the web front follow
issue("sentry:shop-db:9001", "shop_db", "OperationalError: too many connections for role \"shop\"", "pg pool",
      level="fatal", first=0.01, users=0)
issue("sentry:shop-api:4242", "shop_api", "TimeoutError: acquiring a connection from the pool timed out",
      "app/db.py in get_conn", users=210,
      exc={"type": "TimeoutError", "value": "acquiring a connection timed out after 5s",
           "frames": frames(("app/db.py", "get_conn", 31, "conn = await pool.acquire(timeout=5)"),
                            ("app/routes/cart.py", "view_cart", 58, "async with get_conn() as c:"))})
issue("sentry:shop-web:7310", "shop_web", "Error: Request failed with status code 502", "src/api/client.ts",
      users=530, exc={"type": "Error", "value": "Request failed with status code 502",
                      "frames": frames(("src/api/client.ts", "request", 44, "throw new ApiError(res)"))})
# a real code defect, new and bursting, outside the cascade window
issue("sentry:billing:5120", "billing_api", "KeyError: 'vat_rate'", "billing/invoice.py in total", first=0.02,
      users=37, exc={"type": "KeyError", "value": "'vat_rate'",
                     "frames": frames(("billing/invoice.py", "total", 88, "rate = country['vat_rate']"),
                                      ("billing/api.py", "create_invoice", 140, "inv.total()"))})
# a slowly rising, chronic one
issue("sentry:shop-worker:3007", "shop_worker", "ValueError: image too large to thumbnail", "worker/thumbs.py",
      level="warning", first=40, users=4, exc={"type": "ValueError", "value": "image too large (48 MP)",
                                               "frames": frames(("worker/thumbs.py", "make", 21, "img.thumbnail(size)"))})
# a regression, and a quiet low-level one
issue("sentry:billing-cron:66", "billing_cron", "ZeroDivisionError: division by zero", "cron/report.py in ratio",
      first=60, users=0, regression=True,
      exc={"type": "ZeroDivisionError", "value": "division by zero",
           "frames": frames(("cron/report.py", "ratio", 12, "return paid / issued"))})
issue("sentry:shop-web:7400", "shop_web", "ResizeObserver loop limit exceeded", "window", level="info", first=90)

totals = {
    "sentry:shop-db:9001": burst("sentry:shop-db:9001", 60, 300),
    "sentry:shop-api:4242": spread("sentry:shop-api:4242", lambda d: 1) + burst("sentry:shop-api:4242", 140, 270),
    "sentry:shop-web:7310": spread("sentry:shop-web:7310", lambda d: 2) + burst("sentry:shop-web:7310", 220, 240),
    "sentry:billing:5120": burst("sentry:billing:5120", 45, 280),
    "sentry:shop-worker:3007": spread("sentry:shop-worker:3007", lambda d: 1 + d * 2.5, jitter=0.2),
    "sentry:billing-cron:66": spread("sentry:billing-cron:66", lambda d: 1 if d == 13 else 0, jitter=0),
    "sentry:shop-web:7400": spread("sentry:shop-web:7400", lambda d: 1 if d % 5 == 0 else 0, jitter=0),
}
for k, n in totals.items():
    store.upsert_issue(k, total=n)
engine.alertmanager_webhook({"alerts": [{"status": "firing", "labels": {
    "alertname": "ContainerMemoryNearLimit", "service": "shop_worker", "severity": "warning"},
    "annotations": {"summary": "shop_worker uses 96% of its memory limit"}}]})

engine.tick(now)

# what the works look like once stairway and GitHub have answered (the demo has neither)
for w in store.works():
    issue_ = store.issue(w["key"])
    if w["kind"] == "fix" and issue_["service"] == "billing_api":
        store.update_work(w["id"], status="running", note="folder created; cloning with env AITOMATON_GIT_TOKEN_ACME",
                          url="https://claude.ai/code/session_demo")
    elif w["kind"] == "fix":
        store.update_work(w["id"], status="done", note="opened PR",
                          result_url="https://github.com/%s/pull/128" % swarm.services[issue_["service"]]["repo"],
                          summary="Release pooled connections on cancellation; regression test added.")
    elif w["kind"] == "report":
        store.update_work(w["id"], status="done", note="opened issue",
                          result_url="https://github.com/acme/shop-db/issues/12")

status = engine.status()
status["demo"] = True
for s in status["services"]:
    if s["enabled"]:
        s["git"] = "env AITOMATON_GIT_TOKEN_ACME"
json.dump(status, sys.stdout, indent=1, default=str)
