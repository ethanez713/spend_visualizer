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
        analyzer_python=analyzer / "venv" / "bin" / "python",
        pipeline_python=tmp_path / "pipeline" / "python",
        transactions_secrets=transactions / ".secrets",
        transformer_secrets=transformer / ".secrets",
        ui_port=_free_port(),
        ui_start_timeout_s=10.0,
    )

    _write_exe(cfg.transactions_python, component_python("fetch"))
    _write_exe(cfg.transformer_python, component_python("categorize"))
    _write_exe(cfg.analyzer_streamlit, streamlit_body)
    # Sheet sync and the budget-row export run under the analyzer venv, not
    # streamlit. The master cross-check runs under the pipeline's own interpreter.
    _write_exe(cfg.analyzer_python, component_python("analyzer"))
    _write_exe(cfg.pipeline_python, component_python("compare"))

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


def given_no_flags_when_parsing_then_push_on_and_sheet_left_to_converter_presence():
    args = parse_args([])
    assert args.push_data is True
    assert args.sheet is None
    assert args.sheet_sync is None


def given_sheet_sync_flags_when_parsing_then_tri_state():
    assert parse_args(["--sheet-sync"]).sheet_sync is True
    assert parse_args(["--no-sheet-sync"]).sheet_sync is False


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


def given_healthy_components_when_main_then_steps_run_in_order(fake_world, capsys):
    cfg, log = fake_world

    main(["--no-push-data", "--no-llm", "--no-browser"], cfg=cfg)

    lines = _log_lines(log)
    assert [l.split()[0] for l in lines] == ["fetch", "categorize", "streamlit"]
    assert "--no-llm" in lines[1]            # flag reached the transformer
    assert f"--server.port {cfg.ui_port}" in lines[2]
    out = capsys.readouterr().out
    assert "UI serving" in out
    assert "--no-browser" in out             # browser deliberately not opened


def given_no_ui_when_main_then_streamlit_never_launches(fake_world):
    cfg, log = fake_world

    main(["--no-push-data", "--no-ui"], cfg=cfg)

    assert [l.split()[0] for l in _log_lines(log)] == ["fetch", "categorize"]


def given_fetch_fails_when_main_then_pipeline_stops_before_categorize(fake_world):
    cfg, log = fake_world
    _fail_next(cfg.transactions_python, 1)   # e.g. unresolved raw-store conflict

    with pytest.raises(SystemExit) as exc:
        main(["--no-push-data", "--no-ui"], cfg=cfg)

    assert "fetch" in str(exc.value)
    assert [l.split()[0] for l in _log_lines(log)] == ["fetch"]   # nothing after


def given_categorize_fails_when_main_then_ui_never_launches(fake_world):
    cfg, log = fake_world
    _fail_next(cfg.transformer_python, 2)    # e.g. Drive divergence gate

    with pytest.raises(SystemExit) as exc:
        main(["--no-push-data"], cfg=cfg)

    assert "categorize" in str(exc.value)
    assert [l.split()[0] for l in _log_lines(log)] == ["fetch", "categorize"]


def given_ui_dies_before_serving_when_main_then_clear_error(fake_world):
    cfg, log = fake_world
    _fail_next(cfg.analyzer_streamlit, 3)    # streamlit exits without listening

    with pytest.raises(SystemExit) as exc:
        main(["--no-push-data", "--no-browser"], cfg=cfg)

    assert "before serving" in str(exc.value)


def given_no_linked_banks_when_preflight_then_stops_with_link_hint(fake_world):
    cfg, log = fake_world
    (cfg.transactions_dir / ".secrets" / "tokens.json").unlink()

    with pytest.raises(SystemExit) as exc:
        main(["--no-push-data", "--no-ui"], cfg=cfg)

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
        main(["--no-push-data", "--no-ui"], cfg=cfg)

    assert "--no-drive" in str(exc.value)    # offers the offline alternative
    assert _log_lines(log) == []


def given_no_drive_flag_when_main_then_no_cred_seeding_needed(fake_world):
    cfg, log = fake_world
    (cfg.transactions_secrets / "client_secret.json").unlink()  # no creds at all

    main(["--no-push-data", "--no-drive", "--no-ui"], cfg=cfg)                 # still fine offline

    assert [l.split()[0] for l in _log_lines(log)] == ["fetch", "categorize"]
    assert not cfg.transformer_secrets.exists()


# `data_repo` (conftest.py) builds its git repo at the same tmp path fake_world
# uses for Config.data_root, so combining the fixtures makes the pipeline's data
# root a real (synthetic) git repo with a local bare "GitHub" remote.

def _remote_commits(origin) -> int:
    proc = subprocess.run(["git", "rev-list", "--count", "main"],
                          cwd=str(origin), capture_output=True, text=True)
    return int(proc.stdout) if proc.returncode == 0 else 0


def given_lock_held_when_main_then_exits_before_any_component(fake_world):
    cfg, log = fake_world
    cfg.data_root.mkdir()
    with (cfg.data_root / ".pipeline.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with pytest.raises(SystemExit) as exc:
            main(["--no-push-data", "--no-ui", "--no-drive"], cfg=cfg)

    assert "in progress" in str(exc.value)
    assert _log_lines(log) == []                 # nothing ran under a held lock


def given_no_push_data_flag_when_main_then_data_repo_never_touched(fake_world):
    cfg, log = fake_world                        # data_root isn't even a git repo

    main(["--no-push-data", "--no-ui", "--no-drive"], cfg=cfg)     # would die if a push were tried

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


# The converter is an opt-in add-on (config._converter_dir resolves it). When
# present, the pipeline regenerates the budget ledger after categorize; the step
# is offline (--no-fetch/--all/--no-upload) and NON-fatal (it derives a view).

def given_no_converter_when_main_then_no_convert_step(fake_world):
    cfg, log = fake_world                         # default fake_world has no converter

    main(["--no-push-data", "--no-drive", "--no-ui"], cfg=cfg)

    assert "convert" not in [l.split()[0] for l in _log_lines(log)]


def given_converter_configured_when_building_convert_cmd_then_local_and_offline(with_converter):
    cfg, _ = with_converter
    cmd = convert_cmd(cfg)
    assert cmd[:2] == [str(cfg.converter_python), "refresh.py"]
    assert {"--all", "--no-upload"} <= set(cmd)   # whole history, no Sheet egress
    assert cmd[-2:] == ["--output", str(cfg.budget_ledger_csv)]


def given_converter_configured_when_main_then_convert_runs_after_categorize(with_converter):
    cfg, log = with_converter

    main(["--no-sheet", "--no-push-data", "--no-drive", "--no-browser"], cfg=cfg)

    # --no-drive skips sheet sync; the local cross-check still runs before the UI.
    assert [l.split()[0] for l in _log_lines(log)] == \
        ["fetch", "categorize", "convert", "analyzer", "compare", "streamlit"]
    convert_line = next(l for l in _log_lines(log) if l.startswith("convert"))
    assert "--no-upload" in convert_line          # the ledger regen never uploads


def given_no_convert_flag_when_main_then_ledger_not_regenerated(with_converter):
    cfg, log = with_converter

    main(["--no-sheet", "--no-push-data", "--no-drive", "--no-ui", "--no-convert"], cfg=cfg)

    # Ledger regen is skipped; the local cross-check is a different step and still runs.
    assert [l.split()[0] for l in _log_lines(log)] == \
        ["fetch", "categorize", "analyzer", "compare"]
    assert not any(l.startswith("convert") for l in _log_lines(log))


def given_converter_fails_when_main_then_pipeline_continues(with_converter, capsys):
    cfg, log = with_converter
    _fail_next(cfg.converter_python, 5)           # converter breaks — must not sink the run

    main(["--no-sheet", "--no-push-data", "--no-drive", "--no-browser"], cfg=cfg)  # does NOT raise

    assert [l.split()[0] for l in _log_lines(log)] == \
        ["fetch", "categorize", "convert", "analyzer", "compare", "streamlit"]
    assert "converter exited 5" in capsys.readouterr().out


def given_converter_venv_missing_when_main_then_warns_and_continues(with_converter, capsys):
    cfg, log = with_converter
    cfg.converter_python.unlink()                 # configured but venv not built

    # --no-sheet-sync: this test is about the ledger regen's own missing-venv warning.
    # Sheet sync's preflight would otherwise stop the run on the same missing python.
    main(["--no-sheet", "--no-sheet-sync", "--no-push-data", "--no-drive", "--no-ui"], cfg=cfg)

    assert [l.split()[0] for l in _log_lines(log)] == ["fetch", "categorize"]
    assert "venv python is missing" in capsys.readouterr().out


def given_converter_and_push_when_main_then_convert_precedes_push(with_converter, data_repo):
    cfg, log = with_converter
    repo, origin = data_repo
    assert repo == cfg.data_root
    (repo / "transactions.csv").write_text("synthetic\n")

    main(["--no-sheet", "--no-drive", "--no-ui", "--push-data"], cfg=cfg)

    # --no-drive skips sheet sync. The cross-check is outside the lock, after the push.
    assert [l.split()[0] for l in _log_lines(log)] == \
        ["fetch", "categorize", "convert", "analyzer", "compare"]
    assert _remote_commits(origin) == 1           # ledger regen happens before the push


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


def given_converter_when_building_sheet_cmd_then_upload_stays_on_and_window_is_left_to_it(
        with_converter):
    cfg, _ = with_converter
    cmd = sheet_cmd(cfg, "/tmp/u.txt")
    assert cmd == [str(cfg.converter_python), "refresh.py", "--url-file", "/tmp/u.txt"]
    assert "--no-upload" not in cmd               # this step IS the (opt-in) egress


def given_removed_window_flags_when_parsing_then_rejected():
    for flag in ("--sheet-month", "--sheet-since", "--sheet-until"):
        with pytest.raises(SystemExit):
            parse_args([flag, "2026-06"])


def given_sheet_flag_when_main_then_sheet_runs_last_and_tabs_open(
        with_converter, opened_urls):
    cfg, log = with_converter

    main(["--no-push-data", "--no-drive", "--sheet"], cfg=cfg)

    assert [l.split()[0] for l in _log_lines(log)] == \
        ["fetch", "categorize", "convert", "convert", "analyzer", "compare", "streamlit"]
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

    main(["--no-push-data", "--no-drive", "--sheet"], cfg=cfg)

    assert opened_urls == [f"http://localhost:{cfg.ui_port}",
                           "https://sheet.example/fake",
                           "https://docs.google.com/spreadsheets/d/master",
                           "https://docs.google.com/document/d/notes"]


def given_no_sheet_flag_when_main_then_no_upload_and_no_pinned_tabs(
        with_converter, opened_urls):
    cfg, log = with_converter
    cfg.data_root.mkdir(parents=True, exist_ok=True)
    (cfg.data_root / "pinned_tabs").write_text("https://example.com/pinned\n")

    main(["--no-sheet", "--no-push-data", "--no-drive"], cfg=cfg)

    convert_lines = [l for l in _log_lines(log) if l.startswith("convert")]
    assert len(convert_lines) == 1                # ledger regen only, no Sheet mode
    assert "--no-upload" in convert_lines[0]
    assert opened_urls == [f"http://localhost:{cfg.ui_port}"]   # pinned tabs are --sheet-only


def given_converter_configured_when_main_without_sheet_flag_then_sheet_uploads(
        with_converter, opened_urls):
    cfg, log = with_converter

    main(["--no-push-data", "--no-drive"], cfg=cfg)

    convert_lines = [l for l in _log_lines(log) if l.startswith("convert")]
    assert len(convert_lines) == 2 and "--url-file" in convert_lines[1]
    assert "https://sheet.example/fake" in opened_urls


def given_no_converter_when_main_without_sheet_flag_then_no_sheet_and_no_preflight_stop(
        fake_world):
    cfg, log = fake_world

    main(["--no-push-data", "--no-drive", "--no-ui"], cfg=cfg)

    assert [l.split()[0] for l in _log_lines(log)] == ["fetch", "categorize"]


def given_sheet_without_converter_when_preflight_then_stops(fake_world):
    cfg, log = fake_world                         # no converter configured

    with pytest.raises(SystemExit) as exc:
        main(["--no-push-data", "--no-drive", "--no-ui", "--sheet"], cfg=cfg)

    assert "--sheet" in str(exc.value)
    assert _log_lines(log) == []                  # nothing ran


def given_sheet_upload_fails_when_main_then_ui_still_serves(
        with_converter, opened_urls, capsys):
    cfg, log = with_converter
    _fail_next(cfg.converter_python, 7)           # both converter calls fail — non-fatal

    main(["--no-push-data", "--no-drive", "--sheet"], cfg=cfg)      # does NOT raise

    assert _log_lines(log)[-1].startswith("streamlit")
    assert opened_urls == [f"http://localhost:{cfg.ui_port}"]   # no broken Sheet tab
    assert "sheet upload exited 7" in capsys.readouterr().out


def given_converter_reports_no_url_when_sheet_then_warns_without_tab(
        with_converter, opened_urls, capsys):
    cfg, _ = with_converter
    (cfg.converter_python.parent / "no_url.txt").write_text("")  # upload failed inside

    main(["--no-push-data", "--no-drive", "--sheet"], cfg=cfg)

    assert opened_urls == [f"http://localhost:{cfg.ui_port}"]
    assert "reported no Sheet URL" in capsys.readouterr().out


def given_sheet_with_no_ui_when_main_then_urls_printed_not_opened(
        with_converter, opened_urls, capsys):
    cfg, log = with_converter

    main(["--no-push-data", "--no-drive", "--no-ui", "--sheet"], cfg=cfg)

    assert [l.split()[0] for l in _log_lines(log)] == \
        ["fetch", "categorize", "convert", "convert", "analyzer", "compare"]
    assert opened_urls == []
    assert "open https://sheet.example/fake in your browser" in capsys.readouterr().out


def given_pinned_tabs_file_when_parsing_then_comments_and_blanks_skipped(tmp_path):
    (tmp_path / "pinned_tabs").write_text("# a comment\n\n  https://x.example/a  \n")
    assert pinned_urls(tmp_path) == ["https://x.example/a"]
    assert pinned_urls(tmp_path / "missing") == []   # no data root file → no tabs


# Sheet sync (2d) mines manual edits back into the store inside the lock, before
# the data push. The master cross-check (2e) runs after the incremental Sheet
# step, outside the lock. Both are on by default when a converter is configured;
# --no-sheet-sync skips both, and --no-drive skips only 2d.

def _lock_held(data_root) -> bool:
    """True when another fd in this process already holds the pipeline lock.

    flock is per open-file-description, so a second open in the orchestrator
    process conflicts with pipeline_lock's fd — the same check a subprocess
    would see from outside."""
    lock = data_root / ".pipeline.lock"
    if not lock.exists():
        return False
    with lock.open("a") as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False


def _step_kind(text: str):
    if text == "__push_data__":
        return "push"
    if "fetch_transactions.py" in text:
        return "fetch"
    if "categorize.py" in text:
        return "categorize"
    if "refresh.py" in text and "--url-file" in text:
        return "sheet"
    if "refresh.py" in text:
        return "convert"
    if "mine_edits.py" in text:
        return "mine"
    if "tools.sheet_sync" in text:
        return "sync"
    if "push_edits.py" in text:
        return "pushedits"
    if "export_budget_rows.py" in text:
        return "export"
    if "sheet_compare.py" in text:
        return "compare"
    return None


def given_converter_when_main_then_sheet_sync_inside_lock_and_crosscheck_outside(
        with_converter, data_repo, monkeypatch, capsys):
    cfg, log = with_converter
    repo, origin = data_repo
    assert repo == cfg.data_root
    (repo / "transactions.csv").write_text("synthetic\n")

    seen = []
    real_run = subprocess.run
    real_push = src.pipeline.push_data

    def spy(cmd, cwd=None, **kwargs):
        seen.append((" ".join(str(c) for c in cmd), _lock_held(cfg.data_root), cwd))
        return real_run(cmd, cwd=cwd, **kwargs)

    def spy_push(data_root):
        seen.append(("__push_data__", _lock_held(data_root), data_root))
        return real_push(data_root)

    monkeypatch.setattr(src.pipeline.subprocess, "run", spy)
    monkeypatch.setattr(src.pipeline, "push_data", spy_push)

    main(["--no-ui", "--push-data"], cfg=cfg)

    kinds = [k for k in (_step_kind(text) for text, _, _ in seen) if k]
    assert kinds == ["fetch", "categorize", "convert", "mine", "sync", "pushedits",
                     "push", "sheet", "export", "compare"]
    inside = {"fetch", "categorize", "convert", "mine", "sync", "pushedits", "push"}
    by_kind = {}
    for text, held, cwd in seen:
        kind = _step_kind(text)
        if kind is None:
            continue
        by_kind[kind] = (text, held, cwd)
        assert held is (kind in inside), kind

    assert by_kind["mine"][2] == cfg.converter_dir
    assert by_kind["sync"][2] == cfg.analyzer_dir
    assert by_kind["pushedits"][2] == cfg.converter_dir
    assert by_kind["sheet"][2] == cfg.converter_dir
    assert by_kind["export"][2] == cfg.analyzer_dir
    assert by_kind["compare"][2] == src.pipeline._PIPELINE_DIR

    assert by_kind["mine"][0].startswith(str(cfg.converter_python) + " ")
    assert by_kind["mine"][0].endswith("mine_edits.py")
    assert by_kind["sync"][0] == f"{cfg.analyzer_python} -m tools.sheet_sync"
    assert by_kind["pushedits"][0] == f"{cfg.converter_python} push_edits.py"
    assert "tools/export_budget_rows.py" in by_kind["export"][0]
    export_out = by_kind["export"][0].split("--out ", 1)[1]
    assert export_out.endswith("/budget_rows.csv")
    assert by_kind["compare"][0] == (
        f"{cfg.pipeline_python} tools/sheet_compare.py --three-way --app-csv {export_out}")

    # The fakes actually ran, in the same order the spy saw.
    assert [l.split()[0] for l in _log_lines(log)] == [
        "fetch", "categorize", "convert", "convert", "analyzer", "convert",
        "convert", "analyzer", "compare"]
    text = "\n".join(_log_lines(log))
    assert text.index("mine_edits.py") < text.index("tools.sheet_sync") < text.index("push_edits.py")
    assert text.index("push_edits.py") < text.index("--url-file")
    assert text.index("--url-file") < text.index("export_budget_rows.py") < text.index("--three-way")

    out = capsys.readouterr().out
    assert "Drive sync ON + data git push + Sheet upload + Sheet sync" in out
    assert _remote_commits(origin) == 1


def given_sheet_sync_substep_fails_when_main_then_rest_of_sync_skipped_and_run_continues(
        with_converter, capsys):
    cfg, log = with_converter
    # Fail only the apply step. Export and the UI share no fail file with this.
    _write_exe(cfg.analyzer_python, textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import pathlib, sys
        pathlib.Path({str(log)!r}).open("a").write(
            "analyzer " + " ".join(sys.argv[1:]) + chr(10))
        if "tools.sheet_sync" in sys.argv:
            sys.exit(4)
        sys.exit(0)
        """))

    main(["--no-push-data", "--no-sheet", "--no-browser"], cfg=cfg)  # does NOT raise

    lines = _log_lines(log)
    text = "\n".join(lines)
    assert "mine_edits.py" in text                 # earlier substep ran
    assert "tools.sheet_sync" in text              # the failing substep ran
    assert "push_edits.py" not in text             # the rest of 2d was skipped
    assert "export_budget_rows.py" in text         # the run continued to 2e
    assert "--three-way" in text
    assert lines[-1].startswith("streamlit")      # and on to the UI
    out = capsys.readouterr().out
    assert "sheet sync: tools.sheet_sync exited 4" in out
    assert "skipping the rest of sheet sync" in out
    assert "✓ sheet sync finished" not in out
    assert "+ Sheet sync" in out                   # 2d was going to run


def given_export_fails_when_main_then_compare_skipped_and_run_continues(
        with_converter, capsys):
    cfg, log = with_converter
    _write_exe(cfg.analyzer_python, textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import pathlib, sys
        pathlib.Path({str(log)!r}).open("a").write(
            "analyzer " + " ".join(sys.argv[1:]) + chr(10))
        if "export_budget_rows.py" in " ".join(sys.argv[1:]):
            sys.exit(9)
        sys.exit(0)
        """))

    main(["--no-drive", "--no-push-data", "--no-sheet", "--no-ui"], cfg=cfg)

    text = "\n".join(_log_lines(log))
    assert "export_budget_rows.py" in text
    assert "sheet_compare.py" not in text
    assert "mine_edits.py" not in text             # --no-drive skipped 2d
    out = capsys.readouterr().out
    assert "export_budget_rows.py exited 9" in out
    assert "skipping the compare" in out
    assert "Data pipeline complete" in out


def given_no_sheet_sync_when_main_then_sync_and_crosscheck_skipped(with_converter, capsys):
    cfg, log = with_converter

    # Drive stays on, so 2d would run if the flag didn't suppress it.
    main(["--no-sheet-sync", "--no-push-data", "--no-ui", "--no-sheet"], cfg=cfg)

    text = "\n".join(_log_lines(log))
    for needle in ("mine_edits.py", "tools.sheet_sync", "push_edits.py",
                   "export_budget_rows.py", "sheet_compare.py"):
        assert needle not in text, needle
    assert [l.split()[0] for l in _log_lines(log)] == ["fetch", "categorize", "convert"]
    out = capsys.readouterr().out
    assert "+ Sheet sync" not in out
    assert "━━━ sheet sync ━━━" not in out
    assert "━━━ cross-check" not in out


def given_no_drive_when_main_then_sheet_sync_skipped_but_crosscheck_runs(
        with_converter, capsys):
    cfg, log = with_converter

    main(["--no-drive", "--no-push-data", "--no-ui", "--no-sheet"], cfg=cfg)

    text = "\n".join(_log_lines(log))
    assert "mine_edits.py" not in text
    assert "tools.sheet_sync" not in text
    assert "push_edits.py" not in text
    assert "export_budget_rows.py" in text
    assert "--three-way" in text and "--app-csv" in text
    out = capsys.readouterr().out
    assert "+ Sheet sync" not in out
    assert "━━━ sheet sync ━━━" not in out
    assert "━━━ cross-check (master workbook) ━━━" in out
    assert "✓ cross-check finished" in out


def given_no_converter_when_main_then_sheet_sync_and_crosscheck_skipped(fake_world, capsys):
    cfg, log = fake_world

    main(["--no-push-data", "--no-drive", "--no-ui"], cfg=cfg)

    text = "\n".join(_log_lines(log))
    for needle in ("mine_edits.py", "tools.sheet_sync", "push_edits.py",
                   "export_budget_rows.py", "sheet_compare.py"):
        assert needle not in text, needle
    assert [l.split()[0] for l in _log_lines(log)] == ["fetch", "categorize"]
    assert "+ Sheet sync" not in capsys.readouterr().out


def given_sheet_sync_without_converter_when_preflight_then_stops(fake_world):
    cfg, log = fake_world

    with pytest.raises(SystemExit) as exc:
        main(["--no-push-data", "--no-drive", "--no-ui", "--sheet-sync"], cfg=cfg)

    assert "--sheet-sync" in str(exc.value)
    assert "configured converter" in str(exc.value)
    assert _log_lines(log) == []


def given_sheet_sync_with_missing_converter_venv_when_preflight_then_stops(with_converter):
    cfg, log = with_converter
    cfg.converter_python.unlink()

    with pytest.raises(SystemExit) as exc:
        main(["--no-sheet", "--sheet-sync", "--no-push-data", "--no-drive", "--no-ui"],
             cfg=cfg)

    assert "--sheet-sync" in str(exc.value)
    assert "converter venv python missing" in str(exc.value)
    assert _log_lines(log) == []
