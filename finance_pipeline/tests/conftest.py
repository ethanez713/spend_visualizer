"""Shared offline fixture: a synthetic data-root git repo wired to a local bare
"origin" — the no-network stand-in for the private GitHub finance_data repo.

The repo is created at ``tmp_path/"finance_data"``, the same path ``fake_world``
(test_pipeline.py) uses for ``Config.data_root``, so tests that take both
fixtures get a pipeline whose data root IS the git repo.
"""
from __future__ import annotations

import subprocess

import pytest


def run_git(*args, cwd):
    """Run git in ``cwd``; assert success and return stdout."""
    proc = subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout


@pytest.fixture
def data_repo(tmp_path):
    """(repo, origin): an initialized data-root repo + its local bare remote."""
    repo = tmp_path / "finance_data"
    repo.mkdir()
    run_git("init", "-q", "-b", "main", cwd=repo)
    origin = tmp_path / "origin.git"
    origin.mkdir()
    run_git("init", "-q", "--bare", "-b", "main", cwd=origin)
    run_git("remote", "add", "origin", str(origin), cwd=repo)
    return repo, origin
