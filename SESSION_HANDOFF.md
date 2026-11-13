# Session Handoff — Context for Claude Code

> **Purpose:** This file summarizes everything changed in the recent Claude Code session on
> the `yt-multi-lang-shorts` (ai-shorts-pipeline) project, so another machine / another Claude
> session can pick up with full context. Read this top-to-bottom before making changes.
>
> **Last updated:** 2026-11-13

---

## 0. TL;DR — what was done

1. **Meta Reels publishing** — each regional Short is now also posted to **Facebook Reels + Instagram Reels** alongside YouTube. (on `main`)
2. **Expressive voice** — narration switched from robotic edge-tts to **natural, emotion-aware Gemini 3.8 TTS**, with automatic edge-tts fallback. (on `main`)
3. **Modern language** — narration prompt rewritten to use everyday *spoken* Kannada/Telugu/Hindi (no archaic/Sanskritized words). (on `main`)
4. **Schedule fixed** — cron changed to **09:30 & 18:30 IST** (04:00/13:00 UTC). (on `main`)
5. **Free image generation** — new `free-first` provider chain so daily runs cost ~$0. **This is on the PR branch `free-image-generation`, NOT merged to main.** Still being quality-tested.

---

## 1. Branch state

| Branch | Commits | Status |
|---|---|---|
| `main` | `f7c83b7` (Meta + TTS), `941e56f` (schedule) | ✅ pushed |
| `free-image-generation` | `6a6abb8` (free images) | ✅ pushed, **PR not merged** — under test |

- Scheduled workflow runs from `main` only. Manual `workflow_dispatch` can run from any branch.
- The free-image work is intentionally kept on the PR branch so image quality can be tested
  via a manual run before merging.

---

## 2. Feature: Meta (Facebook + Instagram) Reels publishing  [on `main`]

**New file:** `upload_to_meta.py`

- Publishes each language's video as a **Facebook Reel** and an **Instagram Reel**.
- Uses Meta's **resumable binary upload** via `rupload.facebook.com` — **no public video hosting needed** (works in GitHub Actions). Streams the local `.mp4` bytes.
  - Facebook: 3-phase `video_reels` flow (`start` → upload bytes → `finish` with `video_state=PUBLISHED`).
  - Instagram: resumable `REELS` container → upload bytes → poll `status_code` until `FINISHED` → `media_publish`.
- **Best-effort & non-blocking:** per-platform failure isolation; a FB/IG failure never blocks YouTube uploads or episode advancement.
- **Idempotent** via `job_history.json` → `meta_uploads[<lang>][<platform>]` (won't double-post on re-runs).
- Caption built from title + description + SEO tags as hashtags (`build_caption`).
- Graph API version pinned to `v23.0` (override with `META_GRAPH_VERSION`).

**Integration in `main_pipeline.py`:** a Meta step runs right after the YouTube upload loop (Step 5b). Flags: `--skip-meta` (skip FB/IG), `--skip-upload` (skip both).

**Required secret:** `META_CREDENTIALS_JSON` (GitHub Actions). JSON keyed by language:
```json
{
  "kn": {"fb_page_id": "...", "ig_user_id": "...", "page_access_token": "<PERMANENT_PAGE_TOKEN>"},
  "te": {"fb_page_id": "...", "ig_user_id": "...", "page_access_token": "..."},
  "hi": {"fb_page_id": "...", "ig_user_id": "...", "page_access_token": "..."}
}
```
- If this secret is absent, Meta publishing is **silently skipped** (YouTube unaffected).
- Local testing: drop a `META_CREDENTIALS.json` file in repo root (git-ignored). **This file holds live Page tokens — it is in `.gitignore`, never commit it.**

---

## 3. Feature: Expressive, emotion-aware voice (Gemini TTS)  [on `main`]

**Why:** old edge-tts Indic voices were robotic and had no emotion (Microsoft Indic voices
don't support emotion styles at all).

**What changed in `main_pipeline.py`:**
- New TTS engine: **`gemini-3.8-flash-tts`** via `client.models.generate_content(...)` with
  `response_modalities=["AUDIO"]` + `speech_config` (prebuilt voice). Returns WAV → transcoded to mp3 via ffmpeg.
  - **Works on the installed `google-genai` 1.47 — no SDK upgrade needed.**
- **Per-scene emotion:** Gemini's English blueprint (Call 1) now emits an `emotion` per scene
  (e.g. "tense and fearful"). It's injected into the TTS text as an **inline angle-bracket cue**
  `<tense and fearful voice> ...`. Verified the model **performs** the cue (adjusts delivery)
  **without reading it aloud** (ratio ~1.1× vs 2.6× when a plain prompt prefix is spoken).
- **Automatic fallback to edge-tts** on any error/quota, with emotion→(rate,pitch) shaping.
  Controlled by env **`TTS_ENGINE`** (`gemini` default, or `edge`).
- Per-language Gemini voices (env-overridable): KN=`Charon`, TE=`Rasalgethi`, HI=`Algenib`
  (`KN_GEMINI_VOICE` / `TE_GEMINI_VOICE` / `HI_GEMINI_VOICE`). 30 prebuilt voices available.

**Key functions:** `_gemini_tts_to_mp3`, `_edge_prosody_for_emotion`, `synthesize_scene_audio`
(signature: `synthesize_scene_audio(text, out_mp3, *, lang_key, emotion="")`).

**IMPORTANT gotcha discovered:** Gemini's newer **Interactions API** (`client.interactions.create`
with `speech_metadata.style`) is **NOT usable with an AI Studio key** — it returns
`Model 'gemini-3.8-flash-tts' not found`. Also `system_instruction` is disabled on the TTS model.
So the **only working style mechanism is the inline angle-bracket cue** via `generate_content`.
Do not switch to the Interactions API unless on Vertex AI.

---

## 4. Feature: Modern spoken language  [on `main`]

In `main_pipeline.py` Call 2 (per-language narration), the prompt now forces **modern, everyday
spoken** Kannada/Telugu/Hindi (colloquial / "bolchaal"), explicitly **banning archaic,
Sanskritized, literary, textbook words**. The short-sentence + SEO-metadata design from the
remote was preserved (we merged, not replaced).

---

## 5. Feature: Schedule  [on `main`]

`.github/workflows/generate_and_upload.yml`:
```yaml
schedule:
  - cron: '0 4,13 * * *'   # 04:00 UTC = 09:30 IST, 13:00 UTC = 18:30 IST
```
- **GitHub cron is best-effort** and often delays scheduled runs by minutes–hours (worse at round
  times). The workflow was *already running* twice daily; it just drifted and was set to 9:00/6:00.
- To reduce drift: use an odd minute, e.g. `'7 4,13 * * *'`. For exact timing: trigger via an
  external scheduler hitting the `workflow_dispatch` API.

---

## 6. Feature: Free image generation  [on PR branch `free-image-generation` — NOT merged]

**Why:** Gemini "Nano Banana" images cost ~$1–3/run and dominate cost. This makes images free.

**What changed in `main_pipeline.py`:** image providers refactored into an **ordered chain**
(`PROVIDER_CHAINS`), tried until one succeeds:
| `IMAGE_PROVIDER` | Chain | Cost |
|---|---|---|
| `free-first` (new default) | Pollinations → HF FLUX → Gemini (last resort) | ~$0 most runs |
| `free` (new) | Pollinations → HF FLUX (never paid) | $0 (fails if both free down) |
| `auto` (legacy) | Gemini → Pollinations → HF | paid-first |
| `gemini` / `pollinations` / `hf` | single provider | — |

**Workflow:** defaults to `free-first`; overridable per-run via the `image_provider` input, or
repo-wide via an Actions **variable** named `IMAGE_PROVIDER`.

### ⚠️ Status: BLOCKED on free API keys + quality test
- No `HF_TOKEN` or `POLLINATIONS_API_KEY` secrets were set yet → a test run with `hf` (on old
  `main` code, no fallback) **failed** because the anonymous HF FLUX Space was unavailable.
- **Pollinations may no longer be free for images** (their docs: `402 PAYMENT_REQUIRED` = budget
  exhausted; price depends on model). So **HF FLUX is the more reliably-free option.**
- The Pollinations code uses the **old/undocumented** host `image.pollinations.ai/prompt/...` and a
  stale `model=turbo` alias. Current docs use `gen.pollinations.ai/image/...` with `model=flux`
  (alias for `black-forest-labs/flux.1-schnell`). **If relying on Pollinations, update this code.**
  Auth: `Authorization: Bearer sk_...` or `?key=sk_...` — must be the **secret `sk_`** key
  (a `pk_` key returns 401). Create keys at `enter.pollinations.ai/keys`.

### ▶️ NEXT STEPS to finish the free-image feature
1. **Set `HF_TOKEN`** (free): huggingface.co/settings/tokens → "Read" token (`hf_...`) →
   GitHub repo → Settings → Secrets and variables → Actions → new secret `HF_TOKEN`.
2. (Optional) **Set `POLLINATIONS_API_KEY`** = secret `sk_...` from `enter.pollinations.ai/keys`
   (may require paid credits; expect possible 402).
3. **Test:** Actions → Run workflow → branch `free-image-generation`,
   `image_provider=free`, `skip_upload=true`. Download the **`shorts-preview`** artifact to judge
   image quality. (`skip_upload=true` renders without uploading or changing state.)
4. If quality is acceptable → **open/merge the PR**
   (`https://github.com/Ravikumara666/ai-shorts-pipeline/pull/new/free-image-generation`).
5. If relying on Pollinations → update its code to the `gen.pollinations.ai/image/...` endpoint.

---

## 7. Cost per workflow run (rough, late-2026 pricing)

| Item | Qty | Cost |
|---|---|---|
| 🖼️ Images — Gemini Nano Banana @1K (`auto`) | 8 (shared across langs) | **~$1.07** (≈$1.9 @4K) |
| 🖼️ Images — `free`/`free-first` with free keys | 8 | **~$0** |
| 🎙️ Voice — Gemini 3.8 TTS | 24 clips ≈ 2.8 min | **~$0.04** |
| 📝 Text — story + metadata | 4 calls | **~$0.02** |
| 📤 YouTube + FB + IG posting | all | **$0** |
| ⚙️ GitHub Actions | ~15 min | **$0** (free tier) |

- **Paid-images run ≈ $1.10/run** (~$65/mo at 2/day). **Free-images run ≈ $0.06/run** (~$3.6/mo).
- Images dominate cost; **voice is negligible** (~$0.04). Gemini TTS price doubles 2027-01-01
  (still only ~$0.08/run).
- **Text-to-video was rejected:** far more expensive ($8–20+/run on Veo/Kling) and free options
  unreliable. Keep images + Ken Burns motion.

---

## 8. Full GitHub secrets / variables reference

**Secrets (Settings → Secrets and variables → Actions → Secrets):**
| Name | Required? | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | ✅ yes | Story text, images (Nano Banana), and TTS |
| `YT_TOKEN_KN` / `YT_TOKEN_TE` / `YT_TOKEN_HI` | ✅ yes | base64 of YouTube OAuth token files |
| `META_CREDENTIALS_JSON` | optional | Enables FB + IG Reels (see §2) |
| `HF_TOKEN` | optional | Free HF FLUX images (recommended for free mode) |
| `POLLINATIONS_API_KEY` | optional | Free/paid Pollinations images (`sk_` key) |

**Variables (optional, Actions → Variables):**
| Name | Purpose |
|---|---|
| `IMAGE_PROVIDER` | Repo-wide default image backend (e.g. `free`, `free-first`, `auto`) |

**Key env vars (see `.env.example`):** `TTS_ENGINE`, `GEMINI_TTS_MODEL`, `KN/TE/HI_GEMINI_VOICE`,
`IMAGE_PROVIDER`, `GEMINI_IMAGE_MODEL`, `GEMINI_IMAGE_SIZE`, `NUM_SCENES`, `META_GRAPH_VERSION`.

---

## 9. Testing notes / preferences

- **Do NOT run full local tests that hit paid Gemini APIs (images/TTS) — it burns quota.**
  Prefer committing and testing via GitHub Actions (`workflow_dispatch`, `skip_upload=true` for
  cheap render-only smoke tests).
- `job_history.json` tracks per-episode state: `uploads` (YouTube) + `meta_uploads` (FB/IG).
- Pipeline advancement is gated on YouTube uploads only; Meta is best-effort.

---

## 10. Open items / TODO

- [ ] Set `HF_TOKEN` (and optionally `POLLINATIONS_API_KEY`) secrets.
- [ ] Test free-image quality on the `free-image-generation` branch (§6 next steps).
- [ ] Decide: merge free-image PR to `main` if quality is acceptable.
- [ ] (If keeping Pollinations) update its code to the current `gen.pollinations.ai` endpoint.
- [ ] Confirm `META_CREDENTIALS_JSON` secret is set so FB/IG posting actually fires.
- [ ] (Optional) switch cron to odd minute `'7 4,13 * * *'` if schedule drift is annoying.
