"""What a checkout is: its commit, whether it has uncommitted changes, and where it came from."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Checkout:
    commit: str | None
    dirty: bool | None
    remote: str | None
    repository: str | None  # owner/name, when the remote is on GitHub
    commit_time: int | None  # Unix seconds of HEAD's commit

    def as_json(self) -> dict:
        return {"commit": self.commit, "dirty": self.dirty, "remote": self.remote, "repository": self.repository}


def _git(path: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def repository_of(remote: str | None) -> str | None:
    if not remote:
        return None
    match = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", remote)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def inspect(path: Path) -> Checkout:
    commit = _git(path, "rev-parse", "HEAD")
    if commit is None:
        return Checkout(None, None, None, None, None)
    status = _git(path, "status", "--porcelain")
    remote = _git(path, "remote", "get-url", "origin")
    time = _git(path, "log", "-1", "--format=%ct")
    return Checkout(
        commit=commit,
        dirty=None if status is None else bool(status),
        remote=remote,
        repository=repository_of(remote),
        commit_time=int(time) if time and time.isdigit() else None,
    )


def unchanged_between(path: Path, old: str, new: str, *paths: str) -> bool | None:
    """Whether `paths` are identical at two commits; None when either commit is unknown here."""
    try:
        done = subprocess.run(
            ["git", "-C", str(path), "diff", "--quiet", old, new, "--", *paths], capture_output=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {0: True, 1: False}.get(done.returncode)
