"""Google Drive connector for storing generated deal documents (PI/PO, DocuGen docs).

Drive's API has no username/password auth — this uses an OAuth2 refresh token
minted once via ``scripts/google_drive_authorize.py`` against the operator's
own Google account. Set GOOGLE_DRIVE_CLIENT_ID / GOOGLE_DRIVE_CLIENT_SECRET /
GOOGLE_DRIVE_REFRESH_TOKEN to enable; without them ``is_configured()`` is
False and callers fall back to local disk storage (graceful degradation,
same pattern as every other connector in this package).

Documents are organized as ``<GOOGLE_DRIVE_ROOT_FOLDER>/<deal_id>/<filename>``
— the root folder (default "Portless") and per-deal subfolders are created
on first use and cached in-process by folder id.
"""
from __future__ import annotations

import io
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_FOLDER_MIME = "application/vnd.google-apps.folder"
_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class GoogleDriveError(Exception):
    """Raised when a Drive API call fails."""


class GoogleDriveConnector:
    name = "google_drive"

    def __init__(self) -> None:
        self._client_id = os.getenv("GOOGLE_DRIVE_CLIENT_ID", "")
        self._client_secret = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET", "")
        self._refresh_token = os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN", "")
        self._root_folder_name = os.getenv("GOOGLE_DRIVE_ROOT_FOLDER", "Portless")
        self._service: Any = None
        self._root_folder_id: str | None = None
        self._deal_folder_ids: dict[str, str] = {}

    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret and self._refresh_token)

    # -- Drive client -------------------------------------------------------

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service
        from google.oauth2.credentials import Credentials  # type: ignore[import]
        from googleapiclient.discovery import build  # type: ignore[import]

        creds = Credentials(
            token=None,
            refresh_token=self._refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self._client_id,
            client_secret=self._client_secret,
            scopes=_SCOPES,
        )
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    # -- Folder resolution (cached) ------------------------------------------

    def _find_or_create_folder(self, name: str, parent_id: str | None) -> str:
        service = self._get_service()
        safe_name = name.replace("'", "\\'")
        query = f"mimeType='{_FOLDER_MIME}' and name='{safe_name}' and trashed=false"
        if parent_id:
            query += f" and '{parent_id}' in parents"
        try:
            resp = service.files().list(
                q=query, spaces="drive", fields="files(id, name)"
            ).execute()
            files = resp.get("files", [])
            if files:
                return files[0]["id"]
            metadata: dict[str, Any] = {"name": name, "mimeType": _FOLDER_MIME}
            if parent_id:
                metadata["parents"] = [parent_id]
            folder = service.files().create(body=metadata, fields="id").execute()
            return folder["id"]
        except Exception as exc:
            raise GoogleDriveError(f"Failed to resolve Drive folder {name!r}: {exc}") from exc

    def _root_folder(self) -> str:
        if self._root_folder_id is None:
            self._root_folder_id = self._find_or_create_folder(self._root_folder_name, None)
        return self._root_folder_id

    def _deal_folder(self, deal_id: str) -> str:
        if deal_id not in self._deal_folder_ids:
            self._deal_folder_ids[deal_id] = self._find_or_create_folder(deal_id, self._root_folder())
        return self._deal_folder_ids[deal_id]

    # -- Upload ---------------------------------------------------------------

    def upload_document(
        self, deal_id: str, filename: str, content: str, mime_type: str = "text/html"
    ) -> dict[str, str]:
        """Upload *content* as *filename* under ``<root>/<deal_id>/``.

        Overwrites an existing same-named file in that folder instead of
        creating a duplicate (matches re-issue/idempotent-retry semantics).
        Returns ``{"file_id": ..., "web_view_link": ...}``. Blocking network
        I/O — callers on an event loop should run this via ``asyncio.to_thread``.
        """
        from googleapiclient.http import MediaIoBaseUpload  # type: ignore[import]

        service = self._get_service()
        folder_id = self._deal_folder(deal_id)
        media = MediaIoBaseUpload(
            io.BytesIO(content.encode("utf-8")), mimetype=mime_type, resumable=False
        )
        safe_name = filename.replace("'", "\\'")
        query = f"name='{safe_name}' and '{folder_id}' in parents and trashed=false"
        try:
            existing = service.files().list(
                q=query, spaces="drive", fields="files(id)"
            ).execute().get("files", [])

            if existing:
                file = service.files().update(
                    fileId=existing[0]["id"], media_body=media, fields="id, webViewLink"
                ).execute()
            else:
                file = service.files().create(
                    body={"name": filename, "parents": [folder_id]},
                    media_body=media,
                    fields="id, webViewLink",
                ).execute()
        except Exception as exc:
            raise GoogleDriveError(f"Drive upload failed for {filename!r}: {exc}") from exc

        return {"file_id": file["id"], "web_view_link": file.get("webViewLink", "")}


_connector: GoogleDriveConnector | None = None


def get_connector() -> GoogleDriveConnector:
    global _connector
    if _connector is None:
        _connector = GoogleDriveConnector()
    return _connector
