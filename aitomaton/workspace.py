"""Preparing a stairway experiment before the agent starts: clone the repo with the service's own
credential, configure push and identity, create the fix branch.

aitomaton must see stairway's experiments root at the same path (mount it: /srv:/srv). The token goes to
<experiment>/.aitomaton/git-credentials, outside the clone, referenced by the clone's credential helper,
and is deleted when the work ends. It is never in a remote URL, the brief, or a log line.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import urllib.parse

from .credentials import redact


class WorkspaceError(Exception):
    pass


def _git(args, cwd, token, timeout=600):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    r = subprocess.run(["git"] + args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise WorkspaceError(redact("git %s: %s" % (args[0], (r.stderr or r.stdout).strip()[-400:]), token))
    return r.stdout.strip()


def prepare(folder, repo, ref, branch, cred, push_to_ref=False, base="https://github.com"):
    """Clone <base>/<repo> into <folder>/repo on `branch` (from `ref`), push-ready."""
    if not os.path.isdir(folder):
        raise WorkspaceError("%s is not visible to aitomaton: mount stairway's experiments root" % folder)
    token = cred["token"]
    meta = os.path.join(folder, ".aitomaton")
    os.makedirs(meta, mode=0o700, exist_ok=True)
    store = os.path.join(meta, "git-credentials")
    fd = os.open(store, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("https://x-access-token:%s@%s\n" % (token, urllib.parse.urlparse(base).netloc or "github.com"))
    url = "%s/%s.git" % (base.rstrip("/"), repo)
    helper = "store --file=%s" % store
    _git(["-c", "credential.helper=", "-c", "credential.helper=" + helper,
          "clone", "--filter=blob:none", "--branch", ref, url, "repo"], folder, token)
    clone = os.path.join(folder, "repo")
    name, _, email = cred["author"].partition("<")
    # an empty helper first drops the host's own helpers, so pushes use this service's credential only
    _git(["config", "credential.helper", ""], clone, token)
    _git(["config", "--add", "credential.helper", helper], clone, token)
    for k, v in (("user.name", name.strip() or "aitomaton"),
                 ("user.email", email.rstrip(">").strip() or "aitomaton@localhost"), ("push.default", "current")):
        _git(["config", k, v], clone, token)
    if not push_to_ref:
        _git(["checkout", "-b", branch], clone, token)
    return clone


def cleanup(folder):
    """Remove the stored credential once the work is over (the clone stays for inspection)."""
    meta = os.path.join(folder or "", ".aitomaton")
    if folder and os.path.isdir(meta):
        shutil.rmtree(meta, ignore_errors=True)
