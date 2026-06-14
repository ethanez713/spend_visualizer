#!/usr/bin/env bash
# Disable + remove the user units installed by install.sh. Leaves the repos,
# venvs, data, and tailscale config untouched.
set -euo pipefail

systemctl --user disable --now finance-daily.timer spend-analyzer.service 2>/dev/null || true
systemctl --user disable finance-daily.service finance-daily-alert.service 2>/dev/null || true

for unit in finance-daily.service finance-daily.timer \
            finance-daily-alert.service spend-analyzer.service; do
    rm -f "$HOME/.config/systemd/user/$unit"
done
systemctl --user daemon-reload

echo "✓ units removed (repos, venvs, and data left in place)."
