"""All settings come from environment variables (usually an .env file or the stack file)."""
from __future__ import annotations

import json
import os


def _env(name, default=None):
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def _int(name, default):
    return int(_env(name, default))


def _float(name, default):
    return float(_env(name, default))


def _bool(name, default=False):
    return str(_env(name, "1" if default else "0")).lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self):
        self.environ = dict(os.environ)     # per-service git tokens are looked up here (credentials.py)
        # server
        self.host = _env("AITOMATON_HOST", "0.0.0.0")
        self.port = _int("AITOMATON_PORT", 8790)
        self.public_url = _env("AITOMATON_PUBLIC_URL", "http://localhost:%d" % self.port).rstrip("/")
        self.db = _env("AITOMATON_DB", "./state/aitomaton.db")
        self.static_dir = _env("AITOMATON_STATIC", os.path.join(os.path.dirname(__file__), "..", "static"))
        self.tick = _int("AITOMATON_TICK_SECONDS", 30)
        self.status_token = _env("AITOMATON_STATUS_TOKEN")      # optional: protects /status.json and /

        # sentry / urgentry (same /api/0/ surface)
        self.sentry_url = (_env("SENTRY_URL") or "").rstrip("/")
        self.sentry_token = _env("SENTRY_TOKEN")
        self.sentry_org = _env("SENTRY_ORG")
        self.sentry_projects = [p for p in (_env("SENTRY_PROJECTS", "")).split(",") if p]  # empty: all
        self.sentry_poll = _int("SENTRY_POLL_SECONDS", 120)
        self.sentry_hook_secret = _env("SENTRY_HOOK_SECRET")    # integration client secret, verifies webhooks

        # prometheus / alertmanager
        self.prom_url = (_env("PROMETHEUS_URL") or "").rstrip("/")
        self.prom_selector = _env("PROMETHEUS_DEFAULT_SELECTOR",
                                  '{container_label_com_docker_swarm_service_name="{service}"}')
        self.prom_queries = json.loads(_env("PROMETHEUS_QUERIES", "null") or "null") or {
            "memory_bytes": "max(container_memory_usage_bytes{selector})",
            "memory_limit_ratio": "max(container_memory_usage_bytes{selector} / "
                                  "(container_spec_memory_limit_bytes{selector} > 0))",
            "cpu_cores": "sum(rate(container_cpu_usage_seconds_total{selector}[5m]))",
        }
        self.alertmanager_secret = _env("ALERTMANAGER_HOOK_SECRET")  # optional ?token= on the webhook URL

        # docker swarm
        self.docker_socket = _env("DOCKER_SOCKET", "/var/run/docker.sock")
        self.label_prefix = _env("AITOMATON_LABEL_PREFIX", "aitomaton")
        self.swarm_refresh = _int("SWARM_REFRESH_SECONDS", 60)

        # the infrastructure itself: swarm health checks, and the outlet its issues go to (a repo holding
        # the stack files / infrastructure as code). Without a repo, its reports stay on the status page.
        self.infra = _bool("AITOMATON_INFRA", True)
        self.infra_name = _env("AITOMATON_INFRA_NAME", "swarm")
        self.infra_repo = _env("AITOMATON_INFRA_REPO")
        self.infra_ref = _env("AITOMATON_INFRA_REF", "main")
        self.infra_path = _env("AITOMATON_INFRA_PATH", "")
        self.infra_policy = _env("AITOMATON_INFRA_POLICY", "report")
        self.infra_credential = _env("AITOMATON_INFRA_CREDENTIAL")
        self.infra_test = _env("AITOMATON_INFRA_TEST")
        self.infra_notes = _env("AITOMATON_INFRA_NOTES")
        self.infra_policy_emergency = _env("AITOMATON_INFRA_POLICY_EMERGENCY")
        self.infra_grace = _int("INFRA_GRACE_SECONDS", 300)      # below its replicas this long: a condition
        self.infra_task_failures = _int("INFRA_TASK_FAILURES", 3)  # failed tasks of one kind per hour

        # analysis
        self.burst_window = _int("BURST_WINDOW_SECONDS", 300)
        self.burst_min = _int("BURST_MIN_EVENTS", 10)
        self.burst_ratio = _float("BURST_RATIO", 5.0)
        self.baseline_window = _int("BASELINE_WINDOW_SECONDS", 86400)
        self.long_days = _int("LONG_TERM_DAYS", 14)
        self.cascade_window = _int("CASCADE_WINDOW_SECONDS", 600)
        self.new_window = _int("NEW_ISSUE_SECONDS", 3600)

        # decision
        self.decider = _env("AITOMATON_DECIDER", "chain")       # rules | laya | llm | chain
        self.laya_url = (_env("LAYA_URL") or "").rstrip("/")
        # chain order after the rules; each tier is consulted only while the previous one is unsure
        self.chain = [t.strip() for t in _env("AITOMATON_CHAIN", "laya,jev,llm").split(",") if t.strip()]
        self.escalate_below = _float("ESCALATE_BELOW", _float("LAYA_ESCALATE_BELOW", 0.6))
        self.llm_base_url = (_env("LLM_BASE_URL", "https://openrouter.ai/api/v1")).rstrip("/")
        self.llm_api_key = _env("LLM_API_KEY") or _env("OPENROUTER_API_KEY")
        self.llm_model = _env("LLM_MODEL", "deepseek/deepseek-v4.1-flash")
        # JEV: TypeSafe's System One model on OpenRouter (typesafe/jev-router picks model and effort per
        # request). Its own tier, off unless JEV_MODEL is set; base URL and key default to the LLM's.
        self.jev_model = _env("JEV_MODEL")
        self.jev_base_url = (_env("JEV_BASE_URL") or self.llm_base_url).rstrip("/")
        self.jev_api_key = _env("JEV_API_KEY") or self.llm_api_key
        self.decision_cooldown = _int("DECISION_COOLDOWN_SECONDS", 3600)
        self.default_policy = _env("AITOMATON_DEFAULT_POLICY", "report")  # observe | report | pr | push

        # emergencies (emergency.py): a second policy layer, direct actions, notifications
        self.emergency_policy = _env("AITOMATON_EMERGENCY_POLICY")     # default layer for enabled services
        self.emergency_window = _int("EMERGENCY_WINDOW_SECONDS", 21600)  # older episodes are not emergencies
        self.rollback_window = _int("ROLLBACK_WINDOW_SECONDS", 3600)     # trouble this soon after a deploy: roll back
        self.act = _bool("AITOMATON_ACT", False)                 # master switch for direct actions on the swarm
        self.act_verify = _int("ACT_VERIFY_SECONDS", 300)        # wait this long before the next runbook step
        self.max_actions_per_hour = _int("MAX_ACTIONS_PER_HOUR", 3)
        self.act_memory_factor = _float("ACT_MEMORY_FACTOR", 1.5)
        self.act_memory_max_mb = _int("ACT_MEMORY_MAX_MB", 8192)
        self.notify_url = _env("AITOMATON_NOTIFY_URL")           # Slack-compatible incoming webhook

        # execution
        self.dispatch = _bool("AITOMATON_DISPATCH", False)       # off: works are prepared (dry-run), not sent
        self.max_running = _int("MAX_RUNNING_WORKS", 2)
        self.max_per_day = _int("MAX_WORKS_PER_DAY", 5)
        self.stairway_url = (_env("STAIRWAY_URL") or "").rstrip("/")
        self.stairway_token = _env("STAIRWAY_TOKEN")
        self.stairway_agent = _env("STAIRWAY_AGENT", "claude")   # claude | opencode | crush | openclaude
        self.stairway_model = _env("STAIRWAY_MODEL")             # for non-claude agents, e.g. an OpenRouter id
        self.stairway_permission_mode = _env("STAIRWAY_PERMISSION_MODE", "acceptEdits")
        self.stairway_prefix = _env("STAIRWAY_PREFIX", "aitomaton")
        self.callback_url = _env("AITOMATON_CALLBACK_URL", "http://localhost:%d/hooks/stairway" % self.port)
        self.callback_secret = _env("AITOMATON_CALLBACK_SECRET")
        # git credentials are resolved per service (credentials.py); this is only the global fallback
        self.github_token = _env("AITOMATON_GIT_TOKEN") or _env("GITHUB_TOKEN")
        self.secrets_dir = _env("AITOMATON_SECRETS_DIR", "/run/secrets")
        # stairway's experiments root as aitomaton sees it (mounted at the same path). Set: aitomaton clones
        # the repo with the service's credential before the agent starts. Unset: the agent clones itself.
        self.workspace_root = _env("STAIRWAY_EXPERIMENTS_ROOT")
        self.git_base = _env("AITOMATON_GIT_BASE", "https://github.com")
        self.github_api = (_env("GITHUB_API", "https://api.github.com")).rstrip("/")

    def summary(self):
        """What the status JSON shows about the configuration (no secrets)."""
        return {
            "decider": self.decider,
            "dispatch": self.dispatch,
            "default_policy": self.default_policy,
            "sources": {
                "sentry": bool(self.sentry_url and self.sentry_token and self.sentry_org),
                "sentry_webhook": True,
                "prometheus": bool(self.prom_url),
                "alertmanager_webhook": True,
            },
            "infrastructure": {"enabled": self.infra, "name": self.infra_name, "repo": self.infra_repo,
                               "policy": self.infra_policy},
            "emergency": {"default_policy": self.emergency_policy, "infra_policy": self.infra_policy_emergency,
                          "act": self.act, "notify": bool(self.notify_url), "window_s": self.emergency_window,
                          "max_actions_per_hour": self.max_actions_per_hour},
            "laya": bool(self.laya_url),
            "chain": ["rules"] + self.chain,
            "jev": {"model": self.jev_model, "configured": bool(self.jev_model and (
                self.jev_api_key or not self.jev_base_url.startswith("https://openrouter.ai")))},
            "llm": {"base_url": self.llm_base_url, "model": self.llm_model, "configured": bool(self.llm_api_key)
                    or not self.llm_base_url.startswith("https://openrouter.ai")},
            "executor": {"stairway": bool(self.stairway_url and self.stairway_token),
                         "agent": self.stairway_agent, "model": self.stairway_model,
                         "github_issues": bool(self.github_token),
                         "prepares_clone": bool(self.workspace_root)},
            "limits": {"max_running": self.max_running, "max_per_day": self.max_per_day,
                       "decision_cooldown_s": self.decision_cooldown},
            "analysis": {"burst_window_s": self.burst_window, "burst_min": self.burst_min,
                         "burst_ratio": self.burst_ratio, "long_term_days": self.long_days,
                         "cascade_window_s": self.cascade_window},
        }
