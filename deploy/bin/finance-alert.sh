#!/usr/bin/env bash
# OnFailure hook: append one line to <data_root>/logs/failures.log so a failed
# nightly run is visible at a glance. Resolves the data root exactly like the
# components do ($SPEND_VISUALIZER_DATA → repo-root `data_root` file →
# ~/finance_data) via a stdlib python snippet — no venv needed.
set -euo pipefail

repo_root="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"

data_root="$(python3 - "$repo_root" <<'PY'
import os, sys
from pathlib import Path

root = Path(sys.argv[1])
env = os.environ.get("SPEND_VISUALIZER_DATA")
if env:
    print(Path(env).expanduser())
    raise SystemExit
cfg = root / "data_root"
if cfg.is_file():
    for line in cfg.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            print(Path(line).expanduser())
            raise SystemExit
print(Path("~/finance_data").expanduser())
PY
)"

mkdir -p "$data_root/logs"
printf '%s finance-daily FAILED — inspect: journalctl --user -u finance-daily.service -n 100\n' \
    "$(date '+%Y-%m-%d %H:%M:%S %Z')" >> "$data_root/logs/failures.log"
