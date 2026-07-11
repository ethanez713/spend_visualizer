"""Offline tests for the orchestrator: fake component repos in tmp dirs.

Each fake component is an executable script that appends its argv to a shared log
and exits with a configurable code, so the tests assert the real contract of the
orchestrator — step order, flag propagation, stop-on-failure, preflight checks,
credential seeding, and the UI port-wait — without Plaid, Drive, Ollama, or
Streamlit. Nothing touches the network or the real sibling repos.
"""
from __future__ import annotations

import dataclasses
import fcntl
import socket
import stat
import subprocess
import textwrap

import pytest

from src.config import Config
import src.pipeline
from src.pipeline import (
    categorize_cmd,
    convert_cmd,
    ensure_drive_creds,
    fetch_cmd,
    main,
    pinned_urls,
    preflight,
    parse_args,
    sheet_cmd,
    ui_cmd,
)


# ── Fake-component harness ───────────────────────────────────────────────────

def _write_exe(path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_world(tmp_path):
    """A full fake component tree: repos, venv binaries, tokens, Drive creds.

    The fake `python` logs `<repo-name> <argv...>` to log.txt and exits with the
    code in fail.txt (if present). The fake `streamlit` additionally listens on
    the requested --server.port until one probe connects (mimicking a server the
    orchestrator must wait for), then exits 0.
    """
    log = tmp_path / "log.txt"

    def component_python(repo: str):
        return textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import pathlib, sys
            pathlib.Path({str(log)!r}).open("a").write(
                "{repo} " + " ".join(sys.argv[1:]) + chr(10))
            fail = pathlib.Path(__file__).parent / "fail.txt"
            sys.exit(int(fail.read_text()) if fail.is_file() else 0)
            """)

    streamlit_body = textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import pathlib, socket, sys
        pathlib.Path({str(log)!r}).open("a").write(
            "streamlit " + " ".join(sys.argv[1:]) + chr(10))
        fail = pathlib.Path(__file__).parent / "fail.txt"
        if fail.is_file():
            sys.exit(int(fail.read_text()))
        port = int(sys.argv[sys.argv.index("--server.port") + 1])
        s = socket.socket()
        s.bind(("127.0.0.1", port))
        s.listen(1)
        conn, _ = s.accept()   # the orchestrator's readiness probe
        conn.close()
        sys.exit(0)
        """)

    transactions = tmp_path / "transactions"
    transformer = tmp_path / "transformer"
    analyzer = tmp_path / "analyzer"

    cfg = Config(
        transactions_dir=transactions,
        transformer_dir=transformer,
        analyzer_dir=analyzer,
        data_root=tmp_path / "finance_data",
        transactions_python=transactions / "venv" / "bin" / "python",
        transformer_python=transformer / ".venv" / "bin" / "python",
        analyzer_streamlit=analyzer / "venv" / "bin" / "streamlit",
        transactions_secrets=transactions / ".secrets",
        transformer_secrets=transformer / ".secrets",
        ui_port=_free_port(),
        ui_start_timeout_s=10.0,
    )

    _write_exe(cfg.transactions_python, component_python("fetch"))
    _write_exe(cfg.transformer_python, component_python("categorize"))
    _write_exe(cfg.analyzer_streamlit, streamlit_body)

    # Linked banks + existing Drive creds (preflight requirements) — both live in
    # transactions/.secrets, the repo that owns the raw store's Drive sync.
    cfg.transactions_secrets.mkdir(parents=True)
    (cfg.transactions_secrets / "tokens.json").write_text("[]")
    (cfg.transactions_secrets / "client_secret.json").write_text("{}")
    (cfg.transactions_secrets / "token.json").write_text("{}")

    return cfg, log


@pytest.fixture
def with_converter(fake_world, tmp_path):
    """fake_world + an optional external converter wired in (its own fake venv).

    The fake converter `python` logs `convert <argv...>` to the shared log and
    honors fail.txt, exactly like the component pythons — so convert tests assert
    the orchestrator's contract (ordering, offline flags, non-fatal failure)
    without the real converter project. When invoked in Sheet mode (--url-file)
    it reports a fake Sheet URL, unless a `no_url.txt` sentinel simulates an
    upload that failed inside an otherwise-successful converter run."""
    cfg, log = fake_world
    converter = tmp_path / "converter"
    converter_python = converter / ".venv" / "bin" / "python"
    body = textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import pathlib, sys
        pathlib.Path({str(log)!r}).open("a").write(
            "convert " + " ".join(sys.argv[1:]) + chr(10))
        fail = pathlib.Path(__file__).parent / "fail.txt"
        if fail.is_file():
            sys.exit(int(fail.read_text()))
        if "--url-file" in sys.argv and \\
                not (pathlib.Path(__file__).parent / "no_url.txt").is_file():
            pathlib.Path(sys.argv[sys.argv.index("--url-file") + 1]).write_text(
                "https://sheet.example/fake" + chr(10))
        sys.exit(0)
        """)
    _write_exe(converter_python, body)
    cfg = dataclasses.replace(
        cfg,
        converter_dir=converter,
        converter_python=converter_python,
        budget_ledger_csv=cfg.data_root / "spend_analyzer" / "data" / "budget_ledger.csv",
    )
    return cfg, log


def _log_lines(log):
    return log.read_text().splitlines() if log.exists() else []


def _fail_next(cfg_python, code: int) -> None:
    (cfg_python.parent / "fail.txt").write_text(str(code))


# ── Command construction (pure) ───────────────────────────────────────────────

def given_default_args_when_building_cmds_then_components_run_with_defaults(fake_world):
    cfg, _ = fake_world
    args = parse_args([], default_port=cfg.ui_port)
    assert fetch_cmd(cfg, args) == [str(cfg.transactions_python), "fetch_transactions.py"]
    assert categorize_cmd(cfg, args) == [str(cfg.transformer_python), "categorize.py"]
    assert ui_cmd(cfg, args) == [str(cfg.analyzer_streamlit), "run", "app.py",
                                 "--server.port", str(cfg.ui_port)]


def given_flags_when_building_cmds_then_they_propagate(fake_world):
    cfg, _ = fake_world
    args = parse_args(["--no-drive", "--no-llm", "--force-push", "--port", "9999"])
    assert "--no-drive" in fetch_cmd(cfg, args)
    cat = categorize_cmd(cfg, args)
    assert {"--no-drive", "--no-llm", "--force-push"} <= set(cat)
    assert ui_cmd(cfg, args)[-1] == "9999"


def given_llm_defer_when_building_cmds_then_it_propagates(fake_world):
    cfg, _ = fake_world
    args = parse_args(["--llm-defer"])
    assert "--llm-defer" in categorize_cmd(cfg, args)


def given_llm_opt_in_when_building_cmds_then_it_propagates(fake_world):
    # The local LLM is off by default; --llm must reach the transformer to turn it on.
    cfg, _ = fake_world
    assert "--llm" not in categorize_cmd(cfg, parse_args([]))     # default: no flag, LLM off
    assert "--llm" in categorize_cmd(cfg, parse_args(["--llm"]))


def given_no_llm_and_llm_defer_together_then_rejected():
    # Mutually exclusive: "rules-only is final" vs "rules now, LLM later".
    with pytest.raises(SystemExit):
        parse_args(["--no-llm", "--llm-defer"])


# ── Happy path ────────────────────────────────────────────────────────────────

def given_healthy_components_when_main_then_steps_run_in_order(fake_world, capsys):
    cfg, log = fake_world

    main(["--no-llm", "--no-browser"], cfg=cfg)

    lines = _log_lines(log)
    assert [l.split()[0] for l in lines] == ["fetch", "categorize", "streamlit"]
    assert "--no-llm" in lines[1]            # flag reached the transformer
    assert f"--server.port {cfg.ui_port}" in lines[2]
    out = capsys.readouterr().out
    assert "UI serving" in out
    assert "--no-browser" in out             # browser deliberately not opened


def given_no_ui_when_main_then_streamlit_never_launches(fake_world):
    cfg, log = fake_world

    main(["--no-ui"], cfg=cfg)

    assert [l.split()[0] for l in _log_lines(log)] == ["fetch", "categorize"]


# ── Stop-on-failure (the conflict-stop contract) ─────────────────────────────

def given_fetch_fails_when_main_then_pipeline_stops_before_categorize(fake_world):
    cfg, log = fake_world
    _fail_next(cfg.transactions_python, 1)   # e.g. unresolved raw-store conflict

    with pytest.raises(SystemExit) as exc:
        main(["--no-ui"], cfg=cfg)

    assert "fetch" in str(exc.value)
    assert [l.split()[0] for l in _log_lines(log)] == ["fetch"]   # nothing after


def given_categorize_fails_when_main_then_ui_never_launches(fake_world):
    cfg, log = fake_world
    _fail_next(cfg.transformer_python, 2)    # e.g. Drive divergence gate

    with pytest.raises(SystemExit) as exc:
        main([], cfg=cfg)

    assert "categorize" in str(exc.value)
    assert [l.split()[0] for l in _log_lines(log)] == ["fetch", "categorize"]


def given_ui_dies_before_serving_when_main_then_clear_error(fake_world):
    cfg, log = fake_world
    _fail_next(cfg.analyzer_streamlit, 3)    # streamlit exits without listening

    with pytest.raises(SystemExit) as exc:
        main(["--no-browser"], cfg=cfg)

    assert "before serving" in str(exc.value)


# ── Preflight ─────────────────────────────────────────────────────────────────

def given_no_linked_banks_when_preflight_then_stops_with_link_hint(fake_world):
    cfg, log = fake_world
    (cfg.transactions_dir / ".secrets" / "tokens.json").unlink()

    with pytest.raises(SystemExit) as exc:
        main(["--no-ui"], cfg=cfg)

    assert "link" in str(exc.value).lower()
    assert _log_lines(log) == []             # no component ever ran


def given_missing_venv_when_preflight_then_all_problems_reported(fake_world):
    cfg, _ = fake_world
    cfg.transformer_python.unlink()
    (cfg.transactions_dir / ".secrets" / "tokens.json").unlink()

    with pytest.raises(SystemExit) as exc:
        preflight(cfg, parse_args(["--no-drive", "--no-llm"]))

    msg = str(exc.value)
    assert "transformer venv python" in msg and "link" in msg.lower()


# ── Drive credential seeding ──────────────────────────────────────────────────

def given_empty_transformer_secrets_when_drive_run_then_creds_seeded_0600(fake_world):
    cfg, _ = fake_world

    ensure_drive_creds(cfg)  # seeds from transactions/.secrets

    for name in ("client_secret.json", "token.json"):
        f = cfg.transformer_secrets / name
        assert f.is_file()
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(cfg.transformer_secrets.stat().st_mode) == 0o700


def given_transformer_creds_already_present_when_drive_run_then_left_alone(fake_world):
    cfg, _ = fake_world
    cfg.transformer_secrets.mkdir(mode=0o700)
    existing = cfg.transformer_secrets / "client_secret.json"
    existing.write_text('{"mine": true}')

    ensure_drive_creds(cfg)

    assert existing.read_text() == '{"mine": true}'      # not overwritten
    assert not (cfg.transformer_secrets / "token.json").exists()


def given_no_drive_creds_anywhere_when_drive_run_then_stops_with_setup_hint(fake_world):
    cfg, log = fake_world
    (cfg.transactions_secrets / "client_secret.json").unlink()

    with pytest.raises(SystemExit) as exc:
        main(["--no-ui"], cfg=cfg)

    assert "--no-drive" in str(exc.value)    # offers the offline alternative
    assert _log_lines(log) == []


def given_no_drive_flag_when_main_then_no_cred_seeding_needed(fake_world):
    cfg, log = fake_world
    (cfg.transactions_secrets / "client_secret.json").unlink()  # no creds at all

    main(["--no-drive", "--no-ui"], cfg=cfg)                 # still fine offline

    assert [l.split()[0] for l in _log_lines(log)] == ["fetch", "categorize"]
    assert not cfg.transformer_secrets.exists()


# ── Pipeline lock + data push (the scheduled-run additions) ──────────────────
# `data_repo` (conftest.py) builds its git repo at the same tmp path fake_world
# uses for Config.data_root, so combining the fixtures makes the pipeline's data
# root a real (synthetic) git repo with a local bare "GitHub" remote.

def _remote_commits(origin) -> int:
    import subprocess
    proc = subprocess.run(["git", "rev-list", "--count", "main"],
                          cwd=str(origin), capture_output=True, text=True)
    return int(proc.stdout) if proc.returncode == 0 else 0


def given_lock_held_when_main_then_exits_before_any_component(fake_world):
    cfg, log = fake_world
    cfg.data_root.mkdir()
    with (cfg.data_root / ".pipeline.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with pytest.raises(SystemExit) as exc:
            main(["--no-ui", "--no-drive"], cfg=cfg)

    assert "in progress" in str(exc.value)
    assert _log_lines(log) == []                 # nothing ran under a held lock


def given_no_push_flag_when_main_then_data_repo_never_touched(fake_world):
    cfg, log = fake_world                        # data_root isn't even a git repo

    main(["--no-ui", "--no-drive"], cfg=cfg)     # would die if a push were tried

    assert [l.split()[0] for l in _log_lines(log)] == ["fetch", "categorize"]


def given_push_flag_when_steps_succeed_then_snapshot_lands_on_remote(
        fake_world, data_repo):
    cfg, log = fake_world
    repo, origin = data_repo
    assert repo == cfg.data_root                 # fixtures share the path (see above)
    (repo / "transactions.csv").write_text("synthetic\n")

    main(["--no-ui", "--no-drive", "--push-data"], cfg=cfg)

    assert [l.split()[0] for l in _log_lines(log)] == ["fetch", "categorize"]
    assert _remote_commits(origin) == 1          # pushed only after both steps


def given_push_flag_when_fetch_fails_then_nothing_committed(fake_world, data_repo):
    cfg, log = fake_world
    repo, origin = data_repo
    (repo / "transactions.csv").write_text("synthetic\n")
    _fail_next(cfg.transactions_python, 1)

    with pytest.raises(SystemExit) as exc:
        main(["--no-ui", "--no-drive", "--push-data"], cfg=cfg)

    assert "fetch" in str(exc.value)             # stop-on-failure covers the push
    assert _remote_commits(origin) == 0
    assert _remote_commits(repo) == 0            # not even a local commit


# ── Optional external budget-ledger conversion ───────────────────────────────
# The converter is an opt-in add-on (config._converter_dir resolves it). When
# present, the pipeline regenerates the budget ledger after categorize; the step
# is offline (--no-fetch/--all/--no-upload) and NON-fatal (it derives a view).

def given_no_converter_when_main_then_no_convert_step(fake_world):
    cfg, log = fake_world                         # default fake_world has no converter

    main(["--no-drive", "--no-ui"], cfg=cfg)

    assert "convert" not in [l.split()[0] for l in _log_lines(log)]


def given_converter_configured_when_building_convert_cmd_then_local_and_offline(with_converter):
    cfg, _ = with_converter
    cmd = convert_cmd(cfg)
    assert cmd[:2] == [str(cfg.converter_python), "refresh.py"]
    assert {"--all", "--no-upload"} <= set(cmd)   # whole history, no Sheet egress
    assert cmd[-2:] == ["--output", str(cfg.budget_ledger_csv)]


def given_converter_configured_when_main_then_convert_runs_after_categorize(with_converter):
    cfg, log = with_converter

    main(["--no-drive", "--no-browser"], cfg=cfg)

    assert [l.split()[0] for l in _log_lines(log)] == \
        ["fetch", "categorize", "convert", "streamlit"]
    convert_line = next(l for l in _log_lines(log) if l.startswith("convert"))
    assert "--no-upload" in convert_line          # the ledger regen never uploads


def given_no_convert_flag_when_main_then_ledger_not_regenerated(with_converter):
    cfg, log = with_converter

    main(["--no-drive", "--no-ui", "--no-convert"], cfg=cfg)

    assert [l.split()[0] for l in _log_lines(log)] == ["fetch", "categorize"]


def given_converter_fails_when_main_then_pipeline_continues(with_converter, capsys):
    cfg, log = with_converter
    _fail_next(cfg.converter_python, 5)           # converter breaks — must not sink the run

    main(["--no-drive", "--no-browser"], cfg=cfg)  # does NOT raise

    assert [l.split()[0] for l in _log_lines(log)] == \
        ["fetch", "categorize", "convert", "streamlit"]   # UI still served
    assert "converter exited 5" in capsys.readouterr().out


def given_converter_venv_missing_when_main_then_warns_and_continues(with_converter, capsys):
    cfg, log = with_converter
    cfg.converter_python.unlink()                 # configured but venv not built

    main(["--no-drive", "--no-ui"], cfg=cfg)

    assert [l.split()[0] for l in _log_lines(log)] == ["fetch", "categorize"]
    assert "venv python is missing" in capsys.readouterr().out


def given_converter_and_push_when_main_then_convert_precedes_push(with_converter, data_repo):
    cfg, log = with_converter
    repo, origin = data_repo
    assert repo == cfg.data_root
    (repo / "transactions.csv").write_text("synthetic\n")

    main(["--no-drive", "--no-ui", "--push-data"], cfg=cfg)

    assert [l.split()[0] for l in _log_lines(log)] == ["fetch", "categorize", "convert"]
    assert _remote_commits(origin) == 1           # ledger regen happens before the push


# ── --sheet: monthly Google-Sheet upload + extra browser tabs ─────────────────
# The `run_finances` ritual: after the data steps, the converter runs a second
# time in Sheet mode (upload ON, URL reported back via --url-file) and the fresh
# Sheet plus any <data_root>/pinned_tabs URLs open as extra browser tabs.

@pytest.fixture
def opened_urls(monkeypatch):
    """Capture what the pipeline would open in a browser (no real browser)."""
    urls: list[str] = []
    monkeypatch.setattr(src.pipeline, "open_browser",
                        lambda url: urls.append(url) or "fake-opener")
    return urls


def given_sheet_flag_when_building_sheet_cmd_then_upload_stays_on(with_converter):
    cfg, _ = with_converter
    args = parse_args(["--sheet"])
    cmd = sheet_cmd(cfg, args, "/tmp/u.txt")
    assert cmd[:2] == [str(cfg.converter_python), "refresh.py"]
    assert "--no-upload" not in cmd               # this step IS the (opt-in) egress
    assert cmd[-2:] == ["--url-file", "/tmp/u.txt"]


def given_sheet_month_when_building_sheet_cmd_then_month_propagates(with_converter):
    cfg, _ = with_converter
    args = parse_args(["--sheet", "--sheet-month", "2026-06"])
    cmd = sheet_cmd(cfg, args, "/tmp/u.txt")
    assert "--month" in cmd and "2026-06" in cmd


def given_sheet_window_when_building_sheet_cmd_then_since_until_propagate(with_converter):
    cfg, _ = with_converter
    args = parse_args(["--sheet", "--sheet-since", "2026-06-15",
                       "--sheet-until", "2026-07-09"])
    cmd = sheet_cmd(cfg, args, "/tmp/u.txt")
    assert cmd[cmd.index("--since") + 1] == "2026-06-15"
    assert cmd[cmd.index("--until") + 1] == "2026-07-09"


def given_sheet_flag_when_main_then_sheet_runs_last_and_tabs_open(
        with_converter, opened_urls):
    cfg, log = with_converter

    main(["--no-drive", "--sheet"], cfg=cfg)

    assert [l.split()[0] for l in _log_lines(log)] == \
        ["fetch", "categorize", "convert", "convert", "streamlit"]
    sheet_line = _log_lines(log)[3]               # second converter call = Sheet mode
    assert "--url-file" in sheet_line and "--no-upload" not in sheet_line
    assert opened_urls == [f"http://localhost:{cfg.ui_port}",
                           "https://sheet.example/fake"]


def given_pinned_tabs_when_sheet_run_then_pinned_urls_open_after_sheet(
        with_converter, opened_urls):
    cfg, _ = with_converter
    cfg.data_root.mkdir(parents=True, exist_ok=True)
    (cfg.data_root / "pinned_tabs").write_text(
        "# master budget spreadsheet\n"
        "https://docs.google.com/spreadsheets/d/master\n"
        "\n"
        "https://docs.google.com/document/d/notes\n")

    main(["--no-drive", "--sheet"], cfg=cfg)

    assert opened_urls == [f"http://localhost:{cfg.ui_port}",
                           "https://sheet.example/fake",
                           "https://docs.google.com/spreadsheets/d/master",
                           "https://docs.google.com/document/d/notes"]


def given_no_sheet_flag_when_main_then_no_upload_and_no_pinned_tabs(
        with_converter, opened_urls):
    cfg, log = with_converter
    cfg.data_root.mkdir(parents=True, exist_ok=True)
    (cfg.data_root / "pinned_tabs").write_text("https://example.com/pinned\n")

    main(["--no-drive"], cfg=cfg)

    convert_lines = [l for l in _log_lines(log) if l.startswith("convert")]
    assert len(convert_lines) == 1                # ledger regen only, no Sheet mode
    assert "--no-upload" in convert_lines[0]
    assert opened_urls == [f"http://localhost:{cfg.ui_port}"]   # pinned tabs are --sheet-only


def given_sheet_without_converter_when_preflight_then_stops(fake_world):
    cfg, log = fake_world                         # no converter configured

    with pytest.raises(SystemExit) as exc:
        main(["--no-drive", "--no-ui", "--sheet"], cfg=cfg)

    assert "--sheet" in str(exc.value)
    assert _log_lines(log) == []                  # nothing ran


def given_sheet_upload_fails_when_main_then_ui_still_serves(
        with_converter, opened_urls, capsys):
    cfg, log = with_converter
    _fail_next(cfg.converter_python, 7)           # both converter calls fail — non-fatal

    main(["--no-drive", "--sheet"], cfg=cfg)      # does NOT raise

    assert _log_lines(log)[-1].startswith("streamlit")
    assert opened_urls == [f"http://localhost:{cfg.ui_port}"]   # no broken Sheet tab
    assert "sheet upload exited 7" in capsys.readouterr().out


def given_converter_reports_no_url_when_sheet_then_warns_without_tab(
        with_converter, opened_urls, capsys):
    cfg, _ = with_converter
    (cfg.converter_python.parent / "no_url.txt").write_text("")  # upload failed inside

    main(["--no-drive", "--sheet"], cfg=cfg)

    assert opened_urls == [f"http://localhost:{cfg.ui_port}"]
    assert "reported no Sheet URL" in capsys.readouterr().out


def given_sheet_with_no_ui_when_main_then_urls_printed_not_opened(
        with_converter, opened_urls, capsys):
    cfg, log = with_converter

    main(["--no-drive", "--no-ui", "--sheet"], cfg=cfg)

    assert [l.split()[0] for l in _log_lines(log)] == \
        ["fetch", "categorize", "convert", "convert"]
    assert opened_urls == []
    assert "open https://sheet.example/fake in your browser" in capsys.readouterr().out


def given_pinned_tabs_file_when_parsing_then_comments_and_blanks_skipped(tmp_path):
    (tmp_path / "pinned_tabs").write_text("# a comment\n\n  https://x.example/a  \n")
    assert pinned_urls(tmp_path) == ["https://x.example/a"]
    assert pinned_urls(tmp_path / "missing") == []   # no data root file → no tabs
