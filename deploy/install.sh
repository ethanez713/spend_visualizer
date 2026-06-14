#!/usr/bin/env bash
# Install + enable the server units (user-level systemd — no root except the
# optional linger fallback). Run ON THE SERVER, after RUNBOOK.md §§1-7.
set -euo pipefail

here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
root="$(cd "$here/.." && pwd)"

fail() { echo "✖ $1" >&2; exit 1; }

command -v systemctl >/dev/null || fail "systemctl not found — these units need systemd"
# The unit files address the repo as %h/spend_visualizer; refuse to install a
# clone that lives anywhere else rather than enable units pointing at nothing.
[[ "$root" == "$HOME/spend_visualizer" ]] \
    || fail "units assume ~/spend_visualizer; this clone is at $root — move it (or edit deploy/systemd/*)"
[[ -x "$root/spend_analyzer/venv/bin/streamlit" ]] \
    || fail "spend_analyzer venv missing — build the venvs first (RUNBOOK.md §3)"
[[ -f "$root/finance_pipeline/run.py" ]] \
    || fail "finance_pipeline/run.py missing — incomplete clone?"
[[ -f "$root/transactions/.secrets/tokens.json" ]] \
    || fail "transactions/.secrets/tokens.json missing — migrate secrets first (RUNBOOK.md §4)"

systemctl --user link \
    "$here/systemd/finance-daily.service" \
    "$here/systemd/finance-daily-alert.service"
systemctl --user enable --now \
    "$here/systemd/spend-analyzer.service" \
    "$here/systemd/finance-daily.timer"
systemctl --user daemon-reload

# Keep the user manager (and so the UI + timer) alive without an open session.
loginctl enable-linger "$USER" \
    || echo "⚠ enable-linger needs auth here — run: sudo loginctl enable-linger $USER"

echo
echo "✓ installed."
systemctl --user --no-pager list-timers finance-daily.timer || true
systemctl --user --no-pager status spend-analyzer.service --lines=0 || true
