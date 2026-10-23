"""Git credentials per service: a label names the credential, the secret itself lives in an env var or
a Docker secret, never in the label.

Service labels are readable by anyone who can `docker service inspect` (and get mirrored by tools such as
holodocker), so a label carries only a *name*:

  aitomaton.git.credential: shop        ->  /run/secrets/aitomaton_git_shop   (Docker secret)
                                             or env AITOMATON_GIT_TOKEN_SHOP
  aitomaton.git.author: "Bot <b@x.org>"  ->  commit identity (default AITOMATON_GIT_AUTHOR)

Without the label, the repo owner picks the token (AITOMATON_GIT_TOKEN_<OWNER>, e.g. ..._ACME_CORP for
Acme-Corp/core), then the global AITOMATON_GIT_TOKEN (or GITHUB_TOKEN). A label that holds what looks like
a literal token is refused.
"""
from __future__ import annotations

import os
import re

TOKEN_LIKE = re.compile(r"^(ghp_|gho_|ghs_|ghu_|github_pat_|glpat-)|^[A-Za-z0-9_]{36,}$")


def env_name(name):
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def _secret_file(dirpath, name):
    p = os.path.join(dirpath, "aitomaton_git_" + re.sub(r"[^a-z0-9_-]+", "_", name.lower()))
    try:
        with open(p) as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def resolve(service, environ=None, secrets_dir="/run/secrets"):
    """{"token", "source", "author"} for a service. token is None when nothing is configured; source says
    where it came from (never the value), for the status page."""
    env = os.environ if environ is None else environ
    service = service or {}
    author = service.get("git_author") or env.get("AITOMATON_GIT_AUTHOR") or "aitomaton <aitomaton@localhost>"
    name = service.get("git_credential")
    if name:
        if TOKEN_LIKE.search(name):
            return {"token": None, "author": author,
                    "source": "refused: aitomaton.git.credential looks like a literal token; name a secret instead"}
        tok = _secret_file(secrets_dir, name)
        if tok:
            return {"token": tok, "author": author, "source": "secret aitomaton_git_%s" % name}
        var = "AITOMATON_GIT_TOKEN_" + env_name(name)
        if env.get(var):
            return {"token": env[var], "author": author, "source": "env " + var}
        return {"token": None, "author": author, "source": "missing: credential %r is neither a secret nor %s"
                % (name, var)}
    owner = (service.get("repo") or "").split("/")[0]
    if owner:
        var = "AITOMATON_GIT_TOKEN_" + env_name(owner)
        if env.get(var):
            return {"token": env[var], "author": author, "source": "env " + var}
    for var in ("AITOMATON_GIT_TOKEN", "GITHUB_TOKEN"):
        if env.get(var):
            return {"token": env[var], "author": author, "source": "env " + var}
    return {"token": None, "author": author, "source": "none (the agent uses the host's own git login)"}


def redact(text, token):
    return (text or "").replace(token, "***") if token else (text or "")
