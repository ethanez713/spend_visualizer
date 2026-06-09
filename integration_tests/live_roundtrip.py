#!/usr/bin/env python
"""LIVE Google Drive round-trip test for persister — REAL network egress.

Validates the actual DriveSync push/pull/update path against real Google Drive using
the persister public API exactly as Projects B/C will. It exercises the full lifecycle:

    create (push, no file_id) → pull → update-in-place (push, same file_id) → pull

and asserts the pulled bytes round-trip the local store, and that an update reuses the
SAME Drive file_id (a new *revision*, not a duplicate file).

Prereqs:
  * .secrets/client_secret.json present (OAuth desktop client; chmod 600).
  * First run does a one-time browser OAuth consent → mints .secrets/token.json. Run it
    yourself in your terminal (so you can approve in a browser):
        ./.venv/bin/python integration_tests/live_roundtrip.py

⚠ Uploads data to Google Drive. Uses a DEDICATED test file + folder
  ("persister_live_test"), so it never touches the real transactions archive.
"""
import os
import sys
import tempfile

# Line-buffer stdout so the one-time OAuth consent URL appears immediately even when
# this is run in the background / piped to a file (block-buffering would hide it and the
# process would deadlock waiting on a redirect for a URL you never saw).
try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass

# Make `import persister` work straight from the source tree (no install required).
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from persister import DriveSync, load_jsonl_bytes, save_jsonl  # noqa: E402

FILE_NAME = "persister_live_test.jsonl"
FOLDER = "persister_live_test"


def main() -> int:
    ds = DriveSync(FILE_NAME, folder_name=FOLDER)
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, FILE_NAME)

    # --- v1: create -----------------------------------------------------------
    v1 = {
        "t1": {"transaction_id": "t1", "date": "2026-01-01", "pending": False,
               "amount": 1.0, "name": "Alpha"},
        "t2": {"transaction_id": "t2", "date": "2026-01-02", "pending": False,
               "amount": 2.0, "name": "Beta"},
    }
    save_jsonl(path, v1)

    print("→ push v1 (no file_id yet → files().create)…")
    link = ds.push(path)
    if not link:
        print("FAIL: push v1 returned None (auth / network problem)")
        return 1
    fid1 = ds._file_id()
    print(f"  created: {link}")
    print(f"  remembered file_id in .secrets/drive_state.json: {fid1}")

    print("→ pull v1 (files().get_media) and parse via load_jsonl_bytes…")
    pulled1 = load_jsonl_bytes(ds.pull())
    if pulled1 != v1:
        print("FAIL: pulled v1 != local v1")
        print("  got:", pulled1)
        return 1
    print(f"  PASS: byte round-trip exact — {len(pulled1)} records match local store")

    # --- v2: update in place (same file_id → new revision) --------------------
    v2 = dict(v1)
    v2["t3"] = {"transaction_id": "t3", "date": "2026-01-03", "pending": False,
                "amount": 3.0, "name": "Gamma"}
    save_jsonl(path, v2)

    print("→ push v2 (known file_id → files().update, in place)…")
    link2 = ds.push(path)
    if not link2:
        print("FAIL: push v2 returned None")
        return 1
    fid2 = ds._file_id()
    if fid2 != fid1:
        print(f"FAIL: file_id changed on update ({fid1} → {fid2}) — should be the SAME file")
        return 1
    print(f"  same file_id (in-place update → new Drive revision): {fid2}")

    print("→ pull v2…")
    pulled2 = load_jsonl_bytes(ds.pull())
    if pulled2 != v2:
        print("FAIL: pulled v2 != local v2 (update not reflected)")
        print("  got:", pulled2)
        return 1
    print(f"  PASS: in-place update reflected — {len(pulled2)} records (added t3)")

    print("\n✅ LIVE ROUND-TRIP PASSED")
    print("   create → pull → update-in-place → pull all verified against real Drive.")
    print(f"   Drive: folder '{FOLDER}', file '{FILE_NAME}', file_id {fid2}")
    print("   Revision history is viewable in the Drive UI (file → version history).")
    print("   Cleanup: trash the file/folder in Drive UI and remove the "
          f"'{FILE_NAME}' entry from .secrets/drive_state.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
