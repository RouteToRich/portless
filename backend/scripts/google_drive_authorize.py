"""One-time OAuth2 consent flow to mint a Google Drive refresh token.

Google's Drive API has no username/password auth, so document storage
(services/dealmaker, services/docugen) authenticates as your own Google
account via a long-lived refresh token instead. This script performs the
one-time interactive consent to obtain it.

Setup (once):
  1. https://console.cloud.google.com/ -> create/select a project.
  2. APIs & Services -> Library -> enable the "Google Drive API".
  3. APIs & Services -> Credentials -> Create Credentials -> OAuth client ID
     -> Application type "Desktop app". Copy the Client ID and Client Secret.
  4. Put them in backend/.env:
       GOOGLE_DRIVE_CLIENT_ID=...
       GOOGLE_DRIVE_CLIENT_SECRET=...

Run from backend/:
    python scripts/google_drive_authorize.py

A browser window opens for you to sign in and grant access to the Drive
files this app creates (scope: drive.file — it can only see/edit files it
creates, not your whole Drive). On success the script prints
GOOGLE_DRIVE_REFRESH_TOKEN=... — paste that into backend/.env alongside the
client id/secret and document uploads will start using Drive automatically.

Requires: google-auth-oauthlib, google-auth, google-api-python-client
(all in requirements.txt), python-dotenv.
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_ENV_PATH = _HERE.parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main() -> int:
    import os

    client_id = os.getenv("GOOGLE_DRIVE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print(
            "GOOGLE_DRIVE_CLIENT_ID / GOOGLE_DRIVE_CLIENT_SECRET not set in backend/.env.\n"
            "Create an OAuth client (Desktop app) at "
            "https://console.cloud.google.com/apis/credentials and set both "
            "vars before running this script — see the module docstring.",
            file=sys.stderr,
        )
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=_SCOPES)
    creds = flow.run_local_server(port=0)

    if not creds.refresh_token:
        print(
            "No refresh token returned. Google only issues one on first consent "
            "for a given client — revoke prior access at "
            "https://myaccount.google.com/permissions and re-run this script.",
            file=sys.stderr,
        )
        return 1

    print("\nAuthorization complete. Add this to backend/.env:\n")
    print(f"GOOGLE_DRIVE_REFRESH_TOKEN={creds.refresh_token}")
    print(
        "\n(GOOGLE_DRIVE_CLIENT_ID / GOOGLE_DRIVE_CLIENT_SECRET are already set — "
        "leave them as-is. Optionally set GOOGLE_DRIVE_ROOT_FOLDER, default \"Portless\".)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
