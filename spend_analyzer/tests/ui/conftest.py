"""Safety + convenience fixtures for the UI suite.

The tests run the REAL app over the REAL archive (read-only). Two autouse
fixtures make that safe by construction:

* ``isolate_writes`` — redirects both UI write paths into pytest's tmp_path:
  the transformer's manual-edit intent log (``manual_edits.edits_path``) and
  the local corrections queue (``corrections.STORE``). No test can append to
  live data even if it presses every save/revoke/delete button.
* ``live_data_untouched`` — hashes the live archive, intent log, and
  corrections store before/after every test and fails loudly on any change
  (belt and suspenders for the requirement that tests never mutate live data).
"""
from __future__ import annotations

import pytest

import corrections as corr
import manual_edits
from tests._liveguard import digest_all
from tests.ui._harness import boot


@pytest.fixture(autouse=True)
def live_data_untouched():
    before = digest_all()
    yield
    assert digest_all() == before, "a UI test mutated live data files!"


@pytest.fixture(autouse=True)
def isolate_writes(monkeypatch, tmp_path):
    """Redirect every UI write path to tmp; returns the tmp intent-log path."""
    edits = tmp_path / "manual_edits.jsonl"
    monkeypatch.setattr(manual_edits, "edits_path", lambda: str(edits))
    monkeypatch.setattr(corr, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(corr, "STORE", tmp_path / "data" / "corrections.jsonl")
    return edits


@pytest.fixture
def at():
    return boot()


@pytest.fixture
def boot_app():
    """The boot function itself, for tests that must seed state before booting."""
    return boot


@pytest.fixture(scope="session")
def cube_df():
    """Ground truth: the same enriched DataFrame the app builds (read-only)."""
    from data import build_cube
    try:
        return build_cube().df
    except FileNotFoundError:
        # Same posture as boot(): no live archive (fresh clone / data root not
        # populated) means the real-archive suite is skipped, never errored.
        pytest.skip("real transaction archive not present")
