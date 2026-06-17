"""Sync ONE logical file to a Google Drive file, in place, with native revisions.

The capability here is in-place sync with native revisions (vs. a write-only uploader
that creates a *new* file each run with no file_id memory and no download). The OAuth
flow (``_get_credentials``) and folder management (``_get_or_create_folder``) keep the
least-privilege ``drive.file`` scope.

Added here:
  * download   — ``files().get_media`` → bytes (to fetch the remote store for reconcile)
  * update-in-place — ``files().update(media_body=…)`` → a new *revision* of the same file
    (exact byte round-trip; we never convert to a Google Sheet, which is lossy)
  * file_id persistence — ``.secrets/drive_state.json`` (``{logical_name: file_id}``, 0600),
    required because the ``drive.file`` scope can only see files this app created.
  * revision audit / rollback — ``list_revisions`` (``revisions().list``) +
    ``pull_revision`` (``revisions().get_media`` → a prior revision's full bytes) +
    ``restore_revision`` (re-push an old revision as a NEW head revision). The Drive API
    returns whole revisions, not diffs; diff two with ``load_jsonl_bytes`` + ``reconcile``.

**Append-only by design — this library can NEVER delete or trash a Drive file.** The real
Drive service is wrapped in a guard (:class:`_GuardedService`) that blocks ``delete`` on
files() and revisions() and rejects any ``trashed=True`` body. Rolling back appends a new
revision; it never destroys history. Delete files yourself in the Drive UI if you must.

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

# .secrets/ at the persister repo root: drive_sync.py → persister/ → src/ → <repo>.
_DEFAULT_SECRETS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".secrets"
)

_SETUP_HINT = """\
  Google Drive sync setup (one-time):
    1. console.cloud.google.com → project → Enable Google Drive API.
    2. OAuth consent screen → External → add your account as a Test User.
    3. Credentials → OAuth client ID → Desktop app → download JSON →
       save it to {client_secret} (then: chmod 700 .secrets && chmod 600 .secrets/client_secret.json).
  Then re-run."""


# --- OAuth + folder management (least-privilege drive.file scope) -------------------

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


# --- Append-only safety: the library must NEVER be able to destroy Drive data ------
# persister only reads, creates, and updates-in-place (each update is a new revision;
# Drive preserves the old ones). It must not hard-delete files/revisions or trash them.
# The guard below wraps the REAL Drive service so that even a future code change — or a
# caller poking at the raw service — cannot delete or trash: `delete` is blocked outright
# and `create`/`update` reject a `trashed` body. Roll back by appending, never deleting.

class AppendOnlyError(PermissionError):
    """Raised when something attempts a destructive Drive op through the guarded service."""


class DrivePullError(RuntimeError):
    """A remote IS known (file_id remembered) but its content could not be fetched.

    Callers must treat this as "remote state unknown" and STOP any reconcile-then-push
    flow: silently proceeding as if there were no remote would rebuild stores without
    the remote-only rows (durable history) and overwrite the Drive head with that loss.
    Distinct from ``pull()`` returning ``None``, which means nothing was ever pushed.
    """


_FORBIDDEN_METHODS = frozenset({"delete"})


def _no_trash(method, method_name: str):
    """Wrap create/update so a request can't trash a file (trashing ≈ deletion)."""
    def _wrapped(*args, **kwargs):
        body = kwargs.get("body")
        if isinstance(body, dict) and body.get("trashed"):
            raise AppendOnlyError(
                f"persister is append-only: Drive '{method_name}' may not set "
                "trashed=True; trash/delete files manually in the Drive UI.")
        return method(*args, **kwargs)
    return _wrapped


class _GuardedResource:
    """Proxies a Drive collection (files()/revisions()), blocking destructive ops."""

    def __init__(self, resource):
        object.__setattr__(self, "_resource", resource)

    def __getattr__(self, name):
        if name in _FORBIDDEN_METHODS:
            raise AppendOnlyError(
                f"persister is append-only: Drive '{name}' is disabled by design; "
                "delete files manually in the Drive UI if you must.")
        attr = getattr(self._resource, name)
        if name in ("create", "update"):
            return _no_trash(attr, name)
        return attr


class _GuardedService:
    """Read/append/update-only proxy over a Drive v3 service.

    Blocks ``delete`` on ``files()`` and ``revisions()`` (no hard-delete; revision history
    can never be destroyed) and rejects trashing via ``create``/``update``. Every other
    call passes straight through.
    """

    def __init__(self, service):
        object.__setattr__(self, "_service", service)

    def files(self):
        return _GuardedResource(self._service.files())

    def revisions(self):
        return _GuardedResource(self._service.revisions())

    def __getattr__(self, name):
        return getattr(self._service, name)


class DriveSync:
    """Sync ONE logical file to a Drive file, in place, with native revision history.

    Lazy-imports the google libraries. Remembers the Drive ``file_id`` per logical name
    in ``.secrets/drive_state.json`` so subsequent pushes update the same file (new revision)
    rather than creating duplicates.
    """

    def __init__(self, file_name: str, folder_name: str = "transactions_archive",
                 secrets_dir: str = _DEFAULT_SECRETS_DIR):
        self.file_name = file_name
        self.folder_name = folder_name
        self.secrets_dir = secrets_dir
        self.client_secret = os.path.join(secrets_dir, "client_secret.json")
        self.token_path = os.path.join(secrets_dir, "token.json")
        self.state_path = os.path.join(secrets_dir, "drive_state.json")
        # Cached Drive service; tests inject a fake here to stub out the network.
        self._service = None

    # --- file_id persistence (.secrets/drive_state.json, 0600) -------------------------

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
        os.makedirs(self.secrets_dir, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.secrets_dir, suffix=".tmp")
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
        # Wrap in the append-only guard so the library can never delete or trash a file.
        self._service = _GuardedService(build("drive", "v3", credentials=creds))
        return self._service

    # --- public API ---------------------------------------------------------------

    def pull(self) -> bytes | None:
        """Download the current Drive file content as bytes.

        Returns None ONLY when no file_id is remembered yet (nothing ever pushed —
        genuinely no remote). When a remote IS known but cannot be fetched (service
        unavailable, API error), raises :class:`DrivePullError`: reconcile flows must
        not mistake a transient failure for an empty remote, or remote-only durable
        history silently drops out of the rebuilt stores and the next push.
        """
        file_id = self._file_id()
        if not file_id:
            return None
        try:
            service = self._get_service()
            if service is None:
                raise DrivePullError(
                    f"remote {self.file_name!r} is known (file_id {file_id}) but the "
                    "Drive service is unavailable — remote state unknown")
            data = service.files().get_media(fileId=file_id).execute()
            if isinstance(data, str):
                return data.encode("utf-8")
            return bytes(data) if data is not None else None
        except DrivePullError:
            raise
        except Exception as e:
            raise DrivePullError(
                f"pull failed for {self.file_name!r}: {type(e).__name__}: {e}") from e

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

    # --- revision audit / rollback (read-only history; rollback appends) ----------

    def list_revisions(self) -> list[dict]:
        """List the synced file's revision history (Drive's order, oldest→newest).

        Each entry: ``{id, modifiedTime, size, ...}``. Every in-place ``push`` leaves a
        revision here, so this is the audit trail / rollback menu. Returns ``[]`` if
        nothing has been pushed yet or on any error (never raises).
        """
        file_id = self._file_id()
        if not file_id:
            return []
        try:
            service = self._get_service()
            if service is None:
                return []
            revisions: list[dict] = []
            page_token = None
            while True:
                kwargs = dict(
                    fileId=file_id,
                    fields="nextPageToken,revisions(id,modifiedTime,size,"
                           "keepForever,originalFilename)",
                    pageSize=200,
                )
                if page_token:
                    kwargs["pageToken"] = page_token
                resp = service.revisions().list(**kwargs).execute()
                revisions.extend(resp.get("revisions", []))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
            return revisions
        except Exception as e:  # noqa: BLE001 — audit must not crash the caller
            print(f"  drive: list_revisions failed for {self.file_name!r}: "
                  f"{type(e).__name__}: {e}")
            return []

    def pull_revision(self, revision_id: str) -> bytes | None:
        """Download the full content of a SPECIFIC prior revision (audit / rollback source).

        Returns the revision's bytes, or ``None`` if nothing pushed / not found / error
        (never raises). Drive returns whole-revision content, not diffs — to diff two
        revisions, ``load_jsonl_bytes`` each and pass them to ``reconcile``.
        """
        file_id = self._file_id()
        if not file_id:
            return None
        try:
            service = self._get_service()
            if service is None:
                return None
            data = service.revisions().get_media(
                fileId=file_id, revisionId=revision_id).execute()
            if isinstance(data, str):
                return data.encode("utf-8")
            return bytes(data) if data is not None else None
        except Exception as e:  # noqa: BLE001 — never let a revision pull crash the caller
            print(f"  drive: pull_revision {revision_id!r} failed for {self.file_name!r}: "
                  f"{type(e).__name__}: {e}")
            return None

    def restore_revision(self, revision_id: str) -> str | None:
        """Roll back by re-pushing a prior revision's content as a NEW head revision.

        Append-only: this does NOT delete or reorder anything — it pulls the chosen
        revision and pushes its bytes back as the latest revision, so every prior
        revision (including the one being rolled back from) remains for audit. Returns
        the new revision's webViewLink, or ``None`` on error.
        """
        data = self.pull_revision(revision_id)
        if data is None:
            print(f"  drive: cannot restore — revision {revision_id!r} unavailable")
            return None
        fd, tmp = tempfile.mkstemp(suffix=".jsonl")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            return self.push(tmp)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
