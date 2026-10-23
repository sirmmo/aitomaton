"""Prometheus input: Alertmanager webhooks become issues, PromQL queries add context to decisions."""
from __future__ import annotations

import urllib.parse

from ..http import request
from .sentry import parse_time

SERVICE_LABELS = ("service", "swarm_service", "container_label_com_docker_swarm_service_name", "job")
SEVERITY_LEVEL = {"critical": "fatal", "page": "fatal", "error": "error", "warning": "warning", "info": "info"}


def alert_service(labels):
    for k in SERVICE_LABELS:
        if labels.get(k):
            return labels[k]
    return None


def parse_alertmanager(payload):
    """Alertmanager webhook v4 -> [{"key", "service", "fields", "firing", "occurred"}]."""
    out = []
    for a in payload.get("alerts") or []:
        labels, ann = a.get("labels") or {}, a.get("annotations") or {}
        name = labels.get("alertname", "alert")
        service = alert_service(labels)
        key = "prom:%s:%s" % (name, service or labels.get("instance") or "global")
        starts = parse_time(a.get("startsAt"))
        out.append({
            "key": key, "service": service, "firing": a.get("status") == "firing",
            "occurred": starts,
            "fields": {
                "source": "prometheus", "project": labels.get("job"), "external_id": a.get("fingerprint"),
                "title": "%s: %s" % (name, ann.get("summary") or ann.get("description") or ""),
                "culprit": labels.get("instance"),
                "level": SEVERITY_LEVEL.get(labels.get("severity", "").lower(), "warning"),
                "status": "unresolved" if a.get("status") == "firing" else "resolved",
                "url": a.get("generatorURL"),
                "detail": {"labels": labels, "annotations": ann},
            },
        })
    return out


class Prometheus:
    def __init__(self, cfg):
        self.cfg = cfg
        self.error = None

    @property
    def enabled(self):
        return bool(self.cfg.prom_url)

    def query(self, promql):
        r = request("GET", "%s/api/v1/query?%s" % (self.cfg.prom_url, urllib.parse.urlencode({"query": promql})),
                    timeout=10)
        res = ((r or {}).get("data") or {}).get("result") or []
        if not res:
            return None
        try:
            return float(res[0]["value"][1])
        except (KeyError, IndexError, ValueError):
            return None

    def context(self, service):
        """The configured queries for one service, e.g. {"memory_limit_ratio": 0.97, "cpu_cores": 0.4}."""
        if not self.enabled or not service:
            return {}
        sel = service.get("prometheus_selector") or self.cfg.prom_selector.replace("{service}", service["name"])
        out = {}
        for name, q in self.cfg.prom_queries.items():
            try:
                out[name] = self.query(q.replace("{selector}", sel))
                self.error = None
            except Exception as e:
                self.error = str(e)
                out[name] = None
        return out
