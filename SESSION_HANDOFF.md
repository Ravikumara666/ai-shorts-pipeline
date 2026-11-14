# Session Handoff — Context for Claude Code

> **Purpose:** Full current-state context for the `yt-multi-lang-shorts` (ai-shorts-pipeline)
> project, so another machine / another Claude session can continue with no gaps.
> Read this top-to-bottom before making changes.
>
> **Last updated:** 2026-11-14

---

## 0. What this project is
A fully automated pipeline that, twice a day, generates a short animated Indian-folklore story
in **Kannada, Telugu, and Hindi** and posts each to **YouTube Shorts + Facebook Reels +
Instagram Reels** — hands-off, for the long term, at ~₹0 cost. Runs entirely on **GitHub
Actions** (repo is **public** → unlimited free Actions minutes).

---

## 1. Current architecture (all LIVE on `main`)

| Part | How it works | Cost |
|---|---|---|
| **Story/script** | Gemini (2 calls): Call 1 = English director blueprint, Call 2 = per-language narration + SEO metadata | tiny (paise) |
| **Content model** | **STANDALONE complete stories** — each run = one full self-contained ~60s story (hook→twist→moral). NOT serialized parts. | — |
| **Images** | **Cloudflare Workers AI FLUX** (free, ~57 img/day) → Gemini safety net. Chain = `cf-first`. | **~₹0** |
| **Voice** | Gemini 3.8 TTS via **REST** (emotion-aware), edge-tts fallback | **paid, small** |
| **Motion** | Ken Burns pan/zoom (ffmpeg zoompan). AI video (I2V) exists but is **OFF** (no free option). | free |
| **Schedule** | cron `0 4,13 * * *` = **09:30 & 18:30 IST**, 2 runs/day | free |

### Image provider chain (`IMAGE_PROVIDER`, default `cf-first`)
`cf-first` = **Cloudflare (free) → Gemini (paid safety net)**. Other modes: `free`
(cloudflare→pollinations→hf), `auto`, `cloudflare`, `gemini`, `pollinations`, `hf`.
Override repo-wide via an Actions **Variable** `IMAGE_PROVIDER`, or per-run via the dispatch input.

---

## 2. Hard limits (why volume = 2 runs/day)
- **🔴 YouTube Data API: 10,000 units/day, 1,600 per upload, 3 uploads/run = 4,800 → max ~2 runs/day.** This is THE ceiling. A 3rd run's uploads fail with quota-exceeded.
- Cloudflare images: ~40–57/day free (~5–7 runs). Not binding.
- Instagram API: ~50 posts/day per account. Not binding.
- GitHub Actions: unlimited (public repo).
- Gemini TTS: paid, scales per run.
> To exceed 2/day you'd need a YouTube quota increase or a separate Google Cloud project per channel.
> Do NOT switch to 5–10 min long-form — it loses the Shorts/Reels discovery push and costs far more.

---

## 3. Secrets (GitHub → Settings → Secrets and variables → Actions)
| Secret | Purpose | Status |
|---|---|---|
| `GEMINI_API_KEY` | text + voice (+ image safety net) | set |
| `CF_ACCOUNT_ID`, `CF_API_TOKEN` | **Cloudflare free images** | set |
| `YT_TOKEN_KN/TE/HI` | YouTube OAuth (base64) | set |
| `META_CREDENTIALS_JSON` | FB + IG tokens (keyed kn/te/hi: fb_page_id, ig_user_id, page_access_token) | set |
| `HF_TOKEN`, `POLLINATIONS_API_KEY` | optional image fallbacks | optional |

---

## 4. Key files
- `main_pipeline.py` — the whole pipeline (story gen, images, render, orchestration).
  - `generate_episode_data()` — Call 1 (standalone director blueprint) + Call 2 (per-language).
  - `generate_shared_images()` + `_download_via_cloudflare()` / `_download_via_gemini_image()` / `_download_via_hf_flux()` — provider chain.
  - `build_language_video()` — ffmpeg render (Ken Burns, subtitles, BGM, **+faststart** for IG).
  - `_gemini_tts_to_mp3()` / `synthesize_scene_audio()` — voice (REST, emotion cue).
  - `refill_story_vault()` — auto-generates new standalone stories when the bank runs low.
  - `video_animator.py` — free HF ZeroGPU I2V (currently unused; `USE_AI_VIDEO` default off).
- `upload_to_youtube.py` — idempotent YT upload (`_already_uploaded`).
- `upload_to_meta.py` — FB + IG Reels (resumable rupload). `_upload_binary` reads bytes (retry-safe).
- `story_bank.json` — story seeds (6). `story_state.json` — `current_story_id`, `current_episode=1`, `max_episodes_in_arc=1`, `completed_story_ids`. `job_history.json` — per-story upload ledger (idempotency).
- `.github/workflows/generate_and_upload.yml` — schedule, env, pre-flight gate, commit-state-back step.

---

## 5. How it runs autonomously (the loop)
Each scheduled run: pick current story → generate complete script (3 langs) → Cloudflare images
→ Gemini voice → Ken Burns render (+faststart) → upload YT/FB/IG → record in `job_history`
→ if YT uploads all succeeded, **advance to next story** → when <2 stories remain,
**`refill_story_vault`** invents 4 new standalone stories → bot commits updated state back to
`main` (`[skip ci]`). Repeat 2×/day. The commit-back keeps the repo active so GitHub never
auto-disables the cron.

---

## 6. Long-term risks to watch (NOT code — these can silently stop it)
1. **YouTube OAuth app must be "In production"**, not "Testing" — else Google expires refresh tokens every 7 days and uploads fail. (Google Cloud Console → OAuth consent screen → Publish App.)
2. **Meta tokens must be permanent** (System User token) — normal page tokens expire ~60 days → FB/IG stops.
3. **Gemini credit (~₹582) expires ~1 week** → after that, voice (and rare image fallback) cost small money. For true ₹0, set `TTS_ENGINE=edge` (free edge-tts, lower quality).
4. Over 2 years a provider may change an API / retire a model → occasional 1-line fix.

---

## 7. Recent fixes (newest first)
- **IG `+faststart`** — Instagram rejected all Reels with `ProcessingFailedError` (moov atom at end). Added `-movflags +faststart` to the final MP4. (FB was unaffected.)
- **Standalone stories** — replaced serialized Part 1–5 arcs with complete self-contained stories (better cold discovery), stronger hooks, no-"Part N" titles, cleaner hashtags.
- **Cloudflare free images** — `cf-first` chain; CF payload is `{prompt, steps}` only (extra fields → 400).
- **IG kn/te retry bug** — `_upload_binary` now reads bytes once (was re-sending a consumed file handle on retry → 0 bytes → 400).
- **CI hardening** — commit step skipped on `skip_upload` runs; push retries on transient GitHub 500.
- **Gemini TTS via REST** — SDK-version-proof (newer google-genai broke `speechConfig`).

---

## 8. Current state (as of this handoff)
- `main` is the live production branch. Story pointer is at **`story_002`** (story_001 posted to YT+FB; its IG was missed by the pre-faststart bug — one-time gap).
- **Next scheduled run (18:30 IST) will post story_002 to all 3 platforms including Instagram** and confirm the faststart fix.
- Merged & closed PRs: #3 (Cloudflare images), #4 (standalone stories). Stale branches may remain.

---

## 9. How to test safely (no cost)
Manual run: **Actions → Run workflow**, pick branch, set `skip_upload=true` (render only, no
upload, no state change), `image_provider=cloudflare` for a free image test → download the
`shorts-preview` artifact. **Do NOT run local tests that hit paid Gemini APIs** (burns quota) —
commit and let CI validate. Prefer testing new features on a PR branch, merge to `main` only
after quality is confirmed.
