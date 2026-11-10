from __future__ import annotations

"""
One-time OAuth2 setup helper for YouTube channel authentication.

Run this script ONCE per language channel to generate token files:
  python3 -m auth_setup --lang kn   # Kannada channel
  python3 -m auth_setup --lang te   # Telugu channel  
  python3 -m auth_setup --lang hi   # Hindi channel

Prerequisites:
  1. Create a project in Google Cloud Console
  2. Enable YouTube Data API v3
  3. Create OAuth 2.0 Client ID (Desktop application)
  4. Download the client_secret.json file to this directory
"""

import argparse
import base64
import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from dotenv import load_dotenv

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

LANG_NAMES = {
    "kn": "Kannada",
    "te": "Telugu",
    "hi": "Hindi",
}


def setup_channel(lang_code: str) -> None:
    client_secret = os.getenv("YT_CLIENT_SECRET_FILE", "client_secret.json")
    if not os.path.isfile(client_secret):
        sys.exit(
            f"[ERROR] '{client_secret}' not found.\n"
            "Download it from: Google Cloud Console -> APIs & Services -> Credentials\n"
            "Choose 'OAuth 2.0 Client IDs' -> Download JSON -> rename to client_secret.json"
        )

    token_file = f"token_{lang_code}.json"
    lang_name = LANG_NAMES.get(lang_code, lang_code)

    print(f"\n{'='*50}")
    print(f"🔐 Setting up YouTube OAuth for {lang_name} ({lang_code}) channel")
    print(f"{'='*50}")
    print(f"\nA browser window will open.")
    print(f"Sign in with the Google account that owns your {lang_name} YouTube channel.")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(token_file, "w") as f:
        f.write(creds.to_json())

    print(f"\n✅ Token saved to: {token_file}")
    
    # Also print base64 for GitHub Secrets
    with open(token_file, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    secret_name = f"YT_TOKEN_{lang_code.upper()}"
    print(f"\n📋 For GitHub Actions, add this as a repository secret:")
    print(f"   Secret Name: {secret_name}")
    print(f"   Secret Value (base64):")
    print(f"   {b64}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Set up YouTube OAuth2 token for a language channel"
    )
    parser.add_argument(
        "--lang", required=True, choices=["kn", "te", "hi"],
        help="Language code: kn (Kannada), te (Telugu), hi (Hindi)"
    )
    args = parser.parse_args()
    setup_channel(args.lang)
