"""Offline tests for the --push-data snapshot step (src/git_push.py).

Real git against tmp repos; the ``data_repo`` fixture's local bare repo stands
in for the private GitHub remote — no network, no real data root.
"""
from __future__ import annotations

import re
import socket
import subprocess

import pytest

from conftest import run_git
from src.git_push import push_data


def _git(*args, cwd):
    """Raw git (no success assertion) for probing expected-failure states."""
    return subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True)


def _commit_count(repo, ref="main") -> int:
    proc = _git("rev-list", "--count", ref, cwd=repo)
    return int(proc.stdout) if proc.returncode == 0 else 0


def given_dirty_repo_when_push_data_then_snapshot_committed_and_pushed(data_repo):
    repo, origin = data_repo
    (repo / "transactions").mkdir()
    (repo / "transactions" / "transactions.csv").write_text("synthetic\n")

    push_data(repo)

    assert _commit_count(origin) == 1            # landed on the "GitHub" remote
    host = re.escape(socket.gethostname())
    subject, author, email = run_git(
        "log", "-1", "--format=%s%n%an%n%ae", cwd=origin).splitlines()
    assert re.fullmatch(
        rf"Pipeline snapshot \d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}} \S+ \({host}\)",
        subject)
    assert author == "finance-pipeline"          # automated identity, not the user's
    assert email.startswith("finance-pipeline@")


def given_first_push_when_push_data_then_upstream_set(data_repo):
    repo, _ = data_repo
    (repo / "file.jsonl").write_text("{}\n")

    push_data(repo)

    upstream = run_git("rev-parse", "--abbrev-ref", "main@{upstream}", cwd=repo)
    assert upstream.strip() == "origin/main"


def given_clean_repo_with_unpushed_commit_when_push_data_then_still_pushed(
        data_repo, capsys):
    repo, origin = data_repo
    (repo / "file.jsonl").write_text("{}\n")
    run_git("add", "-A", cwd=repo)
    run_git("-c", "user.name=t", "-c", "user.email=t@t",
            "commit", "-q", "-m", "left behind by a failed push", cwd=repo)

    push_data(repo)                              # clean tree, but history is ahead

    assert "nothing new to commit" in capsys.readouterr().out
    assert _commit_count(origin) == 1            # the stranded commit got pushed


def given_no_origin_remote_when_push_data_then_warns_and_keeps_local_commit(
        data_repo, capsys):
    repo, origin = data_repo
    run_git("remote", "remove", "origin", cwd=repo)
    (repo / "file.jsonl").write_text("{}\n")

    push_data(repo)                              # must NOT raise

    assert "no 'origin' remote" in capsys.readouterr().out
    assert _commit_count(repo, "HEAD") == 1      # committed locally all the same
    assert _commit_count(origin) == 0


def given_data_root_not_a_git_repo_when_push_data_then_exits_nonzero(tmp_path):
    with pytest.raises(SystemExit) as exc:
        push_data(tmp_path)

    assert "not a git repo" in str(exc.value)


def given_unreachable_remote_when_push_data_then_exits_nonzero(data_repo, tmp_path):
    repo, _ = data_repo
    run_git("remote", "set-url", "origin", str(tmp_path / "missing.git"), cwd=repo)
    (repo / "file.jsonl").write_text("{}\n")

    with pytest.raises(SystemExit) as exc:
        push_data(repo)

    assert "push" in str(exc.value)
    assert _commit_count(repo, "HEAD") == 1      # commit survives for the next try
