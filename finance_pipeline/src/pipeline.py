"""End-to-end finance pipeline: fetch → categorize → analyze.

Runs the three sibling components in sequence, each as a subprocess under its own
venv from its own repo root, stopping at the first non-zero exit:

  1. ``transactions``  — /transactions/sync pulls everything new since the last run
     (cursor-based), reconciles the raw store against the Drive remote with Plaid as
     the golden repair source, updates its durable store in ``data/``,
     and pushes new Drive revisions of the raw JSONL + CSV. Conflicts the golden
     re-fetch cannot resolve exit non-zero → the pipeline STOPS before persisting.
  2. ``plaid_category_transformer`` — audits the new/changed rows (mechanical rules
     + local LLM reviewer), updates ``data/transactions_categorized.{jsonl,csv}`` and
     the review worklist, and pushes Drive revisions. Its divergence gate exits
     non-zero (pipeline STOPS) if the Drive copy drifted from the local store.
  2b. (optional) the external ``converter`` project — only when one is configured
     (see ``config._converter_dir``). Regenerates the budget ledger CSV the Budget
     tab reads so its categories match the established budget. Local-only and
     NON-fatal: it derives a view, never touches the source stores.
  2c. (optional, ``--sheet``) the converter again, in its month-Sheet mode: converts
     the chosen month and uploads it as a new Google Sheet (explicit opt-in egress,
     like every upload). NON-fatal; the Sheet URL is captured via the converter's
     ``--url-file`` and opened as an extra browser tab, along with any URLs pinned
     in ``<data_root>/pinned_tabs`` (e.g. the master budget spreadsheet).
  3. ``spend_analyzer`` — serves the Streamlit UI over the categorized store and
     opens the default browser at the local URL.

The components own all domain logic (sync cursors, reconcile policy, audit rules,
review flow, Drive sync). This orchestrator is deliberately just preflight +
choreography, and needs only the Python standard library.
"""
from __future__ import annotations

import argparse
import fcntl
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
from contextlib import contextmanager
from pathlib import Path

from .config import Config, default_config
from .git_push import push_data

# Files that let persister's DriveSync authenticate; copied (locally) from
# transactions/.secrets into the transformer's .secrets on first Drive-enabled run.
_DRIVE_CRED_FILES = ("client_secret.json", "token.json")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args(argv=None, *, default_port: int = 8501) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="finance_pipeline",
        description="Fetch new Plaid transactions, audit/correct their categories, "
                    "persist raw + categorized stores to Google Drive, then launch "
                    "the Spend Analyzer UI in the default browser.",
    )
    p.add_argument("--no-drive", action="store_true",
                   help="fully offline run: no Drive pull/reconcile/push in any component")
    llm_group = p.add_mutually_exclusive_group()
    llm_group.add_argument("--llm", action="store_true",
                           help="enable the transformer's local-LLM review stage (OFF by "
                                "default since the 7B was too noisy; the Claude ritual is "
                                "the default reviewer — see plaid_category_transformer)")
    llm_group.add_argument("--no-llm", action="store_true",
                           help="explicitly skip the transformer's local-LLM stage (the "
                                "default now; rules still apply, rows stamp as fully audited)")
    llm_group.add_argument("--llm-defer", action="store_true",
                           help="rules-only categorize now, rows stay pending so a later "
                                "--llm run audits them")
    p.add_argument("--force-push", action="store_true",
                   help="pass through to the transformer: override its Drive divergence "
                        "gate and treat the local categorized store as authoritative")
    p.add_argument("--push-data", action="store_true",
                   help="after the data steps succeed, commit the data-root git repo "
                        "(if dirty) and push it to its 'origin' remote — explicit "
                        "opt-in upload, like every off-machine egress")
    p.add_argument("--no-convert", action="store_true",
                   help="skip regenerating the external budget ledger even if a "
                        "converter is configured (see the README's converter step)")
    p.add_argument("--sheet", action="store_true",
                   help="after the data steps, run the external converter's monthly "
                        "Google-Sheet upload and open the new Sheet (plus any URLs in "
                        "<data_root>/pinned_tabs) as extra browser tabs — explicit "
                        "opt-in egress; needs a configured converter")
    p.add_argument("--sheet-month", default=None, metavar="YYYY-MM",
                   help="month window for the --sheet upload (default: the converter's "
                        "default, i.e. the current calendar month)")
    p.add_argument("--no-ui", action="store_true",
                   help="stop after the data steps; don't launch the Streamlit UI")
    p.add_argument("--no-browser", action="store_true",
                   help="launch the UI but don't open a browser window")
    p.add_argument("--port", type=int, default=default_port,
                   help=f"Streamlit port (default {default_port})")
    return p.parse_args(argv)


# ── Component commands (pure: easy to test) ───────────────────────────────────

def fetch_cmd(cfg: Config, args: argparse.Namespace) -> list[str]:
    cmd = [str(cfg.transactions_python), "fetch_transactions.py"]
    if args.no_drive:
        cmd.append("--no-drive")
    return cmd


def categorize_cmd(cfg: Config, args: argparse.Namespace) -> list[str]:
    cmd = [str(cfg.transformer_python), "categorize.py"]
    if args.no_drive:
        cmd.append("--no-drive")
    if args.llm:
        cmd.append("--llm")
    if args.no_llm:
        cmd.append("--no-llm")
    if args.llm_defer:
        cmd.append("--llm-defer")
    if args.force_push:
        cmd.append("--force-push")
    return cmd


def convert_cmd(cfg: Config) -> list[str]:
    """Regenerate the budget ledger from the just-categorized store, fully local.

    ``--all`` converts the whole history so the Budget tab's trailing averages have
    every month, and ``--no-upload`` keeps it offline (no Google-Sheet egress —
    Drive/Sheet pushes stay opt-in). The converter never fetches: this pipeline is
    its upstream (invocation is one-directional, pipeline → converter)."""
    return [str(cfg.converter_python), "refresh.py", "--all",
            "--no-upload", "--output", str(cfg.budget_ledger_csv)]


def sheet_cmd(cfg: Config, args: argparse.Namespace, url_file: str) -> list[str]:
    """The converter's month-Sheet mode: convert the chosen month (its default:
    the current one) and upload it as a new Google Sheet, reporting the Sheet's
    URL through ``url_file`` so the pipeline can open it in a browser tab."""
    cmd = [str(cfg.converter_python), "refresh.py", "--url-file", url_file]
    if args.sheet_month:
        cmd += ["--month", args.sheet_month]
    return cmd


def ui_cmd(cfg: Config, args: argparse.Namespace) -> list[str]:
    return [str(cfg.analyzer_streamlit), "run", "app.py",
            "--server.port", str(args.port)]


# ── Preflight ─────────────────────────────────────────────────────────────────

def ensure_drive_creds(cfg: Config) -> None:
    """Seed the transformer's .secrets with Drive credentials if it has none.

    Each Drive-pushing component keeps its own ``.secrets`` (credentials AND its own
    ``drive_state.json`` file-id memory) — persister is a pure library and holds no
    state of its own. The original OAuth'd credentials live in
    ``transactions/.secrets``; copy them over — locally, owner-only — so the
    transformer's first Drive push doesn't silently degrade to "no client_secret.json
    found".
    """
    if (cfg.transformer_secrets / "client_secret.json").is_file():
        return
    if not (cfg.transactions_secrets / "client_secret.json").is_file():
        sys.exit(
            "✖ Drive sync is enabled but no Google credentials exist at "
            f"{cfg.transactions_secrets}/client_secret.json.\n"
            "  Either set up Drive access (see persister/README.md for the OAuth "
            "steps) or run with --no-drive for a fully local run."
        )
    cfg.transformer_secrets.mkdir(mode=0o700, exist_ok=True)
    cfg.transformer_secrets.chmod(0o700)
    copied = []
    for name in _DRIVE_CRED_FILES:
        src = cfg.transactions_secrets / name
        if src.is_file():
            dst = cfg.transformer_secrets / name
            shutil.copy2(src, dst)
            dst.chmod(0o600)
            copied.append(name)
    print(f"  preflight: seeded {cfg.transformer_secrets} with {', '.join(copied)} "
          "from transactions/.secrets (local copy, 0600)")


def _ollama_reachable(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def preflight(cfg: Config, args: argparse.Namespace) -> None:
    """Fail fast, with one combined report, on anything that would break mid-run."""
    problems = []
    for what, path in (
        ("transactions repo", cfg.transactions_dir),
        ("plaid_category_transformer repo", cfg.transformer_dir),
        ("spend_analyzer repo", cfg.analyzer_dir),
        ("transactions venv python", cfg.transactions_python),
        ("transformer venv python", cfg.transformer_python),
        ("analyzer venv streamlit", cfg.analyzer_streamlit),
    ):
        if not path.exists():
            problems.append(f"  - {what} missing: {path}")
    tokens = cfg.transactions_dir / ".secrets" / "tokens.json"
    if not tokens.is_file():
        problems.append(
            f"  - no linked banks ({tokens} missing): run "
            f"`{cfg.transactions_python} app.py` in {cfg.transactions_dir} "
            "and link your banks first"
        )
    # --sheet was asked for explicitly, so a missing converter is a hard failure
    # (unlike the ledger regen, which silently skips when none is configured).
    if args.sheet:
        if not cfg.converter_dir:
            problems.append(
                "  - --sheet needs a configured converter: set "
                "$SPEND_VISUALIZER_CONVERTER or <data_root>/converter_root"
            )
        elif not cfg.converter_python.is_file():
            problems.append(
                f"  - --sheet: converter venv python missing ({cfg.converter_python}); "
                "rebuild the converter's .venv"
            )
    if problems:
        sys.exit("✖ Preflight failed:\n" + "\n".join(problems))

    if not args.no_drive:
        ensure_drive_creds(cfg)
    # The local LLM is OFF by default now, so only warn about Ollama when it was asked for.
    if args.llm and not _ollama_reachable(cfg.ollama_port):
        print(f"  ⚠ --llm was requested but Ollama is not reachable on "
              f"127.0.0.1:{cfg.ollama_port} — the transformer will skip its LLM review "
              "stage (mechanical rules still apply). Start it with `ollama serve`.")


# ── Steps ─────────────────────────────────────────────────────────────────────

@contextmanager
def pipeline_lock(data_root):
    """Hold an exclusive flock for the data steps so a timer-scheduled run and a
    manual one can't interleave writes on the same machine (cross-machine
    concurrency is already handled by the components' Drive reconcile). Held for
    fetch → categorize → push only — the UI runs indefinitely and is read-only,
    so it must not keep the next day's run out. The lock file is never deleted
    (unlinking would race another process opening it)."""
    data_root.mkdir(parents=True, exist_ok=True)
    lock_path = data_root / ".pipeline.lock"
    with lock_path.open("w") as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(f"✖ Another pipeline run holds {lock_path} — a scheduled or "
                     "manual run is in progress; re-run when it finishes.")
        yield


def run_step(name: str, cmd: list[str], cwd) -> None:
    """Run one component to completion, streaming its output; stop on failure."""
    print(f"\n━━━ {name} ━━━")
    t0 = time.monotonic()
    rc = subprocess.run(cmd, cwd=cwd).returncode
    if rc != 0:
        sys.exit(f"\n✖ Pipeline stopped: {name} exited with code {rc}. "
                 "Fix/inspect above, then re-run.")
    print(f"✓ {name} finished in {time.monotonic() - t0:.0f}s")


def convert_ledger(cfg: Config, args: argparse.Namespace) -> None:
    """Optionally regenerate the external budget ledger from the categorized store.

    No-op unless a converter is configured (`cfg.converter_dir`) and `--no-convert`
    wasn't passed. Unlike the data steps, this is NON-fatal: it only derives a view
    over already-persisted data (it never touches the source stores), so a converter
    hiccup must not sink the core pipeline or the UI — it warns loudly and continues."""
    if args.no_convert or not cfg.converter_dir:
        return
    print("\n━━━ convert (external budget ledger) ━━━")
    if not cfg.converter_python.is_file():
        print(f"  ⚠ converter configured at {cfg.converter_dir} but its venv python "
              f"is missing ({cfg.converter_python}); skipping ledger regen. "
              "Rebuild the converter's .venv, or unset its pointer.")
        return
    t0 = time.monotonic()
    rc = subprocess.run(convert_cmd(cfg), cwd=cfg.converter_dir).returncode
    if rc != 0:
        print(f"  ⚠ converter exited {rc}; the Budget tab will use the previous "
              "ledger (or its built-in categorization). Core pipeline unaffected.")
        return
    print(f"✓ convert finished in {time.monotonic() - t0:.0f}s → {cfg.budget_ledger_csv}")


def upload_sheet(cfg: Config, args: argparse.Namespace) -> str | None:
    """(--sheet) Upload the month's ledger as a new Google Sheet via the converter.

    Runs AFTER the lock is released — it's read-only over the categorized store, and
    a slow Google API must not keep the next scheduled run out. NON-fatal like the
    ledger regen: a failed upload just means no Sheet tab to open. Returns the new
    Sheet's URL (read back through the converter's --url-file), or None."""
    print("\n━━━ sheet (monthly Google-Sheet upload) ━━━")
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        url_file = Path(tmp) / "sheet_url.txt"
        rc = subprocess.run(sheet_cmd(cfg, args, str(url_file)),
                            cwd=cfg.converter_dir).returncode
        if rc != 0:
            print(f"  ⚠ sheet upload exited {rc}; no Sheet tab will be opened. "
                  "Core pipeline unaffected — re-run refresh.py in the converter "
                  "to retry the upload alone.")
            return None
        if not url_file.is_file():
            print("  ⚠ converter succeeded but reported no Sheet URL (upload "
                  "skipped/failed inside the converter?) — no Sheet tab to open.")
            return None
        url = url_file.read_text(encoding="utf-8").strip()
    print(f"✓ sheet finished in {time.monotonic() - t0:.0f}s → {url}")
    return url


def pinned_urls(data_root: Path) -> list[str]:
    """Extra tabs for --sheet runs: non-comment lines of ``<data_root>/pinned_tabs``
    (e.g. the master budget spreadsheet). Personal URLs, so the file lives in the
    private data root — missing file just means no extra tabs."""
    f = data_root / "pinned_tabs"
    if not f.is_file():
        return []
    return [line.strip() for line in f.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")]


def wait_for_ui(proc: subprocess.Popen, port: int, timeout_s: float) -> None:
    """Block until the UI accepts TCP on ``port``; die clearly if it never does."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            sys.exit(f"✖ UI process exited (code {proc.returncode}) before serving.")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.3)
    proc.terminate()
    sys.exit(f"✖ UI did not start listening on port {port} within {timeout_s:.0f}s.")


def open_browser(url: str) -> str | None:
    """Open ``url`` in the default browser; return the opener used (None if none).

    On WSL the *Windows* side owns the default browser: prefer wslview, then
    explorer.exe (which reports exit code 1 even on success, so launching is
    best-effort). Elsewhere try xdg-open, then Python's webbrowser.
    """
    on_wsl = "microsoft" in platform.uname().release.lower()
    openers = ("wslview", "explorer.exe") if on_wsl else ("xdg-open", "wslview")
    for opener in openers:
        if shutil.which(opener):
            try:
                subprocess.run([opener, url], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return opener
            except OSError:
                continue
    return "webbrowser" if webbrowser.open(url) else None


def step_analyze(cfg: Config, args: argparse.Namespace,
                 extra_urls: tuple[str, ...] = ()) -> int:
    """Launch the Streamlit UI, open the browser (plus ``extra_urls`` as additional
    tabs — the fresh Sheet and any pinned tabs on --sheet runs), and hand the
    foreground to it."""
    print("\n━━━ analyze (spend_analyzer UI) ━━━")
    proc = subprocess.Popen(ui_cmd(cfg, args), cwd=cfg.analyzer_dir)
    url = f"http://localhost:{args.port}"
    try:
        wait_for_ui(proc, args.port, cfg.ui_start_timeout_s)
        print(f"✓ UI serving at {url}")
        if args.no_browser:
            for u in (url, *extra_urls):
                print(f"  open {u} in your browser (--no-browser was set)")
        else:
            for u in (url, *extra_urls):
                used = open_browser(u)
                print(f"  opened {u} via {used}" if used
                      else f"  ⚠ couldn't auto-open a browser — visit {u}")
        print("  Ctrl+C stops the UI (the fetched/categorized data is already saved).")
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("\n✓ UI stopped.")
        return 0


# ── Entry point ───────────────────────────────────────────────────────────────

def main(argv=None, cfg: Config | None = None) -> None:
    cfg = cfg or default_config()
    args = parse_args(argv, default_port=cfg.ui_port)

    # When stdout is redirected (cron, `./run.py > log`), Python block-buffers it and
    # the status lines below would only land at exit — AFTER the components' own
    # (unbuffered) output, scrambling the log. Stream them line by line instead.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    mode = "OFFLINE (--no-drive)" if args.no_drive else "Drive sync ON"
    if args.push_data:
        mode += " + data git push"
    if args.sheet:
        mode += " + Sheet upload"
    print(f"finance_pipeline — fetch → categorize → analyze   [{mode}]")
    preflight(cfg, args)

    try:
        with pipeline_lock(cfg.data_root):
            run_step("fetch (transactions)", fetch_cmd(cfg, args),
                     cfg.transactions_dir)
            run_step("categorize (plaid_category_transformer)",
                     categorize_cmd(cfg, args), cfg.transformer_dir)
            convert_ledger(cfg, args)   # optional, non-fatal; before push so it's versioned
            if args.push_data:
                print("\n━━━ push data (git → origin) ━━━")
                t0 = time.monotonic()
                push_data(cfg.data_root)
                print(f"✓ push data finished in {time.monotonic() - t0:.0f}s")
    except KeyboardInterrupt:
        # The subprocess got the same SIGINT and is already stopping; just exit cleanly
        # instead of dumping a traceback. Nothing is half-written (components write
        # atomically), so a plain re-run resumes from the durable state.
        sys.exit("\n✖ Interrupted — pipeline stopped. Re-run ./run.py to resume.")

    # Sheet upload + tab collection happen OUTSIDE the lock (read-only over the
    # store; a slow Google API must not block the next scheduled run).
    extra_urls: tuple[str, ...] = ()
    if args.sheet:
        sheet_url = upload_sheet(cfg, args)
        extra_urls = ((sheet_url,) if sheet_url else ()) + tuple(pinned_urls(cfg.data_root))

    if args.no_ui:
        print("\n✓ Data pipeline complete (--no-ui: skipping the Streamlit step).")
        for u in extra_urls:
            print(f"  open {u} in your browser (--no-ui was set)")
        return

    rc = step_analyze(cfg, args, extra_urls)
    if rc != 0:
        sys.exit(rc)


if __name__ == "__main__":
    main()
