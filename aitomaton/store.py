"""SQLite state: issues, minute-bucketed occurrences, findings, decisions and works."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
  key TEXT PRIMARY KEY,           -- sentry:<project>:<id> | prom:<alertname>:<service>
  source TEXT, project TEXT, external_id TEXT,
  service TEXT, architecture TEXT,
  title TEXT, culprit TEXT, level TEXT, status TEXT,
  first_seen REAL, last_seen REAL, total INTEGER DEFAULT 0, users INTEGER DEFAULT 0,
  url TEXT, detail TEXT DEFAULT '{}', flags TEXT DEFAULT '{}', updated REAL
);
CREATE TABLE IF NOT EXISTS occurrences (
  key TEXT, bucket INTEGER, count INTEGER,   -- bucket = epoch minute
  PRIMARY KEY (key, bucket)
);
CREATE INDEX IF NOT EXISTS occ_bucket ON occurrences(bucket);
CREATE TABLE IF NOT EXISTS findings (
  id TEXT PRIMARY KEY,            -- <kind>:<scope>:<target>
  kind TEXT, scope TEXT, target TEXT, severity REAL,
  first_at REAL, last_at REAL, active INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT, at REAL, action TEXT, applied TEXT, policy TEXT, decider TEXT,
  confidence REAL, rationale TEXT, features TEXT
);
CREATE TABLE IF NOT EXISTS works (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT, decision_id INTEGER, kind TEXT, status TEXT, executor TEXT,
  ref TEXT, url TEXT, result_url TEXT, summary TEXT, brief TEXT,
  created REAL, updated REAL, log TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

OPEN_WORK = ("queued", "dispatched", "running", "waiting")


def _row(r):
    d = dict(r)
    for k in ("detail", "flags", "features", "log"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


class Store:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def q(self, sql, args=()):
        with self.lock:
            return [_row(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    # ---- kv
    def get(self, k, default=None):
        r = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default

    def put(self, k, v):
        self.x("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))

    # ---- issues
    def upsert_issue(self, key, **f):
        """Insert or update an issue. detail/flags are merged, not replaced."""
        now = time.time()
        with self.lock:
            cur = self.q("SELECT * FROM issues WHERE key=?", (key,))
            if cur:
                old = cur[0]
                detail = dict(old["detail"] or {}, **(f.pop("detail", None) or {}))
                flags = dict(old["flags"] or {}, **(f.pop("flags", None) or {}))
                f = {k: v for k, v in f.items() if v is not None}
                if "first_seen" in f and old["first_seen"]:
                    f["first_seen"] = min(f["first_seen"], old["first_seen"])
                f.update(detail=json.dumps(detail), flags=json.dumps(flags), updated=now)
                sets = ",".join("%s=?" % k for k in f)
                self.x("UPDATE issues SET %s WHERE key=?" % sets, tuple(f.values()) + (key,))
            else:
                f["detail"] = json.dumps(f.get("detail") or {})
                f["flags"] = json.dumps(f.get("flags") or {})
                f.update(key=key, updated=now)
                cols = ",".join(f)
                self.x("INSERT INTO issues(%s) VALUES(%s)" % (cols, ",".join("?" * len(f))), tuple(f.values()))
        return self.issue(key)

    def issue(self, key):
        r = self.q("SELECT * FROM issues WHERE key=?", (key,))
        return r[0] if r else None

    def issues(self, since=0):
        return self.q("SELECT * FROM issues WHERE last_seen>=? ORDER BY last_seen DESC", (since,))

    # ---- occurrences
    def add_occurrence(self, key, ts, count=1):
        if count <= 0:
            return
        args = (key, int(ts // 60))
        with self.lock:   # no UPSERT: SQLite < 3.24 is still common on hosts
            self.db.execute("INSERT OR IGNORE INTO occurrences(key,bucket,count) VALUES(?,?,0)", args)
            self.db.execute("UPDATE occurrences SET count=count+? WHERE key=? AND bucket=?", (int(count),) + args)
            self.db.commit()

    def has_occurrences(self, key):
        return bool(self.q("SELECT 1 FROM occurrences WHERE key=? LIMIT 1", (key,)))

    def series(self, keys, since):
        """{key: [(bucket_minute, count), ...]} for the given issue keys since a timestamp."""
        if not keys:
            return {}
        out = {k: [] for k in keys}
        keys = list(keys)
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            rows = self.q("SELECT key,bucket,count FROM occurrences WHERE bucket>=? AND key IN (%s) ORDER BY bucket"
                          % ",".join("?" * len(chunk)), (int(since // 60),) + tuple(chunk))
            for r in rows:
                out[r["key"]].append((r["bucket"], r["count"]))
        return out

    def prune(self, older_than):
        self.x("DELETE FROM occurrences WHERE bucket<?", (int(older_than // 60),))

    # ---- findings
    def set_findings(self, findings, now):
        """Replace the active set: findings not reported this tick become inactive (history is kept)."""
        with self.lock:
            self.db.execute("UPDATE findings SET active=0")
            for f in findings:
                fid = "%s:%s:%s" % (f["kind"], f["scope"], f["target"])
                cur = self.db.execute("SELECT active, last_at FROM findings WHERE id=?", (fid,)).fetchone()
                detail = json.dumps(f.get("detail") or {}, default=str)
                if cur is None:
                    self.db.execute("INSERT INTO findings(id,kind,scope,target,severity,first_at,last_at,active,detail)"
                                    " VALUES(?,?,?,?,?,?,?,1,?)", (fid, f["kind"], f["scope"], f["target"],
                                                                   f.get("severity", 0), now, now, detail))
                else:
                    # a finding that went quiet for over an hour starts a new episode
                    restart = not cur["active"] and cur["last_at"] < now - 3600
                    self.db.execute("UPDATE findings SET severity=?, last_at=?, active=1, detail=?%s WHERE id=?"
                                    % (", first_at=?" if restart else ""),
                                    (f.get("severity", 0), now, detail) + ((now,) if restart else ()) + (fid,))
            self.db.commit()

    def findings(self, active_only=False, limit=200):
        sql = "SELECT * FROM findings %s ORDER BY active DESC, last_at DESC LIMIT ?" % (
            "WHERE active=1" if active_only else "")
        return self.q(sql, (limit,))

    # ---- decisions
    def add_decision(self, key, action, applied, policy, decider, confidence, rationale, features):
        return self.x("INSERT INTO decisions(key,at,action,applied,policy,decider,confidence,rationale,features) "
                      "VALUES(?,?,?,?,?,?,?,?,?)", (key, time.time(), action, applied, policy, decider,
                                                    confidence, rationale, json.dumps(features)))

    def last_decision(self, key):
        r = self.q("SELECT * FROM decisions WHERE key=? ORDER BY id DESC LIMIT 1", (key,))
        return r[0] if r else None

    def decisions(self, limit=100):
        return self.q("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))

    # ---- works
    def add_work(self, key, decision_id, kind, status, executor, brief):
        now = time.time()
        return self.x("INSERT INTO works(key,decision_id,kind,status,executor,brief,created,updated,log) "
                      "VALUES(?,?,?,?,?,?,?,?,?)", (key, decision_id, kind, status, executor, brief, now, now,
                                                    json.dumps([{"at": now, "status": status}])))

    def update_work(self, wid, note=None, **f):
        with self.lock:
            w = self.work(wid)
            log = w["log"] or []
            if note or ("status" in f and f["status"] != w["status"]):
                log.append({"at": time.time(), "status": f.get("status", w["status"]), "note": note})
            f.update(updated=time.time(), log=json.dumps(log[-50:]))
            sets = ",".join("%s=?" % k for k in f)
            self.x("UPDATE works SET %s WHERE id=?" % sets, tuple(f.values()) + (wid,))

    def work(self, wid):
        r = self.q("SELECT * FROM works WHERE id=?", (wid,))
        return r[0] if r else None

    def work_by_ref(self, ref):
        r = self.q("SELECT * FROM works WHERE ref=? ORDER BY id DESC LIMIT 1", (ref,))
        return r[0] if r else None

    def open_work(self, key):
        r = self.q("SELECT * FROM works WHERE key=? AND status IN (%s) ORDER BY id DESC LIMIT 1"
                   % ",".join("?" * len(OPEN_WORK)), (key,) + OPEN_WORK)
        return r[0] if r else None

    def last_work(self, key, kind, statuses):
        r = self.q("SELECT * FROM works WHERE key=? AND kind=? AND status IN (%s) ORDER BY id DESC LIMIT 1"
                   % ",".join("?" * len(statuses)), (key, kind) + tuple(statuses))
        return r[0] if r else None

    def works(self, statuses=None, limit=100):
        if statuses:
            return self.q("SELECT * FROM works WHERE status IN (%s) ORDER BY id DESC LIMIT ?"
                          % ",".join("?" * len(statuses)), tuple(statuses) + (limit,))
        return self.q("SELECT * FROM works ORDER BY id DESC LIMIT ?", (limit,))

    def works_since(self, since, kind=None):
        """Works actually sent (dry-runs and queued ones do not count against the daily limit)."""
        sql, args = "SELECT COUNT(*) n FROM works WHERE created>=? AND status NOT IN ('dry-run','queued')", [since]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        return self.q(sql, tuple(args))[0]["n"]
