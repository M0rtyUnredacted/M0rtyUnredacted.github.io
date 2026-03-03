"""Google Drive API helper — list files, download files, move files."""

import io
import os
import logging
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2 import service_account

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]


def _build_service(credentials_path: str = "credentials.json"):
    creds = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


class DriveClient:
    def __init__(self, credentials_path: str = "credentials.json"):
        self.service = _build_service(credentials_path)

    # ── Listing ──────────────────────────────────────────────────────────────

    def list_files(self, folder_id: str, mime_filter: str | None = None) -> list[dict]:
        """Return all files directly inside *folder_id*."""
        q = f"'{folder_id}' in parents and trashed = false"
        if mime_filter:
            q += f" and mimeType = '{mime_filter}'"

        results, page_token = [], None
        while True:
            resp = (
                self.service.files()
                .list(
                    q=q,
                    spaces="drive",
                    fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
                    pageToken=page_token,
                )
                .execute()
            )
            results.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return results

    def list_docs(self, folder_id: str) -> list[dict]:
        return self.list_files(folder_id, mime_filter="application/vnd.google-apps.document")

    def list_mp4s(self, folder_id: str) -> list[dict]:
        return self.list_files(folder_id, mime_filter="video/mp4")

    def list_markdowns(self, folder_id: str) -> list[dict]:
        """Return .md files (stored as plain text)."""
        return self.list_files(folder_id, mime_filter="text/plain")

    # ── Downloading ──────────────────────────────────────────────────────────

    def download_file(self, file_id: str, dest_path: str) -> str:
        """Download a binary Drive file to *dest_path*. Returns path."""
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        request = self.service.files().get_media(fileId=file_id)
        with io.FileIO(dest_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        log.info("Downloaded Drive file %s → %s", file_id, dest_path)
        return dest_path

    def export_doc_as_text(self, file_id: str) -> str:
        """Export a Google Doc as plain text and return the string."""
        resp = (
            self.service.files()
            .export(fileId=file_id, mimeType="text/plain")
            .execute()
        )
        return resp.decode("utf-8") if isinstance(resp, bytes) else resp

    def read_plain_text(self, file_id: str) -> str:
        """Download a plain-text Drive file and return its contents."""
        request = self.service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue().decode("utf-8")

    # ── Moving ───────────────────────────────────────────────────────────────

    def move_file(self, file_id: str, dest_folder_id: str) -> None:
        """Move file to a different folder (removes from all current parents)."""
        file_meta = (
            self.service.files().get(fileId=file_id, fields="parents").execute()
        )
        previous_parents = ",".join(file_meta.get("parents", []))
        self.service.files().update(
            fileId=file_id,
            addParents=dest_folder_id,
            removeParents=previous_parents,
            fields="id, parents",
        ).execute()
        log.info("Moved Drive file %s → folder %s", file_id, dest_folder_id)

    # ── Upload ───────────────────────────────────────────────────────────────

    def upload_file(self, local_path: str, folder_id: str, mime_type: str = "video/mp4") -> str:
        """Upload a local file to Drive and return its new file ID."""
        from googleapiclient.http import MediaFileUpload

        name = os.path.basename(local_path)
        media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)
        meta = {"name": name, "parents": [folder_id]}
        file = (
            self.service.files()
            .create(body=meta, media_body=media, fields="id")
            .execute()
        )
        log.info("Uploaded %s → Drive folder %s (id=%s)", local_path, folder_id, file["id"])
        return file["id"]
