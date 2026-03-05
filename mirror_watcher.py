"""Mirror Watcher — syncs local app files to Google Drive project folder.

Watches C:\\nlm_app\\ for changes to log files, screenshots, and DOM dumps,
then uploads them to the TikTok Auto Scheduler Project folder on Drive.

Usage:
    python mirror_watcher.py

Requires:
    pip install watchdog google-api-python-client google-auth-httplib2 google-auth-oauthlib

Run this in a second terminal alongside run.bat. It uses the same
service account credentials as the main app (credentials.json).
"""

import logging
import os
import time

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ── Config ────────────────────────────────────────────────────────────────────
WATCH_DIR = r"C:\nlm_app"
DRIVE_FOLDER_ID = "1IkiEQcUNqxqf5DhMcl-UwGCzw_W0MWoh"  # TikTok Auto Scheduler Project
CREDENTIALS_PATH = os.path.join(os.path.dirname(__file__), "credentials.json")

# File patterns to mirror (extensions + specific names)
WATCH_EXTENSIONS = {".log", ".png"}
WATCH_NAMES = {"schedule_dom.log", "recent.log", "app.log"}

# Debounce: don't re-upload same file more than once per N seconds
DEBOUNCE_SECONDS = 5

log = logging.getLogger("mirror_watcher")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _build_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_PATH,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds)


class MirrorHandler(FileSystemEventHandler):
    def __init__(self, drive_service):
        super().__init__()
        self._svc = drive_service
        self._last_upload: dict[str, float] = {}
        self._file_ids: dict[str, str] = {}

    def _should_mirror(self, path: str) -> bool:
        name = os.path.basename(path)
        ext = os.path.splitext(name)[1].lower()
        return ext in WATCH_EXTENSIONS or name in WATCH_NAMES

    def _debounced(self, path: str) -> bool:
        now = time.time()
        last = self._last_upload.get(path, 0)
        if now - last < DEBOUNCE_SECONDS:
            return False
        self._last_upload[path] = now
        return True

    def _upload(self, path: str):
        if not os.path.isfile(path):
            return
        if not self._debounced(path):
            return

        name = os.path.basename(path)
        mime = "text/plain" if path.endswith(".log") else "image/png"

        try:
            media = MediaFileUpload(path, mimetype=mime, resumable=False)

            existing_id = self._file_ids.get(name)
            if not existing_id:
                resp = self._svc.files().list(
                    q=f"name='{name}' and '{DRIVE_FOLDER_ID}' in parents and trashed=false",
                    fields="files(id,name)",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ).execute()
                hits = resp.get("files", [])
                if hits:
                    existing_id = hits[0]["id"]
                    self._file_ids[name] = existing_id

            if existing_id:
                self._svc.files().update(
                    fileId=existing_id,
                    media_body=media,
                    supportsAllDrives=True,
                ).execute()
                log.info("Updated Drive: %s", name)
            else:
                meta = {"name": name, "parents": [DRIVE_FOLDER_ID]}
                result = self._svc.files().create(
                    body=meta,
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                ).execute()
                self._file_ids[name] = result["id"]
                log.info("Uploaded new to Drive: %s (id=%s)", name, result["id"])

        except Exception as exc:
            log.warning("Mirror upload failed for %s: %s", name, exc)

    def on_modified(self, event):
        if not event.is_directory and self._should_mirror(event.src_path):
            self._upload(event.src_path)

    def on_created(self, event):
        if not event.is_directory and self._should_mirror(event.src_path):
            self._upload(event.src_path)


def main():
    log.info("Mirror Watcher starting — watching %s", WATCH_DIR)
    log.info("Uploading to Drive folder: %s", DRIVE_FOLDER_ID)

    svc = _build_drive_service()
    handler = MirrorHandler(svc)
    observer = Observer()
    observer.schedule(handler, WATCH_DIR, recursive=True)
    observer.start()
    log.info("Watching. Press Ctrl+C to stop.")

    # Do an initial sync of existing log files on startup
    for fname in ("app.log", "recent.log", "schedule_dom.log"):
        fpath = os.path.join(WATCH_DIR, fname)
        if os.path.isfile(fpath):
            handler._upload(fpath)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    log.info("Mirror Watcher stopped.")


if __name__ == "__main__":
    main()
