# 🎬 Multi-Language YouTube Shorts Automation Pipeline

**Zero-cost ($0/₹0 per month)** fully autonomous pipeline that generates and uploads serialized 3D Pixar-style Indian folklore Shorts twice daily in **Kannada**, **Telugu**, and **Hindi**.

## 🏗️ Architecture

```
story_bank.json (Auto-refilling Story Vault)
        │
        ▼
  ┌─────────────┐     ┌──────────────────┐
  │ story_state │────▶│  Gemini 2.5 Flash │
  │   .json     │     │  (Structured Out) │
  └─────────────┘     └────────┬─────────┘
                               │
            ┌──────────────────┼──────────────────┐
            │                  │                  │
   5 Visual Prompts    3 × 5 Narrations    Localized SEO
   (with Character     (KN, TE, HI)       (titles, tags)
    Visual Anchoring)
            │                  │
            ▼                  ▼
   ┌────────────────┐  ┌──────────────┐
   │ Pollinations.ai│  │  Edge-TTS    │
   │ (5 imgs ONCE)  │  │ (3 voices)   │
   └───────┬────────┘  └──────┬───────┘
           │                  │
           └────────┬─────────┘
                    ▼
           ┌────────────────┐
           │    FFmpeg      │
           │ Ken Burns 9:16 │
           │ 1080x1920@30fps│
           └───────┬────────┘
                   │
        ┌──────────┼──────────┐
        ▼          ▼          ▼
   final_kn.mp4  final_te  final_hi
        │          │          │
        ▼          ▼          ▼
   YouTube KN  YouTube TE  YouTube HI
   Channel     Channel     Channel
```

## 💸 Cost Breakdown ($0 / ₹0 per month)

| Service | Free Tier | Our Usage |
|---------|-----------|-----------|
| Gemini 2.5 Flash | 500 req/day | 2-3 req/day |
| Pollinations.ai | Unlimited | 10 images/day |
| Edge-TTS | Unlimited | 30 clips/day |
| GitHub Actions | 2000 min/month | ~30 min/day |
| YouTube Data API | 10,000 units/day | ~3,200 units/day |
| **Total Cost** | | **$0 / ₹0** |

## 📁 Project Structure

```
yt-multi-lang-shorts/
├── main_pipeline.py          # Core orchestrator (Gemini + Images + TTS + FFmpeg + Vault)
├── upload_to_youtube.py      # Multi-channel YouTube Data API v3 uploader
├── upload_to_meta.py         # Facebook + Instagram Reels publisher (resumable upload)
├── auth_setup.py             # One-time OAuth2 helper (generates tokens + GitHub base64)
├── story_bank.json           # 📚 Auto-refilling story vault with visual anchors & beats
├── story_state.json          # 🔄 Current story & episode tracker
├── requirements.txt          # Python dependencies
├── .env.example              # Environment variables template
├── .gitignore                # Git ignore rules
├── .github/workflows/
│   └── generate_and_upload.yml # GitHub Actions cron workflow (9 AM & 6 PM IST)
├── test_pipeline.py          # Local offline smoke test (TTS + FFmpeg Ken Burns)
└── output_build/             # (runtime directory for builds)
    ├── shared_images/        # 5 AI images downloaded once
    ├── kannada/              # Scene audio & video clips
    ├── telugu/               # Scene audio & video clips
    ├── hindi/                # Scene audio & video clips
    ├── final_kannada.mp4     # Complete Kannada Short
    ├── final_telugu.mp4      # Complete Telugu Short
    └── final_hindi.mp4       # Complete Hindi Short
```

## 📚 Story Vault System (`story_bank.json`)

The story vault contains pre-planned, authentic 5-episode narrative arcs across Indian folklore categories (Panchatantra, Tenali Rama, Vijayanagara Mysteries, Mysore Palace legends, etc.).

Each story contains:
- **`character_visual_anchor`**: Highly detailed Pixar-style character descriptions (specific clothing, colors, facial features, accessories) prepended to every visual prompt. This ensures characters look identical across all 5 scenes of an episode and across all episodes of that arc.
- **`episode_beats`**: Part 1 through Part 5 narrative progression with cliffhangers and moral resolutions.
- **Auto-Refill**: When fewer than 2 uncompleted stories remain in the bank, Gemini automatically generates 4 new authentic folklore arcs and appends them to `story_bank.json`.

## 🚀 Setup & Launch Guide

### Step 1: Set Up Gemini API Key (Free)
1. Get a key from [Google AI Studio](https://aistudio.google.com/apikey).
2. Create your `.env` file:
   ```bash
   cp .env.example .env
   # Add your GEMINI_API_KEY=...
   ```

### Step 2: YouTube Multi-Channel OAuth Setup (One-Time)
1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a project.
2. Enable **YouTube Data API v3**.
3. Create **OAuth 2.0 Client ID** (Desktop Application) and download it as `client_secret.json` into this folder.
4. Run `auth_setup.py` once for each language's YouTube brand channel:
   ```bash
   python3 -m auth_setup --lang kn   # Kannada channel
   python3 -m auth_setup --lang te   # Telugu channel
   python3 -m auth_setup --lang hi   # Hindi channel
   ```
   Each command opens your browser to sign in to the corresponding channel. It saves `token_kn.json`, `token_te.json`, `token_hi.json` and prints the base64 value for GitHub Secrets.

### Step 3: Run Smoke Test Locally
To verify your local FFmpeg, ffprobe, and Edge-TTS setup without using Gemini tokens:
```bash
python3 -m test_pipeline
```

### Step 4: Run the Complete Pipeline Locally
```bash
python3 -m main_pipeline
```

### Step 5: Deploy to GitHub Actions (Twice Daily Automation)
1. Push this repository to GitHub.
2. Go to **Settings → Secrets and variables → Actions** and add:
   - `GEMINI_API_KEY`: Your Gemini API Key
   - `YT_TOKEN_KN`: Base64 string of `token_kn.json`
   - `YT_TOKEN_TE`: Base64 string of `token_te.json`
   - `YT_TOKEN_HI`: Base64 string of `token_hi.json`
   - `META_CREDENTIALS_JSON` *(optional — enables Facebook + Instagram Reels)*: a single JSON string keyed by language code. See **Step 6** below.
3. The cron schedule in `.github/workflows/generate_and_upload.yml` will automatically run at `03:30 UTC` (9:00 AM IST) and `12:30 UTC` (6:00 PM IST) daily.
4. After each run, GitHub Actions commits the updated `story_state.json` and `story_bank.json` back to your repo automatically!

### Step 6: Facebook + Instagram Reels Cross-Posting (Optional)
Each regional Short is also published as a **Facebook Reel** and an **Instagram Reel** when Meta credentials are configured. Video bytes are streamed via Meta's resumable upload (`rupload.facebook.com`), so **no public video hosting is required**.

1. In the Meta Developer App, generate a **permanent Page Access Token** for each Page, and note each `fb_page_id` and the linked `ig_user_id` (IG Business/Creator account).
2. Add a GitHub Actions secret named `META_CREDENTIALS_JSON` containing one JSON string:
   ```json
   {
     "kn": {"fb_page_id": "...", "ig_user_id": "...", "page_access_token": "..."},
     "te": {"fb_page_id": "...", "ig_user_id": "...", "page_access_token": "..."},
     "hi": {"fb_page_id": "...", "ig_user_id": "...", "page_access_token": "..."}
   }
   ```
3. For **local testing**, you may instead drop a `META_CREDENTIALS.json` file (same shape) in the repo root — it is git-ignored. Test one channel with:
   ```bash
   python upload_to_meta.py output_build/<job>/final_kannada.mp4 --lang kn --title "..." --description "..." --tags a b c
   ```

Notes:
- Meta publishing is **best-effort and non-blocking**: a Facebook or Instagram failure is recorded in `job_history.json` (`meta_uploads`) but never stops YouTube uploads or episode advancement. Re-runs skip anything already published.
- If `META_CREDENTIALS_JSON` is absent, Meta publishing is silently skipped. Use `--skip-meta` to render + upload to YouTube only.

## ⚙️ Key Technical Highlights
- **Character Visual Anchoring**: Guarantees visual consistency across scenes and episodes.
- **Generate Once, Dub 3×**: 5 AI images downloaded once via Pollinations Flux API; shared across Kannada, Telugu, and Hindi renders.
- **Edge-TTS Microsoft Neural Voices**: Natural regional voices with 0 latency and $0 cost (`kn-IN-GaganNeural`, `te-IN-MohanNeural`, `hi-IN-MadhurNeural`).
- **Smooth Ken Burns Zoompan**: 9:16 vertical 1080x1920 video at 30fps generated via hardware-accelerated FFmpeg filter.
- **Resumable YouTube Upload**: Chunked 1MB upload with exponential backoff and error handling.
