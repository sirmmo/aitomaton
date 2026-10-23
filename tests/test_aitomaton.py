import hashlib
import hmac
import json
import os
import sys
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aitomaton.analysis import analyze  # noqa: E402
from aitomaton.config import Config  # noqa: E402
from aitomaton.deciders import cap, rules  # noqa: E402
from aitomaton.engine import Engine  # noqa: E402
from aitomaton.executors import experiment_name, parse_result  # noqa: E402
from aitomaton.server import make_handler  # noqa: E402
from aitomaton.sources.prometheus import parse_alertmanager  # noqa: E402
from aitomaton.sources.sentry import parse_webhook, verify_signature  # noqa: E402
from aitomaton.store import Store  # noqa: E402
from aitomaton.swarm import Swarm, descriptor, normalize_repo  # noqa: E402

NOW = 1_800_000_000.0


def cfg(**env):
    base = {"AITOMATON_DB": ":memory:", "AITOMATON_DECIDER": "rules", "LLM_BASE_URL": "https://openrouter.ai/api/v1",
            "LLM_API_KEY": "", "OPENROUTER_API_KEY": "", "SENTRY_URL": "", "PROMETHEUS_URL": "", "LAYA_URL": "",
            "STAIRWAY_URL": "", "STAIRWAY_TOKEN": "", "GITHUB_TOKEN": "", "AITOMATON_DISPATCH": "0",
            "JEV_MODEL": "", "JEV_BASE_URL": "", "JEV_API_KEY": "", "AITOMATON_CHAIN": "", "AITOMATON_GIT_TOKEN": ""}
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


class Helpers(unittest.TestCase):
    def test_experiment_name(self):
        self.assertEqual(experiment_name("aitomaton", "My_API", "sentry:api:12345"), "aitomaton-my-api-12345")

    def test_parse_result(self):
        self.assertEqual(parse_result('x\nAITOMATON-RESULT: {"status": "no_fix", "url": ""}')["status"], "no_fix")
        self.assertIsNone(parse_result("still working on it"))


if __name__ == "__main__":
    unittest.main()
