"""Offline sanity checks over the deploy artifacts: shell syntax, unit-file
shape, script wiring, and drift guards against finance_pipeline. Nothing here
talks to systemd, the network, or live data — the alert-script run is pointed
at a tmp data root via $SPEND_VISUALIZER_DATA.
"""
from __future__ import annotations

import configparser
import os
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1]
REPO = DEPLOY.parent

SCRIPTS = sorted((DEPLOY / "bin").glob("*.sh")) + sorted(DEPLOY.glob("*.sh"))
UNITS = sorted((DEPLOY / "systemd").glob("*"))


def _unit(name: str) -> configparser.ConfigParser:
    # systemd units are INI-ish; %h specifiers break interpolation, so none.
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.read_string((DEPLOY / "systemd" / name).read_text())
    return cp


# ── Shell scripts ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def given_shell_script_when_bash_n_then_syntax_ok(script):
    proc = subprocess.run(["bash", "-n", str(script)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def given_shell_script_then_executable(script):
    assert os.access(script, os.X_OK), f"{script.name} lost its +x bit"


# ── Unit-file shape (the keys the design depends on) ─────────────────────────

def given_timer_then_daily_persistent_and_jittered():
    timer = _unit("finance-daily.timer")["Timer"]
    assert timer["OnCalendar"].endswith("06:30:00")
    assert timer["Persistent"] == "true"          # catch-up after downtime
    assert "RandomizedDelaySec" in timer
    assert _unit("finance-daily.timer")["Install"]["WantedBy"] == "timers.target"


def given_daily_service_then_oneshot_with_alert_hook():
    cp = _unit("finance-daily.service")
    assert cp["Service"]["Type"] == "oneshot"
    assert cp["Unit"]["OnFailure"] == "finance-daily-alert.service"
    assert "TimeoutStartSec" in cp["Service"]
    assert "Install" not in cp                    # timer-triggered only


def given_analyzer_service_then_loopback_bound_and_restarting():
    cp = _unit("spend-analyzer.service")
    assert "--server.address 127.0.0.1" in cp["Service"]["ExecStart"]
    assert cp["Service"]["Restart"] == "on-failure"
    assert cp["Install"]["WantedBy"] == "default.target"


def given_any_unit_then_paths_are_home_relative():
    for unit in UNITS:
        text = unit.read_text()
        assert "/home/" not in text, f"{unit.name} hardcodes an absolute home path"


def given_units_with_exec_scripts_then_scripts_exist_and_are_executable():
    checked = 0
    for unit in UNITS:
        cp = _unit(unit.name)
        if "Service" not in cp:                   # the timer has no ExecStart
            continue
        exec_start = cp["Service"]["ExecStart"]
        path = Path(exec_start.split()[0].replace("%h/spend_visualizer", str(REPO)))
        if DEPLOY in path.parents:                # venv binaries are per-machine
            assert path.is_file() and os.access(path, os.X_OK), \
                f"{unit.name} ExecStart points at missing/non-exec {path}"
            checked += 1
    assert checked == 2                           # daily + alert wrappers


# ── Wrapper wiring (drift guards against finance_pipeline) ───────────────────

def given_daily_wrapper_then_flags_match_the_server_contract():
    text = (DEPLOY / "bin" / "finance-daily.sh").read_text()
    for flag in ("--no-ui", "--no-llm", "--push-data"):
        assert flag in text
    assert "--no-drive" not in text               # Drive push stays ON
    # Server is rules-only and FINAL: never enable the local LLM, and never --llm-defer —
    # the latter would leave rows perpetually LLM-pending (no desktop LLM run exists now;
    # deep review is the desktop Claude audit ritual). "--llm" is a substring of both
    # "--llm" and "--llm-defer", but NOT of "--no-llm", so this one check forbids both.
    assert "--llm" not in text


def given_daily_wrapper_flags_then_pipeline_still_defines_them():
    pipeline = (REPO / "finance_pipeline" / "src" / "pipeline.py").read_text()
    for flag in ("--no-ui", "--no-llm", "--push-data"):
        assert f'"{flag}"' in pipeline, \
            f"{flag} used by finance-daily.sh but gone from pipeline.py"


def given_alert_script_when_run_with_tmp_data_root_then_failure_logged(tmp_path):
    proc = subprocess.run(
        [str(DEPLOY / "bin" / "finance-alert.sh")],
        env={**os.environ, "SPEND_VISUALIZER_DATA": str(tmp_path)},
        capture_output=True, text=True)

    assert proc.returncode == 0, proc.stderr
    line = (tmp_path / "logs" / "failures.log").read_text()
    assert "finance-daily FAILED" in line and "journalctl" in line
