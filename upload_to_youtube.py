from __future__ import annotations

import os
import sys
import json
import http.client
import httplib2
import random
import re
import time
import tempfile
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from dotenv import load_dotenv

load_dotenv()

# OAuth2 Scopes needed for YouTube upload
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# Token file per language channel
TOKEN_FILES: dict[str, str] = {
    "kn": "token_kn.json",
    "te": "token_te.json",
    "hi": "token_hi.json",
}

# Retry configuration for resumable uploads
MAX_RETRIES = 5
RETRIABLE_STATUS_CODES = [500, 502, 503, 504]
RETRIABLE_EXCEPTIONS = (httplib2.HttpLib2Error, IOError, http.client.NotConnected,
                         http.client.IncompleteRead, http.client.ImproperConnectionState,
                         http.client.CannotSendRequest, http.client.CannotSendHeader,
                         http.client.ResponseNotReady, http.client.BadStatusLine)
JOB_HISTORY_PATH = Path("job_history.json")

# ---------------------------------------------------------------------------
# Platform-specific metadata formatting (YouTube Shorts)
# ---------------------------------------------------------------------------
YT_MAX_TITLE_CHARS = 60

YT_LANG_NAMES = {"kn": "Kannada", "te": "Telugu", "hi": "Hindi"}

# Exactly 4 targeted hashtags per language for the description (YouTube shows the first 3
# above the title; #Shorts drives the Shorts shelf).
YT_HASHTAGS = {
    "kn": ["#KannadaStories", "#NeethiKathegalu", "#KannadaShorts", "#Shorts"],
    "te": ["#TeluguStories", "#NeethiKathalu", "#TeluguShorts", "#Shorts"],
    "hi": ["#HindiKahaniya", "#MoralStoriesHindi", "#HindiShorts", "#Shorts"],
}

# Curated bilingual (native script + English transliteration) base tags, merged ahead of the
# model's tags so every upload has strong, consistent search coverage.
YT_BASE_TAGS = {
    "kn": ["ಕನ್ನಡ ಕಥೆಗಳು", "ನೀತಿ ಕಥೆ", "kannada stories", "neethi kathegalu",
           "moral stories", "kannada shorts", "panchatantra", "folklore"],
    "te": ["తెలుగు కథలు", "నీతి కథ", "telugu stories", "neethi kathalu",
           "moral stories", "telugu shorts", "panchatantra", "folklore"],
    "hi": ["हिंदी कहानियां", "नीति कथा", "hindi kahaniya", "moral stories hindi",
           "moral stories", "hindi shorts", "panchatantra", "folklore"],
}


def _strip_hashtags_and_part(text: str) -> str:
    """Drop any trailing hashtag block and stray 'Part N' tokens from a model title/line."""
    text = re.sub(r"#\S+", "", text)
    text = re.sub(r"\bPart\s*\d+\b", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" -–—|")


def build_youtube_title(native_title: str, lang_code: str) -> str:
    """<= 60 char native curiosity hook + ' | <Language> Story' suffix."""
    lang_name = YT_LANG_NAMES.get(lang_code, lang_code.upper())
    suffix = f" | {lang_name} Story"
    hook = _strip_hashtags_and_part(native_title)
    budget = YT_MAX_TITLE_CHARS - len(suffix)
    if budget < 10:  # degenerate; just hard-cap the whole thing
        return (hook + suffix)[:YT_MAX_TITLE_CHARS]
    if len(hook) > budget:
        hook = hook[:budget].rstrip(" -–—|,.")
    return f"{hook}{suffix}"


def build_youtube_description(description: str, comment_question: str, lang_code: str) -> str:
    """Line 1 curiosity summary (keywords), Line 2 '💬 {question}', Line 3 exactly 4 hashtags."""
    summary = _strip_hashtags_and_part(description.split("\n")[0]) if description else ""
    hashtags = " ".join(YT_HASHTAGS.get(lang_code, YT_HASHTAGS["hi"]))
    lines = [summary] if summary else []
    q = (comment_question or "").strip()
    if q:
        lines.append(f"💬 {q}")
    lines.append(hashtags)
    return "\n".join(lines)[:5000]


def build_youtube_tags(native_tags: list[str] | None, lang_code: str) -> list[str]:
    """8-10 bilingual keywords: curated base tags first, then fill from the model's tags."""
    out: list[str] = []
    seen: set[str] = set()
    for tag in list(YT_BASE_TAGS.get(lang_code, [])) + list(native_tags or []):
        t = tag.strip().lstrip("#").strip()
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            out.append(t)
        if len(out) >= 10:
            break
    return out


def format_youtube_metadata(
    title: str, description: str, comment_question: str,
    tags: list[str] | None, lang_code: str,
) -> tuple[str, str, list[str]]:
    """One call -> (title, description, tags) formatted for YouTube Shorts."""
    return (
        build_youtube_title(title, lang_code),
        build_youtube_description(description, comment_question, lang_code),
        build_youtube_tags(tags, lang_code),
    )


def _load_job_history() -> dict:
    if not JOB_HISTORY_PATH.exists():
        return {}
    with open(JOB_HISTORY_PATH, "r", encoding="utf-8") as f:
        history = json.load(f)
    if not isinstance(history, dict):
        raise ValueError("job_history.json must contain an object keyed by episode job ID.")
    return history


def _save_job_history(history: dict) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=".", delete=False
    ) as tmp:
        json.dump(history, tmp, indent=2, ensure_ascii=False)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(JOB_HISTORY_PATH)


def _valid_video_id(video_id: object) -> bool:
    return isinstance(video_id, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id))


def _already_uploaded(job_key: str, lang_code: str) -> str | None:
    entry = _load_job_history().get(job_key, {}).get("uploads", {}).get(lang_code, {})
    video_id = entry.get("video_id")
    if entry.get("status") == "uploaded" and _valid_video_id(video_id):
        return video_id
    return None


def _record_upload(job_key: str, lang_code: str, status: str, video_id: str | None = None, error: str | None = None) -> None:
    history = _load_job_history()
    if job_key not in history:
        raise ValueError(f"Cannot record upload: job '{job_key}' is missing from job_history.json.")
    uploads = history[job_key].setdefault("uploads", {})
    uploads[lang_code] = {"status": status, "video_id": video_id}
    if error:
        uploads[lang_code]["error"] = error
    _save_job_history(history)


def _get_authenticated_service(lang_code: str):
    """Build an authenticated YouTube API service for the given language channel."""
    token_file = TOKEN_FILES.get(lang_code)
    if not token_file:
        sys.exit(f"[FATAL] No token file mapping for language code: {lang_code}")

    creds = None

    # Load existing token
    if os.path.isfile(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    # If no valid creds, run OAuth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
        else:
            client_secret = os.getenv("YT_CLIENT_SECRET_FILE", "client_secret.json")
            if not os.path.isfile(client_secret):
                sys.exit(
                    f"[FATAL] {client_secret} not found.\n"
                    "Download it from Google Cloud Console -> APIs & Services -> Credentials -> OAuth 2.0 Client IDs.\n"
                    "See README.md for setup instructions."
                )
            flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
            print(f"\n🔐 Authenticating YouTube channel for language '{lang_code}'...")
            print(f"   A browser window will open. Sign in with the Google account for your {lang_code.upper()} channel.")
            creds = flow.run_local_server(port=0)

        # Save the token for future runs
        with open(token_file, "w") as f:
            f.write(creds.to_json())
        print(f"[Auth] ✅ Token saved to {token_file}")

    return build("youtube", "v3", credentials=creds)


def _resumable_upload(request) -> str:
    """Execute a resumable upload with retry logic. Returns video ID."""
    response = None
    error = None
    retry = 0

    while response is None:
        try:
            print("    Uploading...")
            status, response = request.next_chunk()
            if response is not None:
                if "id" in response:
                    video_id = response["id"]
                    print(f"    ✅ Upload complete! Video ID: {video_id}")
                    print(f"    🔗 https://youtube.com/shorts/{video_id}")
                    return video_id
                else:
                    raise RuntimeError(f"Upload failed with unexpected response: {response}")
        except HttpError as e:
            if e.resp.status in RETRIABLE_STATUS_CODES:
                error = f"HTTP {e.resp.status}: {e.content.decode()}"
            else:
                raise
        except RETRIABLE_EXCEPTIONS as e:
            error = str(e)

        if error:
            retry += 1
            if retry > MAX_RETRIES:
                raise RuntimeError(f"Upload failed after {MAX_RETRIES} retries.")
            sleep_seconds = random.random() * (2 ** retry)
            print(f"    ⚠️  Retriable error: {error}. Retrying in {sleep_seconds:.1f}s...")
            time.sleep(sleep_seconds)
            error = None


def upload_short(
    video_file: str,
    title: str,
    description: str,
    tags: list[str],
    lang_code: str,
    job_key: str | None = None,
    category_id: str = "1",  # Film & Animation
) -> str:
    """Upload a Short to the YouTube channel for the given language.

    Returns the uploaded video ID.
    """
    if job_key:
        existing_video_id = _already_uploaded(job_key, lang_code)
        if existing_video_id:
            print(f"[YouTube] ⏭️  {lang_code.upper()} already uploaded for {job_key}: {existing_video_id}")
            return existing_video_id

    if not os.path.isfile(video_file):
        sys.exit(f"[FATAL] Video file not found: {video_file}")

    youtube = _get_authenticated_service(lang_code)

    # NOTE: `containsSyntheticMedia: True` is not yet available via the YouTube
    # Data API v3. After upload, you MUST manually declare AI-generated content
    # in YouTube Studio -> Video Details -> "Altered or synthetic content".
    # YouTube may add API support in the future.

    body = {
        "snippet": {
            "title": title[:100],  # YouTube title limit
            "description": description[:5000],
            "tags": tags[:30],  # YouTube max 30 tags (though usually ~500 chars total)
            "categoryId": category_id,
            "defaultAudioLanguage": lang_code,
            "defaultLanguage": lang_code,
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
            "madeForKids": False,
        },
    }

    media = MediaFileUpload(
        video_file,
        mimetype="video/mp4",
        resumable=True,
        chunksize=1024 * 1024,  # 1 MB chunks
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    print(f"\n[YouTube] Uploading: {title}")
    print(f"    File: {video_file}")
    print(f"    Language: {lang_code}")
    try:
        video_id = _resumable_upload(request)
    except Exception as exc:
        if job_key:
            _record_upload(job_key, lang_code, "failed", error=str(exc))
        raise

    if job_key:
        _record_upload(job_key, lang_code, "uploaded", video_id=video_id)
    return video_id


# =================================================================
# CLI for manual testing
# =================================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Upload a YouTube Short")
    parser.add_argument("video", help="Path to the .mp4 file")
    parser.add_argument("--lang", required=True, choices=["kn", "te", "hi"])
    parser.add_argument("--title", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--tags", nargs="*", default=[])
    parser.add_argument("--job-key", default=None, help="Episode key in job_history.json for idempotent uploads")
    args = parser.parse_args()

    upload_short(
        video_file=args.video,
        title=args.title,
        description=args.description,
        tags=args.tags,
        lang_code=args.lang,
        job_key=args.job_key,
    )
