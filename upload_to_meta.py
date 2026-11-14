from __future__ import annotations

"""
Cross-platform Reels publishing to Facebook Pages and Instagram Business/Creator
accounts for every regional channel (kn / te / hi).

Both platforms are published with Meta's *resumable binary upload* protocol via
``rupload.facebook.com`` so NO publicly hosted video URL is required — the raw
``.mp4`` bytes are streamed straight from the runner (ideal for GitHub Actions).

Facebook Reels — 3 phases (https://developers.facebook.com/documentation/video-api/guides/reels-publishing):
    1. start   POST /{page_id}/video_reels?upload_phase=start            -> video_id + upload_url
    2. upload  POST rupload.facebook.com/video-upload/{v}/{video_id}      (Authorization: OAuth, offset, file_size)
    3. finish  POST /{page_id}/video_reels?upload_phase=finish&video_state=PUBLISHED&description=...

Instagram Reels — resumable container
(https://developers.facebook.com/docs/instagram-platform/content-publishing/):
    1. create  POST /{ig_user_id}/media?media_type=REELS&upload_type=resumable&caption=...  -> {id, uri}
    2. upload  POST rupload.facebook.com/ig-api-upload/{v}/{container_id}  (Authorization: OAuth, offset, file_size)
    3. poll    GET  /{container_id}?fields=status_code  until FINISHED
    4. publish POST /{ig_user_id}/media_publish?creation_id={container_id}  -> media_id

Idempotency: every published post/media id is recorded in ``job_history.json`` under
``meta_uploads[<lang>][<platform>]`` so re-runs never double-post.
"""

import json
import os
import sys
import time
import tempfile
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v23.0")
GRAPH_HOST = f"https://graph.facebook.com/{GRAPH_VERSION}"
RUPLOAD_HOST = f"https://rupload.facebook.com"

JOB_HISTORY_PATH = Path("job_history.json")
META_CREDENTIALS_FILE = Path("META_CREDENTIALS.json")

SUPPORTED_LANGS = ("kn", "te", "hi")
PLATFORMS = ("facebook", "instagram")

# Instagram caption hard limit is 2200 chars / 30 hashtags; keep both platforms consistent.
MAX_CAPTION_CHARS = 2200
MAX_HASHTAGS = 20

# Network resilience
HTTP_TIMEOUT = 300          # seconds for the large binary upload
MAX_RETRIES = 4
RETRIABLE_STATUS = {429, 500, 502, 503, 504}

# Instagram container processing poll
IG_POLL_INTERVAL = 6        # seconds between status checks
IG_POLL_MAX_ATTEMPTS = 60   # ~6 minutes cap
# Facebook reel processing poll (non-blocking; publish is async once finished)
FB_POLL_INTERVAL = 6
FB_POLL_MAX_ATTEMPTS = 20    # ~2 minutes; FB publishes async, so this is best-effort


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def load_meta_credentials() -> dict:
    """Load Meta credentials from ``META_CREDENTIALS_JSON`` env var (preferred, used in CI)
    or fall back to a local ``META_CREDENTIALS.json`` file for local testing.

    Returns an empty dict if nothing is configured (caller decides whether to skip).
    """
    raw = os.getenv("META_CREDENTIALS_JSON")
    if raw:
        try:
            creds = json.loads(raw)
        except json.JSONDecodeError as exc:
            sys.exit(f"[FATAL] META_CREDENTIALS_JSON is not valid JSON: {exc}")
    elif META_CREDENTIALS_FILE.exists():
        with open(META_CREDENTIALS_FILE, "r", encoding="utf-8") as f:
            creds = json.load(f)
    else:
        return {}

    if not isinstance(creds, dict):
        sys.exit("[FATAL] Meta credentials must be a JSON object keyed by language code.")

    # Validate shape for each present language.
    for lang, cfg in creds.items():
        if not isinstance(cfg, dict):
            sys.exit(f"[FATAL] Meta credentials for '{lang}' must be an object.")
        missing = [k for k in ("fb_page_id", "ig_user_id", "page_access_token") if not cfg.get(k)]
        if missing:
            sys.exit(f"[FATAL] Meta credentials for '{lang}' missing: {', '.join(missing)}")
    return creds


# ---------------------------------------------------------------------------
# Job history (idempotency ledger) — mirrors upload_to_youtube.py conventions
# ---------------------------------------------------------------------------
def _load_job_history() -> dict:
    if not JOB_HISTORY_PATH.exists():
        return {}
    with open(JOB_HISTORY_PATH, "r", encoding="utf-8") as f:
        history = json.load(f)
    if not isinstance(history, dict):
        raise ValueError("job_history.json must contain an object keyed by episode job ID.")
    return history


def _save_job_history(history: dict) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=".", delete=False) as tmp:
        json.dump(history, tmp, indent=2, ensure_ascii=False)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(JOB_HISTORY_PATH)


def _already_published(job_key: str, lang_code: str, platform: str) -> str | None:
    entry = (
        _load_job_history()
        .get(job_key, {})
        .get("meta_uploads", {})
        .get(lang_code, {})
        .get(platform, {})
    )
    post_id = entry.get("post_id")
    if entry.get("status") == "published" and isinstance(post_id, str) and post_id:
        return post_id
    return None


def _record_meta(
    job_key: str,
    lang_code: str,
    platform: str,
    status: str,
    post_id: str | None = None,
    error: str | None = None,
) -> None:
    history = _load_job_history()
    job = history.setdefault(job_key, {})
    meta_uploads = job.setdefault("meta_uploads", {})
    lang_entry = meta_uploads.setdefault(lang_code, {})
    record = {"status": status, "post_id": post_id}
    if error:
        record["error"] = error
    lang_entry[platform] = record
    _save_job_history(history)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    """Issue an HTTP request, retrying transient 429/5xx + connection errors with backoff."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, timeout=HTTP_TIMEOUT, **kwargs)
            if resp.status_code in RETRIABLE_STATUS and attempt < MAX_RETRIES:
                wait = 2 ** attempt
                print(f"    ⚠️  HTTP {resp.status_code} from Meta. Retrying in {wait}s "
                      f"(attempt {attempt}/{MAX_RETRIES})...")
                time.sleep(wait)
                continue
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                wait = 2 ** attempt
                print(f"    ⚠️  Network error: {exc}. Retrying in {wait}s "
                      f"(attempt {attempt}/{MAX_RETRIES})...")
                time.sleep(wait)
            else:
                raise
    if last_exc:
        raise last_exc
    raise RuntimeError("Unreachable retry state")


def _json_or_raise(resp: requests.Response, context: str) -> dict:
    try:
        data = resp.json()
    except ValueError:
        data = {"_raw": resp.text}
    if not resp.ok or (isinstance(data, dict) and data.get("error")):
        err = data.get("error") if isinstance(data, dict) else data
        # Fall back to the full body when there's no explicit "error" field, so a 400 with an
        # odd payload is debuggable instead of printing a useless "None".
        if err is None:
            err = data if data else (resp.text[:300] or "<empty body>")
        raise RuntimeError(f"{context} failed (HTTP {resp.status_code}): {err}")
    return data


def _upload_binary(
    upload_url: str, token: str, video_file: str, raise_on_error: bool = True
) -> str | None:
    """Stream raw .mp4 bytes to an rupload endpoint (shared by FB & IG).

    Returns None on success. When ``raise_on_error`` is False, a failed upload returns the
    error string instead of raising — used by Instagram, where Meta frequently returns a
    non-retriable ProcessingFailedError on this step even though the container keeps
    processing and reaches FINISHED a few seconds later. The caller then verifies via the
    container status poll before deciding the upload really failed.
    """
    file_size = os.path.getsize(video_file)
    headers = {
        "Authorization": f"OAuth {token}",
        "offset": "0",
        "file_size": str(file_size),
        "Content-Type": "application/octet-stream",
    }
    # Read the whole file into memory (Reels are only a few MB) rather than passing an open
    # file handle: _request_with_retry re-issues the POST on a transient 429/5xx, and a handle
    # left at EOF from the first attempt would re-upload 0 bytes -> HTTP 400 (the IG kn/te bug).
    with open(video_file, "rb") as fh:
        payload = fh.read()
    resp = _request_with_retry("POST", upload_url, headers=headers, data=payload)
    try:
        data = _json_or_raise(resp, "Binary upload")
    except RuntimeError as exc:
        if raise_on_error:
            raise
        return str(exc)
    # FB returns {"success": true}; IG returns {"success": true} as well.
    if isinstance(data, dict) and data.get("success") is False:
        msg = f"Binary upload reported failure: {data}"
        if raise_on_error:
            raise RuntimeError(msg)
        return msg
    return None


def build_caption(title: str, description: str, tags: list[str] | None = None) -> str:
    """Compose a Reels caption from the localized title/description, appending SEO
    tags as hashtags (deduped against text already present), within platform limits."""
    parts = [p for p in (title.strip(), description.strip()) if p]
    caption = "\n\n".join(parts)

    if tags:
        existing = caption.lower()
        hashtags = []
        for tag in tags:
            cleaned = "".join(ch for ch in tag if ch.isalnum() or ch in "_")
            if not cleaned:
                continue
            tag_token = f"#{cleaned}"
            if tag_token.lower() in existing or tag_token in hashtags:
                continue
            hashtags.append(tag_token)
            if len(hashtags) >= MAX_HASHTAGS:
                break
        if hashtags:
            caption = f"{caption}\n\n{' '.join(hashtags)}"

    return caption[:MAX_CAPTION_CHARS]


# ---------------------------------------------------------------------------
# Facebook Reels
# ---------------------------------------------------------------------------
def publish_facebook_reel(video_file: str, caption: str, page_id: str, token: str) -> str:
    """Publish a Reel to a Facebook Page. Returns the Facebook video/reel ID."""
    # Phase 1: start
    start = _request_with_retry(
        "POST",
        f"{GRAPH_HOST}/{page_id}/video_reels",
        data={"upload_phase": "start", "access_token": token},
    )
    start_data = _json_or_raise(start, "Facebook Reel start")
    video_id = start_data.get("video_id")
    upload_url = start_data.get("upload_url")
    if not video_id:
        raise RuntimeError(f"Facebook Reel start returned no video_id: {start_data}")
    if not upload_url:
        upload_url = f"{RUPLOAD_HOST}/video-upload/{GRAPH_VERSION}/{video_id}"
    print(f"    [FB] Session started (video_id={video_id})")

    # Phase 2: upload bytes
    print(f"    [FB] Uploading video bytes...")
    _upload_binary(upload_url, token, video_file)

    # Phase 3: finish + publish
    finish = _request_with_retry(
        "POST",
        f"{GRAPH_HOST}/{page_id}/video_reels",
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": caption,
            "access_token": token,
        },
    )
    _json_or_raise(finish, "Facebook Reel finish")
    print(f"    [FB] Finish accepted; encoding/publishing asynchronously.")

    # Best-effort status poll (FB publishes async once processing completes).
    for _ in range(FB_POLL_MAX_ATTEMPTS):
        status_resp = _request_with_retry(
            "GET",
            f"{GRAPH_HOST}/{video_id}",
            params={"fields": "status", "access_token": token},
        )
        try:
            status_data = status_resp.json()
        except ValueError:
            break
        video_status = (status_data.get("status") or {}).get("video_status")
        if video_status in ("ready", "published"):
            print(f"    [FB] ✅ Reel published: {video_id}")
            return video_id
        if video_status == "error":
            raise RuntimeError(f"Facebook Reel processing failed: {status_data}")
        time.sleep(FB_POLL_INTERVAL)

    # Not fatal: finish succeeded, so FB will publish once processing completes.
    print(f"    [FB] ✅ Reel accepted (still processing): {video_id}")
    return video_id


# ---------------------------------------------------------------------------
# Instagram Reels
# ---------------------------------------------------------------------------
def publish_instagram_reel(video_file: str, caption: str, ig_user_id: str, token: str) -> str:
    """Publish a Reel to an Instagram Business/Creator account. Returns the IG media ID."""
    # Phase 1: create resumable container
    create = _request_with_retry(
        "POST",
        f"{GRAPH_HOST}/{ig_user_id}/media",
        data={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "access_token": token,
        },
    )
    create_data = _json_or_raise(create, "Instagram container create")
    container_id = create_data.get("id")
    upload_url = create_data.get("uri")
    if not container_id:
        raise RuntimeError(f"Instagram container create returned no id: {create_data}")
    if not upload_url:
        upload_url = f"{RUPLOAD_HOST}/ig-api-upload/{GRAPH_VERSION}/{container_id}"
    print(f"    [IG] Container created (id={container_id})")

    # Phase 2: upload bytes. Do NOT abort on an upload error — Meta often returns a
    # non-retriable ProcessingFailedError here while the container still finishes
    # processing. We verify the real outcome via the container status poll below.
    print(f"    [IG] Uploading video bytes...")
    upload_err = _upload_binary(upload_url, token, video_file, raise_on_error=False)
    if upload_err:
        print(f"    [IG] ⚠️  Upload step returned an error ({upload_err}). "
              f"Verifying container status before giving up...")

    # Phase 3: poll container until FINISHED (mandatory before publish)
    print(f"    [IG] Waiting for container processing...")
    last_status: dict = {}
    for attempt in range(IG_POLL_MAX_ATTEMPTS):
        status_resp = _request_with_retry(
            "GET",
            f"{GRAPH_HOST}/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
        )
        status_data = _json_or_raise(status_resp, "Instagram container status")
        last_status = status_data
        code = status_data.get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise RuntimeError(
                f"Instagram container processing failed: {status_data}"
                + (f" (upload step error: {upload_err})" if upload_err else "")
            )
        time.sleep(IG_POLL_INTERVAL)
    else:
        # Never reached FINISHED. If the upload itself errored, surface that as the cause.
        raise RuntimeError(
            f"Instagram container {container_id} not FINISHED after "
            f"{IG_POLL_INTERVAL * IG_POLL_MAX_ATTEMPTS}s (last status: {last_status})"
            + (f"; upload step error: {upload_err}" if upload_err else "")
        )

    # Phase 4: publish
    publish = _request_with_retry(
        "POST",
        f"{GRAPH_HOST}/{ig_user_id}/media_publish",
        data={"creation_id": container_id, "access_token": token},
    )
    publish_data = _json_or_raise(publish, "Instagram media_publish")
    media_id = publish_data.get("id")
    if not media_id:
        raise RuntimeError(f"Instagram media_publish returned no id: {publish_data}")
    print(f"    [IG] ✅ Reel published: {media_id}")
    return media_id


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def publish_reel(
    video_file: str,
    caption: str,
    lang_code: str,
    job_key: str | None = None,
    credentials: dict | None = None,
) -> dict:
    """Publish a single localized Reel to BOTH Facebook and Instagram for one language.

    Each platform is independent and best-effort: a failure on one is recorded and
    does not prevent the other. Returns {"facebook": id|None, "instagram": id|None}.
    """
    if not os.path.isfile(video_file):
        sys.exit(f"[FATAL] Video file not found: {video_file}")

    creds = credentials if credentials is not None else load_meta_credentials()
    lang_creds = creds.get(lang_code)
    if not lang_creds:
        print(f"[Meta] ⏭️  No credentials configured for '{lang_code}'. Skipping.")
        return {"facebook": None, "instagram": None}

    page_id = lang_creds["fb_page_id"]
    ig_user_id = lang_creds["ig_user_id"]
    token = lang_creds["page_access_token"]

    results: dict[str, str | None] = {"facebook": None, "instagram": None}

    # ---- Facebook ----
    existing_fb = _already_published(job_key, lang_code, "facebook") if job_key else None
    if existing_fb:
        print(f"[Meta] ⏭️  {lang_code.upper()} Facebook already published: {existing_fb}")
        results["facebook"] = existing_fb
    else:
        print(f"\n[Meta] Publishing {lang_code.upper()} Reel to Facebook Page {page_id}...")
        try:
            fb_id = publish_facebook_reel(video_file, caption, page_id, token)
            results["facebook"] = fb_id
            if job_key:
                _record_meta(job_key, lang_code, "facebook", "published", post_id=fb_id)
        except Exception as exc:
            print(f"[Meta] ❌ Facebook publish failed for {lang_code.upper()}: {exc}")
            if job_key:
                _record_meta(job_key, lang_code, "facebook", "failed", error=str(exc))

    # ---- Instagram ----
    existing_ig = _already_published(job_key, lang_code, "instagram") if job_key else None
    if existing_ig:
        print(f"[Meta] ⏭️  {lang_code.upper()} Instagram already published: {existing_ig}")
        results["instagram"] = existing_ig
    else:
        print(f"\n[Meta] Publishing {lang_code.upper()} Reel to Instagram {ig_user_id}...")
        try:
            ig_id = publish_instagram_reel(video_file, caption, ig_user_id, token)
            results["instagram"] = ig_id
            if job_key:
                _record_meta(job_key, lang_code, "instagram", "published", post_id=ig_id)
        except Exception as exc:
            print(f"[Meta] ❌ Instagram publish failed for {lang_code.upper()}: {exc}")
            if job_key:
                _record_meta(job_key, lang_code, "instagram", "failed", error=str(exc))

    return results


# ---------------------------------------------------------------------------
# CLI for manual testing
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Publish a Reel to Facebook + Instagram")
    parser.add_argument("video", help="Path to the .mp4 file")
    parser.add_argument("--lang", required=True, choices=list(SUPPORTED_LANGS))
    parser.add_argument("--title", default="")
    parser.add_argument("--description", default="")
    parser.add_argument("--tags", nargs="*", default=[])
    parser.add_argument("--caption", default=None, help="Explicit caption (overrides title/description/tags)")
    parser.add_argument("--job-key", default=None, help="Episode key in job_history.json for idempotency")
    parser.add_argument("--platform", choices=["both", "facebook", "instagram"], default="both")
    args = parser.parse_args()

    caption = args.caption or build_caption(args.title, args.description, args.tags)

    creds = load_meta_credentials()
    if not creds:
        sys.exit("[FATAL] No Meta credentials found (set META_CREDENTIALS_JSON or add META_CREDENTIALS.json).")

    lang_creds = creds.get(args.lang)
    if not lang_creds:
        sys.exit(f"[FATAL] No Meta credentials for language '{args.lang}'.")

    if args.platform == "both":
        out = publish_reel(args.video, caption, args.lang, job_key=args.job_key, credentials=creds)
        print(f"\nResult: {json.dumps(out, indent=2)}")
    elif args.platform == "facebook":
        fb_id = publish_facebook_reel(
            args.video, caption, lang_creds["fb_page_id"], lang_creds["page_access_token"]
        )
        print(f"\nFacebook Reel ID: {fb_id}")
    else:
        ig_id = publish_instagram_reel(
            args.video, caption, lang_creds["ig_user_id"], lang_creds["page_access_token"]
        )
        print(f"\nInstagram media ID: {ig_id}")
