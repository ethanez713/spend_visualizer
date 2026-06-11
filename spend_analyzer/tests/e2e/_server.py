"""Launch the real app in a subprocess against throwaway config/data dirs.

A browser-driven app process cannot be monkeypatched, so isolation happens via
environment: SPEND_ANALYZER_CONFIG_DIR points at a temp config whose app.yaml
keeps the REAL archive (read-only) but swaps transformer_root for a temp clone
(symlinked src/, empty data/), and SPEND_ANALYZER_DATA_DIR redirects the
corrections queue. Every UI write path therefore lands in pytest tmp dirs.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
STREAMLIT = ROOT / "venv" / "bin" / "streamlit"
REAL_CONFIG = ROOT / "config"


def build_isolated_env(base: Path) -> dict[str, str]:
    from config_io import AppConfig, load_app_config

    real: AppConfig = load_app_config(REAL_CONFIG / "app.yaml")
    troot = base / "transformer"
    (troot / "data").mkdir(parents=True)
    (troot / "src").symlink_to(Path(real.resolved_transformer_root) / "src")

    cfg = base / "config"
    cfg.mkdir()
    for name in ("taxonomy.yaml", "accounts.yaml", "budget.yaml"):
        if (REAL_CONFIG / name).exists():
            shutil.copy(REAL_CONFIG / name, cfg / name)
    (cfg / "app.yaml").write_text(yaml.safe_dump({
        "archive_paths": real.resolved_archive_paths,   # absolute; read-only
        "transformer_root": str(troot),
        "trailing_avg_months": real.trailing_avg_months,
    }), encoding="utf-8")

    data = base / "data"
    data.mkdir()
    return {"SPEND_ANALYZER_CONFIG_DIR": str(cfg),
            "SPEND_ANALYZER_DATA_DIR": str(data)}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def launch(base: Path) -> tuple[subprocess.Popen, str]:
    env = {**os.environ, **build_isolated_env(base)}
    port = free_port()
    log = (base / "server.log").open("w")
    proc = subprocess.Popen(
        [str(STREAMLIT), "run", "app.py",
         f"--server.port={port}", "--server.headless=true",
         "--server.fileWatcherType=none", "--browser.gatherUsageStats=false"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://localhost:{port}"
    _wait_healthy(url, proc, base / "server.log")
    return proc, url


def _wait_healthy(url: str, proc: subprocess.Popen, log: Path,
                  timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"streamlit exited rc={proc.returncode}:\n"
                               f"{log.read_text()[-2000:]}")
        try:
            with urllib.request.urlopen(f"{url}/_stcore/health", timeout=2) as r:
                if r.read().decode().strip() == "ok":
                    return
        except OSError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"server not healthy after {timeout}s:\n"
                       f"{log.read_text()[-2000:]}")
