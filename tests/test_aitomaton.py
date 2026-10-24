import hashlib
import hmac
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aitomaton.analysis import analyze  # noqa: E402
from aitomaton.config import Config  # noqa: E402
from aitomaton.deciders import cap, rules  # noqa: E402
from aitomaton import emergency  # noqa: E402
from aitomaton.engine import Engine  # noqa: E402
from aitomaton.mcp import caller  # noqa: E402
from aitomaton.notify import Notifier, parse_chats  # noqa: E402
from aitomaton.executors import experiment_name, fix_branch, parse_result  # noqa: E402
from aitomaton.server import make_handler  # noqa: E402
from aitomaton.sources.prometheus import parse_alertmanager  # noqa: E402
from aitomaton.sources import swarm_health  # noqa: E402
from aitomaton.sources.sentry import parse_webhook, verify_signature  # noqa: E402
from aitomaton.store import Store  # noqa: E402
from aitomaton.swarm import Swarm, descriptor, normalize_repo  # noqa: E402

NOW = 1_800_000_000.0


def cfg(**env):
    base = {"AITOMATON_DB": ":memory:", "AITOMATON_DECIDER": "rules", "LLM_BASE_URL": "https://openrouter.ai/api/v1",
            "LLM_API_KEY": "", "OPENROUTER_API_KEY": "", "SENTRY_URL": "", "PROMETHEUS_URL": "", "LAYA_URL": "",
            "STAIRWAY_URL": "", "STAIRWAY_TOKEN": "", "GITHUB_TOKEN": "", "AITOMATON_DISPATCH": "0",
            "JEV_MODEL": "", "JEV_BASE_URL": "", "JEV_API_KEY": "", "AITOMATON_CHAIN": "", "AITOMATON_GIT_TOKEN": "",
            "AITOMATON_INFRA": "1", "AITOMATON_INFRA_REPO": "", "AITOMATON_INFRA_POLICY": "",
            "AITOMATON_INFRA_CREDENTIAL": "",
            "AITOMATON_INFRA_POLICY_EMERGENCY": "", "AITOMATON_EMERGENCY_POLICY": "", "AITOMATON_ACT": "0",
            "AITOMATON_NOTIFY_URL": "", "AITOMATON_TELEGRAM_TOKEN": "", "AITOMATON_TELEGRAM_CHAT": "",
            "AITOMATON_NOTIFY_EVENTS": "", "AITOMATON_SECRETS_DIR": "/nonexistent", "AITOMATON_MCP_TOKEN": "",
            "MCP_DECISION_WAIT_SECONDS": "0"}
    base.update(env)
    old = {k: os.environ.get(k) for k in base}
    os.environ.update(base)
    try:
        return Config()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def swarm_with(c, *descs):
    s = Swarm(c)
    s.services = {d["name"]: dict(d, failed_tasks_1h=0, last_task_error=None) for d in descs}
    s.refreshed = NOW * 2      # never refresh from a real socket in tests
    return s


API = descriptor("shop_api", {"com.docker.stack.namespace": "shop", "aitomaton.repo": "https://github.com/acme/api.git",
                              "aitomaton.policy": "pr", "aitomaton.sentry.project": "api",
                              "aitomaton.depends": "db"})
WEB = descriptor("shop_web", {"com.docker.stack.namespace": "shop", "holodocker.repo": "acme/web",
                              "aitomaton.sentry.project": "web", "aitomaton.depends": "api"})
DB = descriptor("shop_db", {"com.docker.stack.namespace": "shop", "aitomaton.enable": "true",
                            "aitomaton.sentry.project": "db"})


def event_alert(project, issue_id, ts, in_app=True):
    return {"action": "triggered", "data": {"event": {
        "event_id": "e%s" % ts, "issue_id": issue_id, "title": "KeyError: 'price'", "culprit": "cart.total",
        "level": "error", "datetime": ts, "url": "https://sentry.example/api/0/projects/acme/%s/events/x/" % project,
        "web_url": "https://sentry.example/organizations/acme/issues/%s/" % issue_id,
        "exception": {"values": [{"type": "KeyError", "value": "'price'", "stacktrace": {"frames": [
            {"filename": "lib/framework.py", "function": "dispatch", "lineno": 10, "in_app": False},
            {"filename": "cart.py", "function": "total", "lineno": 42, "in_app": in_app,
             "context_line": "  return item['price'] * qty"}]}}]}}}}


class Labels(unittest.TestCase):
    def test_repo_forms(self):
        for v in ("acme/api", "https://github.com/acme/api", "https://github.com/acme/api.git",
                  "git@github.com:acme/api.git"):
            self.assertEqual(normalize_repo(v), "acme/api")
        self.assertIsNone(normalize_repo("https://gitlab.com/acme/api"))

    def test_descriptor(self):
        self.assertEqual((API["repo"], API["policy"], API["architecture"], API["short"]), ("acme/api", "pr", "shop", "api"))
        self.assertEqual((WEB["repo"], WEB["policy"]), ("acme/web", "report"))       # holodocker label, default policy
        self.assertEqual((DB["repo"], DB["enabled"]), (None, True))
        self.assertEqual(descriptor("x", {})["policy"], "observe")                  # no repo: disabled
        self.assertEqual(descriptor("x", {"aitomaton.repo": "a/b", "aitomaton.policy": "yolo"})["policy"], "observe")

    def test_image_source_is_only_a_hint(self):
        third_party = descriptor("traefik", {"org.opencontainers.image.source": "https://github.com/traefik/traefik"})
        self.assertEqual((third_party["enabled"], third_party["repo"], third_party["repo_hint"]),
                         (False, None, "traefik/traefik"))
        own = descriptor("app", {"org.opencontainers.image.source": "https://github.com/me/app",
                                 "aitomaton.enable": "true"})
        self.assertEqual((own["enabled"], own["repo"]), (True, "me/app"))


class Webhooks(unittest.TestCase):
    def test_event_alert(self):
        n = parse_webhook({"Sentry-Hook-Resource": "event_alert"}, event_alert("api", "77", "2027-01-15T08:00:00Z"))
        self.assertEqual((n["issue"]["project"], n["issue"]["id"]), ("api", "77"))
        frames = n["event"]["exceptions"][0]["frames"]
        self.assertEqual([f["file"] for f in frames], ["cart.py"])                  # in-app only
        self.assertTrue(n["occurred"])

    def test_issue_regression(self):
        n = parse_webhook({"Sentry-Hook-Resource": "issue"}, {"action": "unresolved", "data": {"issue": {
            "id": "5", "title": "t", "project": {"slug": "web"}, "count": "12", "lastSeen": "2027-01-15T08:00:00.123Z"}}})
        self.assertTrue(n["issue"]["regression"])
        self.assertEqual(n["issue"]["total"], 12)

    def test_legacy_plugin(self):
        n = parse_webhook({}, {"id": "9", "project": "api", "message": "boom", "level": "fatal", "event": {}})
        self.assertEqual((n["issue"]["id"], n["issue"]["level"]), ("9", "fatal"))

    def test_signature(self):
        body = b'{"a":1}'
        sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature("s3cret", body, sig))
        self.assertFalse(verify_signature("s3cret", body, "nope"))
        self.assertTrue(verify_signature(None, body, None))

    def test_alertmanager(self):
        a = parse_alertmanager({"alerts": [{"status": "firing", "labels": {"alertname": "HighLatency", "service": "shop_api",
                                                                           "severity": "critical"},
                                            "annotations": {"summary": "p99 > 2s"}, "startsAt": "2027-01-15T08:00:00Z"}]})
        self.assertEqual(a[0]["key"], "prom:HighLatency:shop_api")
        self.assertEqual(a[0]["fields"]["level"], "fatal")


class Analysis(unittest.TestCase):
    def setUp(self):
        self.c = cfg()
        self.store = Store(":memory:")
        self.swarm = swarm_with(self.c, API, WEB, DB)

    def issue(self, key, service, **kw):
        self.store.upsert_issue(key, source="sentry", service=service, architecture="shop", title=key, level="error",
                                status="unresolved", first_seen=NOW - 30 * 86400, last_seen=NOW, **kw)

    def test_burst_vs_baseline(self):
        self.issue("sentry:api:1", "shop_api")
        for h in range(1, 24):                       # steady 2/hour baseline
            self.store.add_occurrence("sentry:api:1", NOW - h * 3600, 2)
        self.store.add_occurrence("sentry:api:1", NOW - 60, 30)
        a = analyze(self.store, self.swarm, self.c, NOW)
        b = a["issues"]["sentry:api:1"]["burst"]
        self.assertTrue(b["burst"], b)
        self.assertIn("burst", [f["kind"] for f in a["findings"] if f["scope"] == "issue"])

    def test_steady_is_not_burst(self):
        self.issue("sentry:api:1", "shop_api")
        for m in range(0, 24 * 60, 5):
            self.store.add_occurrence("sentry:api:1", NOW - m * 60, 3)
        self.assertFalse(analyze(self.store, self.swarm, self.c, NOW)["issues"]["sentry:api:1"]["burst"]["burst"])

    def test_rising_trend(self):
        self.issue("sentry:api:2", "shop_api")
        for d in range(14):
            self.store.add_occurrence("sentry:api:2", NOW - (14 - d) * 86400, 1 + d * 3)
        lt = analyze(self.store, self.swarm, self.c, NOW)["issues"]["sentry:api:2"]["long_term"]
        self.assertTrue(lt["rising"], lt)
        self.assertGreaterEqual(lt["days_active"], 13)

    def test_a_burst_today_is_not_a_trend(self):
        self.issue("sentry:api:3", "shop_api")
        for d in range(1, 14):
            self.store.add_occurrence("sentry:api:3", NOW - d * 86400, 5)
        self.store.add_occurrence("sentry:api:3", NOW - 60, 300)
        a = analyze(self.store, self.swarm, self.c, NOW)["issues"]["sentry:api:3"]
        self.assertTrue(a["burst"]["burst"])
        self.assertFalse(a["long_term"]["rising"], a["long_term"])

    def test_cascade_root_by_dependency(self):
        for svc, delay in (("shop_web", 0), ("shop_api", 30), ("shop_db", 120)):
            key = "sentry:%s:1" % svc
            self.issue(key, svc)
            self.store.add_occurrence(key, NOW - 200 + delay, 20)
        a = analyze(self.store, self.swarm, self.c, NOW)
        c = a["cascades"]["shop"]
        self.assertEqual(set(c["services"]), {"shop_web", "shop_api", "shop_db"})
        self.assertEqual(c["root"], "shop_db")      # web needs api, api needs db -> db is depended on
        self.assertTrue(a["issues"]["sentry:shop_db:1"]["cascade_root"])
        self.assertFalse(a["issues"]["sentry:shop_web:1"]["cascade_root"])


class Decisions(unittest.TestCase):
    def features(self, **kw):
        f = {"key": "k", "service": "shop_api", "architecture": "shop", "level": "error", "level_weight": 0.7,
             "new": True, "regression": False, "events_1h": 40, "events_24h": 40, "users": 12, "total": 40,
             "has_stacktrace": True, "in_app_frames": 2, "source": "sentry",
             "burst": {"burst": True, "ratio": 30, "window": 40, "expected": 0, "onset": NOW},
             "long_term": {"rising": False, "chronic": False, "trend": 0, "daily": [0] * 14, "days_active": 1},
             "cascade": False, "cascade_root": False, "service_burst": True, "failed_tasks_1h": 0}
        f.update(kw)
        return f

    def test_fix_when_code_defect(self):
        self.assertEqual(rules({}, self.features(), API, {})["action"], "fix")

    def test_report_without_repo(self):
        self.assertEqual(rules({}, self.features(), DB, {})["action"], "report")

    def test_downstream_of_cascade_is_not_fixed(self):
        d = rules({}, self.features(cascade=True, cascade_root=False), API, {})
        self.assertEqual(d["action"], "report")

    def test_memory_pressure(self):
        d = rules({}, self.features(in_app_frames=0), API, {"memory_limit_ratio": 0.97})
        self.assertNotEqual(d["action"], "fix")

    def test_noise(self):
        f = self.features(level="info", level_weight=0.15, new=False, users=0, service_burst=False,
                          burst={"burst": False, "ratio": 1, "window": 1, "expected": 1, "onset": None})
        self.assertEqual(rules({}, f, API, {})["action"], "ignore")

    def test_policy_cap(self):
        self.assertEqual(cap("fix", "report"), "report")
        self.assertEqual(cap("fix", "observe"), "watch")
        self.assertEqual(cap("watch", "push"), "watch")


class FakeStairway(BaseHTTPRequestHandler):
    calls = []
    root = None           # when set, create_experiment makes the folder like stairway does

    def log_message(self, *a):
        pass

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeStairway.calls.append((self.headers.get("Authorization"), msg))
        name, args = msg["params"]["name"], msg["params"]["arguments"]
        if name == "create_experiment" and FakeStairway.root:
            os.makedirs(os.path.join(FakeStairway.root, args["name"]))
        text = "Created /srv/%s." % args.get("name") if name == "create_experiment" else json.dumps(
            {"name": args["name"], "running": True, "session_url": "https://claude.ai/code/session_x"})
        body = json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                           "result": {"content": [{"type": "text", "text": text}], "isError": False}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Served(unittest.TestCase):
    def setUp(self):
        FakeStairway.calls[:] = []

    def serve(self, handler):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return "http://127.0.0.1:%d" % httpd.server_address[1]

    def burst_of(self, engine, project, issue_id, n=15):
        now = time.time()
        for k in range(n):
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60 + k))
            engine.sentry_webhook({"Sentry-Hook-Resource": "event_alert"}, event_alert(project, issue_id, ts))


class EndToEnd(Served):
    def test_dry_run_prepares_a_brief(self):
        c = cfg()
        e = Engine(c, Store(":memory:"), swarm_with(c, API, WEB, DB))
        self.burst_of(e, "api", "77")
        e.tick()
        d = e.store.decisions()[0]
        self.assertEqual((d["action"], d["applied"]), ("fix", "fix"))
        w = e.store.works()[0]
        self.assertEqual((w["status"], w["kind"], w["ref"]), ("dry-run", "fix", "aitomaton-shop-api-77"))
        self.assertIn("gh repo clone acme/api", w["brief"])
        self.assertIn("cart.py:42", w["brief"])
        self.assertIn("AITOMATON-RESULT", w["brief"])
        e.tick()                                        # nothing new: no second decision
        self.assertEqual(len(e.store.decisions()), 1)
        s = e.status()
        json.dumps(s)
        self.assertEqual(s["summary"]["works"]["dry-run"], 1)
        self.assertTrue(any(f["kind"] == "burst" for f in s["findings"]))

    def test_dispatch_and_callback(self):
        url = self.serve(FakeStairway)
        c = cfg(AITOMATON_DISPATCH="1", STAIRWAY_URL=url, STAIRWAY_TOKEN="t" * 20,
                AITOMATON_CALLBACK_SECRET="c" * 20)
        e = Engine(c, Store(":memory:"), swarm_with(c, API, WEB, DB))
        self.burst_of(e, "api", "77")
        e.tick()
        w = e.store.works()[0]
        self.assertEqual(w["status"], "running")
        auth, msg = FakeStairway.calls[0]
        self.assertEqual(auth, "Bearer " + "t" * 20)
        self.assertEqual(msg["params"]["arguments"]["permission_mode"], "acceptEdits")
        self.assertEqual(msg["params"]["arguments"]["callback_secret"], "c" * 20)
        e.tick()                                        # follows up via experiment_info
        self.assertEqual(e.store.works()[0]["url"], "https://claude.ai/code/session_x")

        app = self.serve(make_handler(e))
        body = json.dumps({"experiment": w["ref"], "event": "reply", "text": "Done.\nAITOMATON-RESULT: "
                           '{"status": "pr", "url": "https://github.com/acme/api/pull/3", "summary": "guard price"}'})
        bad = urllib.request.Request(app + "/hooks/stairway", body.encode(), {"X-Stairway-Signature": "sha256=x"})
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(bad)
        sig = "sha256=" + hmac.new(b"c" * 20, body.encode(), hashlib.sha256).hexdigest()
        urllib.request.urlopen(urllib.request.Request(app + "/hooks/stairway", body.encode(),
                                                      {"X-Stairway-Signature": sig}))
        w = e.store.works()[0]
        self.assertEqual((w["status"], w["result_url"]), ("done", "https://github.com/acme/api/pull/3"))
        status = json.loads(urllib.request.urlopen(app + "/status.json").read())
        self.assertEqual(status["summary"]["works"]["done"], 1)
        self.assertIn(b"aitomaton", urllib.request.urlopen(app + "/").read())

    def test_queue_limit(self):
        url = self.serve(FakeStairway)
        c = cfg(AITOMATON_DISPATCH="1", STAIRWAY_URL=url, STAIRWAY_TOKEN="t" * 20, MAX_RUNNING_WORKS="1")
        e = Engine(c, Store(":memory:"), swarm_with(c, API, WEB, DB))
        self.burst_of(e, "api", "77")
        self.burst_of(e, "api", "78")
        e.tick()
        self.assertEqual(sorted(w["status"] for w in e.store.works()), ["queued", "running"])

    def test_alert_for_service_without_repo_is_reported(self):
        c = cfg()
        e = Engine(c, Store(":memory:"), swarm_with(c, API, WEB, DB))
        e.alertmanager_webhook({"alerts": [{"status": "firing", "labels": {
            "alertname": "DiskFull", "service": "shop_db", "severity": "critical"}, "annotations": {}}]})
        e.tick()
        d = e.store.decisions()[0]
        self.assertEqual(d["key"], "prom:DiskFull:shop_db")
        self.assertIn(d["applied"], ("report", "watch"))
        self.assertNotEqual(d["action"], "fix")


class FakeTiers(BaseHTTPRequestHandler):
    """laya's /predict and an OpenAI-compatible /chat/completions in one fake."""
    llm_calls = []
    urgent, code = 0.8, 0.52      # code defect near 0.5: laya is unsure

    def log_message(self, *a):
        pass

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/predict":
            out = {"answers": {"urgent": {"noul": self.urgent}, "code_defect": {"noul": self.code}},
                   "routing": {"model": "english"}}
        else:
            FakeTiers.llm_calls.append(msg)
            out = {"choices": [{"message": {"content": 'Sure: {"action": "fix", "confidence": 0.9, '
                                                       '"code_defect": 0.85, "rationale": "missing key guard"}'}}]}
        body = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Chain(unittest.TestCase):
    def test_escalates_to_llm_when_laya_is_unsure(self):
        from aitomaton.deciders import Decider
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeTiers)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        url = "http://127.0.0.1:%d" % httpd.server_address[1]
        d = Decider(cfg(AITOMATON_DECIDER="chain", LAYA_URL=url, LLM_BASE_URL=url, LLM_MODEL="m"))
        out = d.decide({"title": "KeyError"}, Decisions().features(), API, {})
        self.assertEqual([t["decider"] for t in out["trail"]], ["rules", "laya", "llm"])
        self.assertEqual((out["action"], out["decider"]), ("fix", "llm"))
        self.assertIn("Earlier tiers said", FakeTiers.llm_calls[-1]["messages"][1]["content"])

    def test_jev_tier_settles_before_the_llm(self):
        from aitomaton.deciders import Decider
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeTiers)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        url = "http://127.0.0.1:%d" % httpd.server_address[1]
        FakeTiers.llm_calls[:] = []
        d = Decider(cfg(AITOMATON_DECIDER="chain", AITOMATON_CHAIN="laya,jev,llm", LAYA_URL=url,
                        LLM_BASE_URL=url, LLM_MODEL="big", JEV_MODEL="typesafe/jev-router"))
        out = d.decide({"title": "KeyError"}, Decisions().features(), API, {})
        self.assertEqual([t["decider"] for t in out["trail"]], ["rules", "laya", "jev"])   # confident: stop
        self.assertEqual(FakeTiers.llm_calls[-1]["model"], "typesafe/jev-router")
        self.assertEqual(out["decider"], "jev")

    def test_a_down_tier_hands_over_to_the_next(self):
        from aitomaton.deciders import Decider
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeTiers)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        url = "http://127.0.0.1:%d" % httpd.server_address[1]
        d = Decider(cfg(AITOMATON_DECIDER="chain", AITOMATON_CHAIN="laya,jev", LAYA_URL="http://127.0.0.1:9",
                        JEV_MODEL="typesafe/jev-router", JEV_BASE_URL=url))
        out = d.decide({"title": "KeyError"}, Decisions().features(), API, {})
        self.assertEqual([t["decider"] for t in out["trail"]], ["rules", "error", "jev"])

    def test_tier_failure_falls_back(self):
        from aitomaton.deciders import Decider
        d = Decider(cfg(AITOMATON_DECIDER="chain", LAYA_URL="http://127.0.0.1:9"))
        out = d.decide({"title": "KeyError"}, Decisions().features(), API, {})
        self.assertEqual((out["decider"], out["action"]), ("rules", "fix"))
        self.assertEqual(out["trail"][-1]["decider"], "error")


class FakeGitHub(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeGitHub.calls.append((self.path, self.headers.get("Authorization"), msg))
        body = json.dumps({"html_url": "https://github.com/acme/api/pull/9"}).encode()
        self.send_response(201)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Credentials(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.secrets = tempfile.mkdtemp()
        with open(os.path.join(self.secrets, "aitomaton_git_shop"), "w") as fh:
            fh.write("tok-from-secret\n")

    def resolve(self, labels, env):
        from aitomaton.credentials import resolve
        return resolve(descriptor("svc", labels), environ=env, secrets_dir=self.secrets)

    def test_order(self):
        repo = {"aitomaton.repo": "Acme-Corp/core"}
        env = {"AITOMATON_GIT_TOKEN": "global", "AITOMATON_GIT_TOKEN_ACME_CORP": "owner",
               "AITOMATON_GIT_TOKEN_OTHER": "named"}
        self.assertEqual(self.resolve(dict(repo, **{"aitomaton.git.credential": "shop"}), env)["token"],
                         "tok-from-secret")
        self.assertEqual(self.resolve(dict(repo, **{"aitomaton.git.credential": "other"}), env)["token"], "named")
        self.assertEqual(self.resolve(repo, env)["token"], "owner")
        self.assertEqual(self.resolve({"aitomaton.repo": "acme/x"}, env)["token"], "global")
        self.assertIsNone(self.resolve({"aitomaton.repo": "acme/x"}, {})["token"])

    def test_missing_named_credential_does_not_fall_back(self):
        r = self.resolve({"aitomaton.repo": "acme/x", "aitomaton.git.credential": "nope"},
                         {"AITOMATON_GIT_TOKEN": "global"})
        self.assertIsNone(r["token"])
        self.assertIn("AITOMATON_GIT_TOKEN_NOPE", r["source"])

    def test_literal_token_in_label_is_refused(self):
        r = self.resolve({"aitomaton.repo": "acme/x", "aitomaton.git.credential": "ghp_" + "a" * 36}, {})
        self.assertIsNone(r["token"])
        self.assertTrue(r["source"].startswith("refused"))
        self.assertNotIn("ghp_", r["source"])

    def test_author(self):
        r = self.resolve({"aitomaton.repo": "a/b", "aitomaton.git.author": "Bot <bot@x.org>"}, {})
        self.assertEqual(r["author"], "Bot <bot@x.org>")


def bare_repo(tmp, name="acme/api"):
    """A local 'remote' with one commit on main, reachable as file://<tmp>/remote/acme/api.git."""
    import subprocess
    src = os.path.join(tmp, "src")
    os.makedirs(src)
    run = lambda *a, cwd=src: subprocess.run(["git"] + list(a), cwd=cwd, check=True, capture_output=True)
    run("init", "-q")
    run("checkout", "-q", "-b", "main")
    with open(os.path.join(src, "cart.py"), "w") as fh:
        fh.write("def total(item, qty):\n    return item['price'] * qty\n")
    run("add", ".")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    remote = os.path.join(tmp, "remote", name + ".git")
    os.makedirs(os.path.dirname(remote))
    run("clone", "-q", "--bare", src, remote, cwd=tmp)
    return "file://" + os.path.join(tmp, "remote"), remote


class Workspace(unittest.TestCase):
    def test_prepare_push_cleanup(self):
        import subprocess
        import tempfile
        from aitomaton import workspace
        tmp = tempfile.mkdtemp()
        base, remote = bare_repo(tmp)
        folder = os.path.join(tmp, "exp")
        os.makedirs(folder)
        clone = workspace.prepare(folder, "acme/api", "main", "aitomaton/fix-1",
                                  {"token": "s3cr3t-token", "author": "Bot <bot@x.org>"}, base=base)
        git = lambda *a: subprocess.run(["git"] + list(a), cwd=clone, check=True, capture_output=True,
                                        text=True).stdout.strip()
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD"), "aitomaton/fix-1")
        self.assertEqual(git("config", "user.email"), "bot@x.org")
        cred = os.path.join(folder, ".aitomaton", "git-credentials")
        self.assertEqual(os.stat(cred).st_mode & 0o777, 0o600)
        self.assertNotIn("s3cr3t-token", open(os.path.join(clone, ".git", "config")).read())
        self.assertNotIn("s3cr3t-token", git("remote", "-v"))
        with open(os.path.join(clone, "cart.py"), "a") as fh:
            fh.write("# fixed\n")
        git("commit", "-qam", "fix")
        git("push", "-q", "-u", "origin", "aitomaton/fix-1")
        self.assertIn("aitomaton/fix-1", subprocess.run(["git", "branch"], cwd=remote, capture_output=True,
                                                        text=True).stdout)
        workspace.cleanup(folder)
        self.assertFalse(os.path.exists(cred))
        self.assertTrue(os.path.isdir(clone))

    def test_errors_are_redacted(self):
        import tempfile
        from aitomaton import workspace
        tmp = tempfile.mkdtemp()
        with self.assertRaises(workspace.WorkspaceError) as e:
            workspace.prepare(tmp, "acme/nope", "main", "b", {"token": "s3cr3t-token", "author": "a <b>"},
                              base="file://" + tmp + "/x-access-token:s3cr3t-token@nowhere")
        self.assertNotIn("s3cr3t-token", str(e.exception))


class PreparedFix(Served):
    def test_clone_with_service_credential_then_aitomaton_opens_the_pr(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        base, remote = bare_repo(tmp)
        root = os.path.join(tmp, "experiments")
        os.makedirs(root)
        FakeStairway.root = root
        self.addCleanup(setattr, FakeStairway, "root", None)
        stairway, gh = self.serve(FakeStairway), self.serve(FakeGitHub)
        c = cfg(AITOMATON_DISPATCH="1", STAIRWAY_URL=stairway, STAIRWAY_TOKEN="t" * 20,
                STAIRWAY_EXPERIMENTS_ROOT=root, AITOMATON_GIT_BASE=base, GITHUB_API=gh,
                AITOMATON_GIT_TOKEN_ACME="acme-token-123")
        e = Engine(c, Store(":memory:"), swarm_with(c, API, WEB, DB))
        self.burst_of(e, "api", "77")
        e.tick()
        w = e.store.works()[0]
        self.assertEqual(w["status"], "running", w["log"])
        tools = [m["params"]["name"] for _, m in FakeStairway.calls]
        self.assertEqual(tools[:2], ["create_experiment", "start_experiment"])
        self.assertFalse(FakeStairway.calls[0][1]["params"]["arguments"]["start"])
        folder = os.path.join(root, w["ref"])
        self.assertTrue(os.path.isdir(os.path.join(folder, "repo", ".git")))
        self.assertIn("already cloned in `./repo`", w["brief"])
        self.assertNotIn("acme-token-123", w["brief"] + json.dumps(e.status()))
        self.assertIn("env AITOMATON_GIT_TOKEN_ACME", json.dumps(e.status()["services"]))

        e.stairway_callback({"experiment": w["ref"], "event": "reply", "text": 'done\nAITOMATON-RESULT: '
                             '{"status": "branch", "branch": "aitomaton/fix-77", "title": "Guard missing price", '
                             '"body": "Cause: ...", "summary": "guarded"}'})
        w = e.store.works()[0]
        self.assertEqual((w["status"], w["result_url"]), ("done", "https://github.com/acme/api/pull/9"))
        path, auth, pr = FakeGitHub.calls[-1]
        self.assertEqual((path, auth), ("/repos/acme/api/pulls", "Bearer acme-token-123"))
        self.assertEqual((pr["head"], pr["base"], pr["title"]), ("aitomaton/fix-77", "main", "Guard missing price"))
        self.assertFalse(os.path.exists(os.path.join(folder, ".aitomaton")))


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.123456789Z", time.gmtime(ts))


def node(nid, host, role="worker", state="ready", reach=None, engine="24.0.2"):
    n = {"ID": nid, "Spec": {"Role": role, "Availability": "active"}, "Status": {"State": state, "Addr": "10.0.0.1"},
         "Description": {"Hostname": host, "Engine": {"EngineVersion": engine},
                         "Resources": {"NanoCPUs": 4e9, "MemoryBytes": 8 * 2 ** 30}}}
    if role == "manager":
        n["ManagerStatus"] = {"Reachability": reach or "reachable", "Leader": nid == "m1"}
    return n


def service(sid, name, replicas=1, updated=0, **spec):
    return {"ID": sid, "UpdatedAt": _iso(updated), "Spec": dict({"Name": name, "Mode": {"Replicated": {
        "Replicas": replicas}}, "TaskTemplate": {"ContainerSpec": {"Image": "traefik:3.1@sha256:x"},
                                                 "Resources": {"Limits": {"MemoryBytes": 2 ** 28}}}}, **spec)}


def task(tid, sid, state, node_id="w1", err=None, at=None):
    return {"ID": tid, "ServiceID": sid, "NodeID": node_id, "DesiredState": "running" if state == "running" else "shutdown",
            "Status": {"State": state, "Err": err, "Timestamp": _iso(at or time.time() - 60)}}


def ts(v):
    from aitomaton.swarm import _ts
    return _ts(v)


class SwarmHealth(unittest.TestCase):
    def check(self, nodes, services, tasks):
        return swarm_health.check(nodes, services, tasks, time.time(), ts)

    def test_classify(self):
        self.assertEqual(swarm_health.classify("task: non-zero exit (137): OOMKilled"), "oom")
        self.assertEqual(swarm_health.classify("No such image: acme/api:1.5"), "image_pull")
        self.assertEqual(swarm_health.classify("no suitable node (insufficient resources on 2 nodes)"), "unschedulable")
        self.assertEqual(swarm_health.classify("Bind for 0.0.0.0:80 failed: port is already allocated"), "port_conflict")
        self.assertEqual(swarm_health.classify("task: non-zero exit (1)"), "crash_loop")

    def test_conditions(self):
        nodes = [node("m1", "boss", "manager"), node("w1", "box", state="down", engine="28.0.1")]
        services = [service("s1", "web_app", 3), service("s2", "edge_traefik"), service("s3", "big_job")]
        tasks = [task("t1", "s1", "running", "m1"), task("t2", "s1", "shutdown", "w1")]
        tasks += [task("o%d" % k, "s2", "failed", err="task: non-zero exit (137): OOMKilled") for k in range(3)]
        tasks += [task("u%d" % k, "s3", "rejected", err="no suitable node (insufficient resources)") for k in range(4)]
        h = self.check(nodes, services, tasks)
        kinds = {c["key"]: c for c in h["conditions"]}
        self.assertEqual(kinds["swarm:box:node_down"]["service"], None)
        self.assertEqual(kinds["swarm:edge_traefik:oom"]["detail"]["failed_1h"], 3)
        self.assertEqual(kinds["swarm:web_app:under_replicated"]["detail"]["cause"], "node box is down")
        self.assertIn("swarm:big_job:unschedulable", kinds)
        self.assertEqual(h["managers"], {"total": 1, "reachable": 1, "quorum": 1})
        self.assertEqual({a["kind"] for a in h["advisories"]}, {"single_manager", "engine_skew"})

    def test_quorum_lost_and_grace(self):
        nodes = [node("m1", "a", "manager"), node("m2", "b", "manager", reach="unreachable"),
                 node("m3", "c", "manager", reach="unreachable")]
        h = self.check(nodes, [service("s1", "fresh", 2, updated=time.time() - 30)], [])
        keys = {c["key"] for c in h["conditions"]}
        self.assertIn("swarm:managers:quorum_lost", keys)
        self.assertIn("swarm:b:manager_unreachable", keys)
        self.assertNotIn("swarm:fresh:under_replicated", keys)      # still rolling out


TRAEFIK = descriptor("edge_traefik", {"com.docker.stack.namespace": "edge"})       # third party, no repo
OWNED = descriptor("shop_worker", {"com.docker.stack.namespace": "shop", "aitomaton.repo": "acme/worker",
                                   "aitomaton.infra.repo": "acme/shop-deploy", "aitomaton.infra.path": "stack.yml",
                                   "aitomaton.infra.policy": "pr"})


class Infrastructure(Served):
    def engine(self, **env):
        c = cfg(AITOMATON_INFRA_REPO="https://github.com/acme/infra", AITOMATON_GIT_TOKEN="g" * 12, **env)
        return Engine(c, Store(":memory:"), swarm_with(c, TRAEFIK, OWNED))

    def health(self, e, *conds, now=None):
        out = []
        for kind, subject, service_ in conds:
            out.append(swarm_health._cond(kind, subject, service_, swarm_health.LEVEL[kind], "%s %s" % (subject, kind),
                                          None, {"service": service_}, failures=["t-%s-%d" % (subject, k)
                                                                                for k in range(3)]
                                          if kind == "oom" else None))
        e.swarm.health = {"conditions": out, "advisories": [], "nodes": [], "managers": {}, "error": None}
        e._ingest_health(now or time.time())

    def test_third_party_oom_goes_to_the_infra_repo(self):
        gh = self.serve(FakeGitHub)
        FakeGitHub.calls[:] = []
        e = self.engine(AITOMATON_DISPATCH="1", GITHUB_API=gh)
        self.health(e, ("oom", "edge_traefik", "edge_traefik"), ("node_down", "box", None))
        e.tick()
        self.assertEqual(e.target(e.store.issue("swarm:edge_traefik:oom"))["repo"], "acme/infra")
        paths = sorted(p for p, _, _ in FakeGitHub.calls)
        self.assertEqual(paths, ["/repos/acme/infra/issues"] * 2)    # the OOM and the dead node, as reports
        node_issue = e.store.issue("swarm:box:node_down")
        self.assertEqual((node_issue["service"], node_issue["architecture"]), ("swarm", "swarm"))
        st = e.status()["infrastructure"]
        self.assertEqual(st["outlet"]["repo"], "acme/infra")
        self.assertEqual(len(st["conditions"]), 2)

        # still firing two hours later: decided again, but not reported twice
        later = time.time() + 7200
        self.health(e, ("oom", "edge_traefik", "edge_traefik"), ("node_down", "box", None), now=later)
        e.tick(later)
        self.assertEqual(len(FakeGitHub.calls), 2)
        # it stops, then comes back: a new episode, a new report
        self.health(e, ("node_down", "box", None), now=later + 60)
        self.assertEqual(e.store.issue("swarm:edge_traefik:oom")["status"], "resolved")
        back = later + 7200
        self.health(e, ("oom", "edge_traefik", "edge_traefik"), ("node_down", "box", None), now=back)
        e.tick(back)
        self.assertEqual(len(FakeGitHub.calls), 3)

    def test_service_infra_label_and_fix_brief(self):
        e = self.engine(AITOMATON_GIT_TOKEN_ACME="a" * 12)
        self.health(e, ("oom", "shop_worker", "shop_worker"))
        issue = e.store.issue("swarm:shop_worker:oom")
        t = e.target(issue)
        self.assertEqual((t["repo"], t["path"], t["policy"], t["outlet"]), ("acme/shop-deploy", "stack.yml", "pr",
                                                                            "service"))
        e.tick()
        w = e.store.works()[0]
        self.assertEqual((w["kind"], w["status"], w["ref"]), ("fix", "dry-run", "aitomaton-infra-shop-worker-oom"))
        self.assertIn("infrastructure repository", w["brief"])
        self.assertIn("aitomaton/fix-shop_worker-oom", w["brief"])
        self.assertIn("docker stack config", w["brief"])
        self.assertEqual(e.status()["infrastructure"]["service_outlets"], ["acme/shop-deploy"])

    def test_node_down_is_never_fixed(self):
        e = self.engine(AITOMATON_INFRA_POLICY="push")
        self.health(e, ("node_down", "box", None), ("quorum_lost", "managers", None))
        e.tick()
        self.assertEqual({d["applied"] for d in e.store.decisions()}, {"report"})

    def test_off(self):
        e = self.engine(AITOMATON_INFRA="0")
        self.assertFalse(e.status()["infrastructure"]["enabled"])
        self.assertEqual(fix_branch({"key": "sentry:api:77"}), "aitomaton/fix-77")


class FakeActions:
    def __init__(self):
        self.calls = []

    def run(self, step, sid):
        self.calls.append((step, sid))
        return "did %s" % step


class Emergencies(Served):
    def test_layers(self):
        self.assertEqual(emergency.parse_policy("push+act"), {"deliver": "push", "act": True})
        t = {"policy": "pr", "policy_emergency": "push+act"}
        self.assertEqual(emergency.layer(t, None, None)["label"], "pr")
        self.assertEqual(emergency.layer(t, None, {"reason": "x"})["label"], "push+act (emergency)")
        self.assertFalse(emergency.layer(t, {"act": False}, {"reason": "x"})["act"])          # the service vetoes
        self.assertEqual(emergency.layer({"policy": "observe"}, None, {"reason": "x"})["deliver"], "report")
        self.assertEqual(emergency.layer({"policy": "pr"}, None, {"reason": "x"}, "act")["label"], "pr+act (emergency)")

    def test_detect(self):
        c, now = cfg(), time.time()
        app = {"replicas": {"running": 0, "desired": 2}, "updated_at": now - 7200}
        f = {"burst": {"burst": False}, "level": "error"}
        down = {"source": "swarm", "service": "web", "detail": {"kind": "under_replicated", "running": 0, "desired": 2},
                "flags": {"fired_at": now - 600}}
        self.assertEqual(emergency.detect(down, f, app, now, c)["runbook"], ["restart"])
        self.assertEqual(emergency.detect(down, f, dict(app, updated_at=now - 900), now, c)["runbook"],
                         ["rollback", "restart"])                                        # broke right after a deploy
        old = dict(down, flags={"fired_at": now - 86400 * 20})
        self.assertIsNone(emergency.detect(old, f, app, now, c))                         # weeks old: a report
        node = dict(down, detail=dict(down["detail"], cause="node box is down"))
        self.assertIsNone(emergency.detect(node, f, app, now, c))                        # the node's emergency
        quorum = dict(old, detail={"kind": "quorum_lost"})
        self.assertEqual(emergency.detect(quorum, f, None, now, c)["runbook"], [])       # always, for humans
        burst = {"source": "sentry", "service": "web", "detail": {}, "flags": {}, "first_seen": now - 120}
        hot = {"burst": {"burst": True}, "level": "error", "service_burst": True}
        self.assertEqual(emergency.detect(burst, hot, dict(app, updated_at=now - 600), now, c)["runbook"], ["rollback"])
        self.assertIsNone(emergency.detect(burst, hot, app, now, c))                     # no recent deploy

    def outage(self, **env):
        c = cfg(AITOMATON_INFRA_REPO="acme/infra", AITOMATON_INFRA_POLICY="pr", **env)
        web = dict(descriptor("shop_web", {"com.docker.stack.namespace": "shop"}), id="svc-web",
                   replicas={"running": 0, "desired": 2}, updated_at=time.time() - 86400)
        e = Engine(c, Store(":memory:"), swarm_with(c, web))
        e.actions = FakeActions()
        e.swarm.health = {"conditions": [], "advisories": [], "nodes": [], "managers": {}, "error": None}
        e._ingest_health(time.time() - 60)      # the baseline: all was well when aitomaton started
        cond = swarm_health._cond("under_replicated", "shop_web", "shop_web", "error", "shop_web runs 0 of 2", None,
                                  {"service": "shop_web", "running": 0, "desired": 2})
        e.swarm.health = {"conditions": [cond], "advisories": [], "nodes": [], "managers": {}, "error": None}
        return e

    def test_runbook_runs_once_per_step_and_the_emergency_closes(self):
        e = self.outage(AITOMATON_INFRA_POLICY_EMERGENCY="push+act", AITOMATON_ACT="1", AITOMATON_DISPATCH="1")
        now = time.time()
        e._ingest_health(now)
        e.tick(now)
        self.assertEqual(e.actions.calls, [("restart", "svc-web")])
        d = e.store.decisions()[0]
        self.assertEqual((d["policy"], d["applied"]), ("push+act (emergency)", "report"))
        st = e.status()
        self.assertEqual(st["summary"]["emergencies"], 1)
        self.assertEqual(st["emergencies"][0]["actions"][0]["status"], "done")
        e.tick(now + 30)
        self.assertEqual(len(e.actions.calls), 1)                 # waits for the step to work
        e.tick(now + 400)
        self.assertEqual(len(e.actions.calls), 1)                 # one step in this runbook: exhausted
        em = [w for w in e.store.works() if w["kind"] == "emergency"][0]
        self.assertIn("runbook exhausted", json.dumps(em["log"]))
        e.swarm.health = dict(e.swarm.health, conditions=[])
        e._ingest_health(now + 500)
        e.tick(now + 500)
        em = [w for w in e.store.works() if w["kind"] == "emergency"][0]
        self.assertEqual((em["status"], em["summary"]), ("done", "resolved"))

    def test_without_act_it_is_a_dry_run_and_a_report(self):
        e = self.outage(AITOMATON_INFRA_POLICY_EMERGENCY="pr+act", AITOMATON_DISPATCH="1")    # AITOMATON_ACT off
        e._ingest_health(time.time())
        e.tick()
        self.assertEqual(e.actions.calls, [])
        acts = [w for w in e.store.works() if w["kind"] == "act"]
        self.assertEqual((acts[0]["status"], acts[0]["ref"]), ("dry-run", "restart"))
        e = self.outage()                                      # no emergency layer: the normal one, no actions
        e._ingest_health(time.time())
        e.tick()
        self.assertEqual([w["kind"] for w in e.store.works()].count("act"), 0)
        self.assertEqual(e.store.decisions()[0]["policy"], "pr (emergency)")

    def test_broken_before_aitomaton_looked_is_not_an_emergency(self):
        e = self.outage(AITOMATON_INFRA_POLICY_EMERGENCY="push+act", AITOMATON_ACT="1", AITOMATON_DISPATCH="1")
        e._health_read = False                  # as if this were the first read after a start
        e._ingest_health(time.time())
        e.tick()
        self.assertEqual((e.emergencies, e.actions.calls), ({}, []))
        self.assertTrue(e.store.issue("swarm:shop_web:under_replicated")["flags"]["preexisting"])

    def test_raise_memory_through_the_docker_api(self):
        calls = []

        def fake(sock, method, path, payload=None, timeout=30):
            calls.append((method, path, payload))
            return {"ID": "s1", "Version": {"Index": 7}, "Spec": {"Name": "edge_traefik", "TaskTemplate": {
                "Resources": {"Limits": {"MemoryBytes": 256 * 2 ** 20}}}}} if method == "GET" else None
        old = emergency.unix_request
        emergency.unix_request = fake
        self.addCleanup(setattr, emergency, "unix_request", old)
        msg = emergency.SwarmActions(cfg()).run("raise_memory", "s1")
        self.assertEqual(msg, "raised edge_traefik's memory limit from 256 to 384 MiB")
        method, path, spec = calls[-1]
        self.assertEqual((method, path), ("POST", "/services/s1/update?version=7"))
        self.assertEqual(spec["TaskTemplate"]["Resources"]["Limits"]["MemoryBytes"], 384 * 2 ** 20)
        with self.assertRaises(ValueError):
            emergency.SwarmActions(cfg()).run("rm -rf", "s1")


class FakeTelegram(BaseHTTPRequestHandler):
    calls = []
    fail = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeTelegram.calls.append((self.path, msg))
        body = json.dumps({"ok": not FakeTelegram.fail, "result": {}}).encode()
        self.send_response(403 if FakeTelegram.fail else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        body = json.dumps({"ok": True, "result": [
            {"message": {"chat": {"id": 42, "first_name": "Ada"}}},
            {"message": {"chat": {"id": -1009, "title": "ops"}, "message_thread_id": 5, "is_topic_message": True}}]
        }).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Telegram(Served):
    TOKEN = "123456:AAH-secret-bot-token"

    def setUp(self):
        super().setUp()
        FakeTelegram.calls[:], FakeTelegram.fail = [], False
        self.api = self.serve(FakeTelegram)

    def test_chats_and_failures_are_redacted(self):
        self.assertEqual(parse_chats("42, -1009/5"), [("42", None), ("-1009", 5)])
        n = Notifier(cfg(AITOMATON_TELEGRAM_TOKEN=self.TOKEN, AITOMATON_TELEGRAM_API=self.api,
                         AITOMATON_TELEGRAM_CHAT="42,-1009/5"))
        self.assertEqual(n.telegram_chats(), {"42": "Ada", "-1009/5": "ops"})
        self.assertEqual(n.send("emergency", "hi"), "notified telegram 42; notified telegram -1009")
        self.assertEqual(FakeTelegram.calls[1][1]["message_thread_id"], 5)
        self.assertEqual(FakeTelegram.calls[0][0], "/bot%s/sendMessage" % self.TOKEN)
        self.assertIsNone(n.send("report", "not subscribed"))                # default events: emergency, fix
        FakeTelegram.fail = True
        out = n.send("fix", "x")
        self.assertIn("failed", out)
        self.assertNotIn("secret-bot-token", out)

    def test_token_from_a_docker_secret(self):
        import tempfile
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "aitomaton_telegram_token"), "w") as fh:
            fh.write(self.TOKEN + "\n")
        self.assertEqual(Notifier(cfg(AITOMATON_SECRETS_DIR=d)).token, self.TOKEN)

    def test_an_emergency_reaches_the_global_and_the_service_chats(self):
        c = cfg(AITOMATON_TELEGRAM_TOKEN=self.TOKEN, AITOMATON_TELEGRAM_API=self.api, AITOMATON_TELEGRAM_CHAT="42",
                AITOMATON_DISPATCH="1")
        web = dict(descriptor("shop_web", {"com.docker.stack.namespace": "shop", "aitomaton.notify.telegram": "-77"}),
                   id="svc-web", replicas={"running": 0, "desired": 2}, updated_at=time.time() - 86400)
        e = Engine(c, Store(":memory:"), swarm_with(c, web))
        e.swarm.health = {"conditions": [], "advisories": [], "nodes": [], "managers": {}, "error": None}
        e._ingest_health(time.time() - 60)
        e.swarm.health = dict(e.swarm.health, conditions=[swarm_health._cond(
            "under_replicated", "shop_web", "shop_web", "error", "shop_web runs 0 of 2", None,
            {"service": "shop_web", "running": 0, "desired": 2})])
        e._ingest_health(time.time())
        e.tick()
        self.assertEqual(sorted(m["chat_id"] for _, m in FakeTelegram.calls), ["-77", "42"])
        self.assertIn("EMERGENCY shop_web", FakeTelegram.calls[0][1]["text"])
        self.assertEqual(e.status()["config"]["notify"], {"telegram": 1, "webhook": False,
                                                          "events": ["emergency", "fix"]})


class McpServer(Served):
    def engine(self, **env):
        c = cfg(AITOMATON_MCP_TOKEN="ada:tok-ada-123456,tok-anon-123456", **env)
        e = Engine(c, Store(":memory:"), swarm_with(c, API, WEB, DB))
        handler = make_handler(e)
        return e, self.serve(handler)

    def rpc(self, url, method, params=None, token="tok-ada-123456", mid=1):
        req = urllib.request.Request(url + "/mcp", json.dumps(
            {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}).encode(),
            {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def tool(self, url, name, **args):
        r = self.rpc(url, "tools/call", {"name": name, "arguments": args})["result"]
        return r["structuredContent"], r["isError"]

    def test_tokens(self):
        c = cfg(AITOMATON_MCP_TOKEN="ada:tok-ada-123456, tok-anon-123456")
        self.assertEqual(caller(c, "Bearer tok-ada-123456"), "ada")
        self.assertEqual(caller(c, "Bearer tok-anon-123456"), "mcp")
        self.assertIsNone(caller(c, "Bearer nope"))
        self.assertIsNone(caller(c, None))

    def test_auth_and_handshake(self):
        e, url = self.engine()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.rpc(url, "initialize", token="wrong")
        self.assertEqual(ctx.exception.code, 401)
        init = self.rpc(url, "initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertEqual(init["serverInfo"]["name"], "aitomaton")
        names = {t["name"] for t in self.rpc(url, "tools/list")["result"]["tools"]}
        self.assertEqual(names, {"status", "service_status", "list_issues", "get_issue", "report_issue",
                                 "add_observation", "resolve_report"})
        c = cfg()
        off = self.serve(make_handler(Engine(c, Store(":memory:"), swarm_with(c))))
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.rpc(off, "initialize")
        self.assertEqual(ctx.exception.code, 404)

    def test_a_report_is_investigated_merged_decided_and_resolved(self):
        e, url = self.engine()
        self.burst_of(e, "api", "77")
        e.tick()
        out, err = self.tool(url, "report_issue", title="checkout takes 20 s", service="api", kind="slowness",
                             severity="high", description="Since 10:00 the cart page hangs before loading.")
        self.assertFalse(err, out)
        key = out["key"]
        self.assertTrue(key.startswith("user:shop_api:"))
        self.assertTrue(out["investigation"]["corroborated"])
        self.assertEqual(out["investigation"]["related"][0]["key"], "sentry:api:77")
        e.tick()
        d = e.store.last_decision(key)
        self.assertIn(d["applied"], ("report", "fix"))
        self.assertIn("reported (slowness)", d["rationale"])
        again, _ = self.tool(url, "report_issue", title="cart is slow", service="shop_api", kind="slowness",
                             description="Same here", reporter="bob")
        self.assertEqual((again["key"], again["merged"], again["reporters"]), (key, True, 2))
        got, _ = self.tool(url, "get_issue", key=key)
        self.assertEqual(got["detail"]["reports"][0]["reporter"], "ada")
        self.assertTrue(got["decisions"])
        listed, _ = self.tool(url, "list_issues", source="user")
        self.assertEqual([i["key"] for i in listed["issues"]], [key])
        svc, _ = self.tool(url, "service_status", service="api")
        self.assertIn(key, [i["key"] for i in svc["issues"]])
        done, _ = self.tool(url, "resolve_report", key=key, note="cache warmed")
        self.assertEqual(done["status"], "resolved")
        st, _ = self.tool(url, "status")
        self.assertIn("summary", st)
        bad, err = self.tool(url, "report_issue", title="x", description="y", service="apii")
        self.assertTrue(err)
        self.assertIn("did you mean", bad["error"])

    def test_critical_report_pages_but_never_acts(self):
        e, url = self.engine(AITOMATON_EMERGENCY_POLICY="push+act")
        out, _ = self.tool(url, "report_issue", title="site is down for everyone", service="shop_web",
                           kind="outage", severity="critical", description="502 on every page")
        e.tick()
        self.assertEqual(e.emergencies[out["key"]]["runbook"], [])
        self.assertEqual(e.store.last_decision(out["key"])["applied"], "report")


class Helpers(unittest.TestCase):
    def test_experiment_name(self):
        self.assertEqual(experiment_name("aitomaton", "My_API", "sentry:api:12345"), "aitomaton-my-api-12345")

    def test_parse_result(self):
        self.assertEqual(parse_result('x\nAITOMATON-RESULT: {"status": "no_fix", "url": ""}')["status"], "no_fix")
        self.assertIsNone(parse_result("still working on it"))


if __name__ == "__main__":
    unittest.main()
