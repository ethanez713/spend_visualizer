"""Opt-in snapshot of the data root into its own git history (``--push-data``).

The data root is a separate PRIVATE git repo (see the root CLAUDE.md). After the
data steps succeed, this commits whatever changed and pushes to its ``origin``
remote — giving an off-machine history independent of the Drive revisions. The
flag is explicit opt-in: anything that sends data off the machine always is.

Stdlib only (plain ``git`` subprocesses), matching the orchestrator's rules:

- **Not a git repo** → exit non-zero (the flag was asked for; fail loud so a
  scheduler's OnFailure hook fires rather than silently skipping uploads).
- **Dirty** → ``git add -A`` + commit under an automated identity, so snapshot
  commits are distinguishable from hand-made ones. **Clean** → still push: a
  previously failed push may have left committed-but-unpushed history.
- **No ``origin`` remote** → warn loudly and return success (commit is kept
  locally; deploy/RUNBOOK.md covers configuring the remote).
- Branch is whatever HEAD names (no hardcoded master/main); ``push -u`` is
  idempotent and makes the very first push set the upstream.

Snapshot race: ``git add -A`` can race a UI corrections-append. Appends are
single small lines; worst case the tail lands in the next day's commit — an
ordering quirk, not a correctness issue (the stores themselves are append-only).
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

_AUTOMATED_IDENTITY = "finance-pipeline"


def _git(data_root: Path, *args: str) -> subprocess.CompletedProcess:
    """Run git in the data root, capturing output (callers decide loudness)."""
    return subprocess.run(["git", "-C", str(data_root), *args],
                          capture_output=True, text=True)


def _git_or_die(data_root: Path, *args: str) -> str:
    proc = _git(data_root, *args)
    if proc.returncode != 0:
        sys.exit(f"✖ --push-data: `git {' '.join(args)}` failed in {data_root}:\n"
                 f"{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def push_data(data_root: Path) -> None:
    """Commit the data root if dirty, then push its current branch to origin."""
    if not (data_root / ".git").exists():
        sys.exit(f"✖ --push-data: {data_root} is not a git repo. Initialize it "
                 "(git init + first commit) or drop the flag.")

    if _git_or_die(data_root, "status", "--porcelain").strip():
        host = socket.gethostname()
        _git_or_die(data_root, "add", "-A")
        _git_or_die(
            data_root,
            "-c", f"user.name={_AUTOMATED_IDENTITY}",
            "-c", f"user.email={_AUTOMATED_IDENTITY}@{host}",
            "-c", "commit.gpgsign=false",   # unattended runs can't prompt to sign
            "commit", "-m",
            f"Pipeline snapshot {time.strftime('%Y-%m-%d %H:%M %Z')} ({host})",
        )
        print(f"  committed data snapshot in {data_root}")
    else:
        print("  data repo clean — nothing new to commit")

    if _git(data_root, "remote", "get-url", "origin").returncode != 0:
        print(f"  ⚠ no 'origin' remote on {data_root} — committed locally only, "
              "skipping push (see deploy/RUNBOOK.md to configure the remote)")
        return

    branch = _git_or_die(data_root, "symbolic-ref", "--short", "HEAD").strip()
    push = _git(data_root, "push", "-u", "origin", branch)
    if push.returncode != 0:
        sys.exit(f"✖ --push-data: push to origin/{branch} failed:\n"
                 f"{push.stderr.strip()}")
    print(f"✓ pushed {branch} to origin")
