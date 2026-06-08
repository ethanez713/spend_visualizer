"""Sync ONE logical file to a Google Drive file, in place, with native revisions.

This is the key new capability over ``converter/src/uploader.py`` (which is write-only:
it creates a *new* Google Sheet each run with no file_id memory and no download). The
OAuth flow (``_get_credentials``) and folder management (``_get_or_create_folder``) are
copied verbatim from that uploader, keeping the least-privilege ``drive.file`` scope.

Added here:
  * download   — ``files().get_media`` → bytes (to fetch the remote store for reconcile)
  * update-in-place — ``files().update(media_body=…)`` → a new *revision* of the same file
    (exact byte round-trip; we never convert to a Google Sheet, which is lossy)
  * file_id persistence — ``var/drive_state.json`` (``{logical_name: file_id}``, 0600),
    required because the ``drive.file`` scope can only see files this app created.

All Google imports are lazy, so the core (non-Drive) library needs none of the
``google-*`` packages installed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

# Least-privilege scope: the app only ever sees files it created.
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# var/ at the persister repo root: drive_sync.py → persister/ → src/ → <repo>.
_DEFAULT_VAR_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "var"
)

_SETUP_HINT = """\
  Google Drive sync setup (one-time):
    1. console.cloud.google.com → project → Enable Google Drive API.
    2. OAuth consent screen → External → add your account as a Test User.
    3. Credentials → OAuth client ID → Desktop app → download JSON →
       save it to {client_secret} (then: chmod 700 var && chmod 600 var/client_secret.json).
  Then re-run."""


# --- OAuth + folder management (copied verbatim from converter/src/uploader.py) ----

def _get_credentials(client_secret: str, token_path: str):
    """Load cached creds, refreshing if expired, or run the browser OAuth flow."""
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    creds = None
    if os.path.isfile(token_path):
        creds = Credentials.from_authorized_user_file(token_path, GOOGLE_SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(client_secret, GOOGLE_SCOPES)
        # open_browser=False: prints the auth URL to paste into any browser; the local
        # callback server still handles the redirect — works on headless / WSL2.
        print("\n  Open this URL in your browser to authorise Google Drive access:")
        creds = flow.run_local_server(port=0, open_browser=False)

    # Atomic write: temp file + rename so an interrupted save never corrupts token.json.
    dir_ = os.path.dirname(os.path.abspath(token_path))
    os.makedirs(dir_, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(creds.to_json())
        os.chmod(tmp, 0o600)  # token.json holds a refresh token — keep owner-only
        os.replace(tmp, token_path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    return creds


def _get_or_create_folder(service, name: str) -> str:
    """Return the ID of a Drive folder named ``name``, creating it if needed.

    Only finds folders this app created (``drive.file`` scope limitation).
    """
    results = service.files().list(
        q=f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id)",
        pageSize=1,
    ).execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    folder = service.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder"},
        fields="id",
    ).execute()
    print(f"  drive: created Drive folder '{name}'")
    return folder["id"]


class DriveSync:
    """Sync ONE logical file to a Drive file, in place, with native revision history.

    Lazy-imports the google libraries. Remembers the Drive ``file_id`` per logical name
    in ``var/drive_state.json`` so subsequent pushes update the same file (new revision)
    rather than creating duplicates.
    """

    def __init__(self, file_name: str, folder_name: str = "transactions_archive",
                 var_dir: str = _DEFAULT_VAR_DIR):
        self.file_name = file_name
        self.folder_name = folder_name
        self.var_dir = var_dir
        self.client_secret = os.path.join(var_dir, "client_secret.json")
        self.token_path = os.path.join(var_dir, "token.json")
        self.state_path = os.path.join(var_dir, "drive_state.json")
        # Cached Drive service; tests inject a fake here to stub out the network.
        self._service = None

    # --- file_id persistence (var/drive_state.json, 0600) -------------------------

    def _load_state(self) -> dict:
        if not os.path.exists(self.state_path):
            return {}
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict) -> None:
        os.makedirs(self.var_dir, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.var_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.state_path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _file_id(self) -> str | None:
        return self._load_state().get(self.file_name)

    def _remember_file_id(self, file_id: str) -> None:
        state = self._load_state()
        state[self.file_name] = file_id
        self._save_state(state)

    # --- Drive service (lazy; injectable for tests) -------------------------------

    def _get_service(self):
        """Build (and cache) the Drive v3 service, or return None if unavailable.

        Returns the injected ``self._service`` if a test set one. Otherwise requires
        ``client_secret.json`` and the google libraries; on any handled absence it
        prints a hint and returns None (callers treat None as "Drive unavailable").
        """
        if self._service is not None:
            return self._service

        if not os.path.isfile(self.client_secret):
            print(f"  drive: client_secret.json not found at {self.client_secret}")
            print(_SETUP_HINT.format(client_secret=self.client_secret))
            return None
        try:
            from googleapiclient.discovery import build
        except ImportError:
            print("  drive: google packages not installed.")
            print("  Run:  pip install -r requirements.txt")
            return None

        creds = _get_credentials(self.client_secret, self.token_path)
        self._service = build("drive", "v3", credentials=creds)
        return self._service

    # --- public API ---------------------------------------------------------------

    def pull(self) -> bytes | None:
        """Download the current Drive file content as bytes.

        Returns None if no file_id is remembered yet (nothing pushed) or on any error —
        a failed pull must degrade to "no remote" for reconcile, never crash.
        """
        file_id = self._file_id()
        if not file_id:
            return None
        try:
            service = self._get_service()
            if service is None:
                return None
            data = service.files().get_media(fileId=file_id).execute()
            if isinstance(data, str):
                return data.encode("utf-8")
            return bytes(data) if data is not None else None
        except Exception as e:  # noqa: BLE001 — never let a pull failure crash the caller
            print(f"  drive: pull failed for {self.file_name!r}: {type(e).__name__}: {e}")
            return None

    def push(self, local_path: str, mime: str = "application/x-ndjson") -> str | None:
        """Upload ``local_path`` to Drive, in place when the file_id is known.

        Known file_id → ``files().update`` (new revision, same file). Otherwise
        ``files().create`` in ``folder_name`` and remember the new file_id. Returns the
        ``webViewLink`` on success, or None on any error — a failed push must NOT lose
        the local data, so it never raises.
        """
        try:
            service = self._get_service()
            if service is None:
                return None
            from googleapiclient.http import MediaFileUpload

            media = MediaFileUpload(local_path, mimetype=mime, resumable=True)
            file_id = self._file_id()

            if file_id:
                updated = service.files().update(
                    fileId=file_id, media_body=media, fields="id,webViewLink",
                ).execute()
                return updated.get("webViewLink")

            folder_id = (
                _get_or_create_folder(service, self.folder_name)
                if self.folder_name else None
            )
            body: dict = {"name": self.file_name}
            if folder_id:
                body["parents"] = [folder_id]
            created = service.files().create(
                body=body, media_body=media, fields="id,webViewLink",
            ).execute()
            new_id = created.get("id")
            if new_id:
                self._remember_file_id(new_id)
            return created.get("webViewLink")
        except Exception as e:  # noqa: BLE001 — never let a push failure lose local data
            print(f"  drive: push failed for {self.file_name!r}: {type(e).__name__}: {e}")
            return None
