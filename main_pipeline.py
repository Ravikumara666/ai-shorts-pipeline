from __future__ import annotations

import os
import sys
import json
import base64
import asyncio
import hashlib
import subprocess
import shutil
import time
import urllib.parse
import io
import textwrap
import tempfile
from pathlib import Path
from typing import Optional

import httpx
import requests
from PIL import Image, ImageDraw, ImageFont, ImageStat
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from google.genai.errors import APIError, ServerError, ClientError
import edge_tts
from dotenv import load_dotenv

load_dotenv()

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
# Scenes per episode = images generated per run (shared across all languages).
# Lower it for a cheap smoke test, e.g. NUM_SCENES=2.
NUM_SCENES = int(os.getenv("NUM_SCENES", "8"))

# Image backend: "auto" (Gemini -> Pollinations -> HF), "gemini", "pollinations",
# or "hf" (free FLUX.1-schnell Space — use this for a $0 free-tier test run).
IMAGE_PROVIDER = os.getenv("IMAGE_PROVIDER", "auto").lower()


# =====================================================================
# 0. TOOL PATHS — prefer Homebrew on macOS, fallback to PATH
# =====================================================================
def _find_tool(name: str) -> str:
    """Locate ffmpeg / ffprobe, preferring Homebrew install."""
    for candidate in [f"/opt/homebrew/bin/{name}", shutil.which(name) or ""]:
        if candidate and os.path.isfile(candidate):
            return candidate
    result = shutil.which(name)
    if result:
        return result
    sys.exit(f"[FATAL] {name} not found. Install via: brew install ffmpeg")


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")


GEMINI_MODELS = [
    os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.5-flash",
    "gemini-3.8-flash",
]
GEMINI_MODELS = list(dict.fromkeys(GEMINI_MODELS))
GEMINI_TIMEOUT_MS = 120_000

# Google's current image-generation models (Nano Banana family), newest first.
# Imagen (generate_images) was retired in 2026, and image generation moved to
# these Gemini models via the Interactions API. They are listed as an ordered
# fallback chain so a single deprecated/unavailable model ID never kills a run
# (the deprecation-404 trap that previously broke this pipeline).
GEMINI_IMAGE_MODELS = [
    os.getenv("GEMINI_IMAGE_MODEL", "gemini-nano-banana-2.1"),
    "gemini-nano-banana-2.1",
    "gemini-3.1-flash-image",
    "gemini-3.1-flash-lite-image",
    "gemini-3-pro-image",
    "gemini-2.5-flash-image",
]
GEMINI_IMAGE_MODELS = list(dict.fromkeys(GEMINI_IMAGE_MODELS))
# "1K" | "2K" | "4K" — 1K is plenty for the 1080x1920 render target and the
# cheapest per-image tier; bump to "2K"/"4K" only if you want sharper source art.
GEMINI_IMAGE_SIZE = os.getenv("GEMINI_IMAGE_SIZE", "1K")


def _new_gemini_client(api_key: str) -> genai.Client:
    """Create a one-request client: macOS LibreSSL must not reuse stale keep-alive sockets."""
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=GEMINI_TIMEOUT_MS,
            clientArgs={"limits": httpx.Limits(max_connections=1, max_keepalive_connections=0)},
        ),
    )


def call_gemini_with_fallback(api_key: str, contents: str, config: Optional[dict] = None):
    """Generate raw JSON using Gemini across active flash-lite models with automatic retry on 503."""
    last_err: Exception | None = None
    retryable_errors = (
        APIError,
        ServerError,
        ClientError,
        httpx.HTTPError,
        httpx.ReadError,
        httpx.ConnectError,
        httpx.RemoteProtocolError,
        ConnectionResetError,
        TimeoutError,
    )

    base_config: dict = {
        "response_mime_type": "application/json",
        "temperature": 0.7,
    }
    if config:
        base_config.update(config)

    for model_name in GEMINI_MODELS:
        model_config = dict(base_config)
        gen_config = types.GenerateContentConfig(**model_config)

        # Retry up to 3 times per model for temporary 503/timeout spikes
        for model_attempt in range(1, 4):
            client: genai.Client | None = None
            try:
                client = _new_gemini_client(api_key)
                resp = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=gen_config,
                )
                return resp
            except retryable_errors as exc:
                last_err = exc
                err_str = str(exc)
                print(f"[Gemini] {model_name} (attempt {model_attempt}/3) error: {exc}")
                # If model is unavailable (404/NOT_FOUND) or quota exhausted (429), move to next model immediately
                if "NOT_FOUND" in err_str or "404" in err_str or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    break
                # On 503 or transient network error, wait a couple seconds and retry the same model
                time.sleep(2 * model_attempt)
            except Exception as exc:
                last_err = exc
                print(f"[Gemini] {model_name} unexpected error: {exc}")
                break
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

    raise RuntimeError(
        f"Gemini generation failed across all models {GEMINI_MODELS}: {last_err}"
    ) from last_err


# =====================================================================
# 1. STRUCTURED OUTPUT SCHEMA (8 SCENES FOR 65-75s RUNTIME)
# =====================================================================
class LocalizedMetadata(BaseModel):
    title: str = Field(description="YouTube title with Part X and #Shorts, max 100 chars")
    description: str = Field(description="YouTube description with 3-4 hashtags and SEO keywords, max 5000 chars")
    tags: list[str] = Field(description="Exactly 10 high-search regional SEO tags")
    narration_scenes: list[str] = Field(
        description=(
            f"Exactly {NUM_SCENES} voiceover narrations in native script. Each scene MUST contain "
            "2 to 3 full, rich, expressive storytelling sentences (at least 22-28 words per scene) "
            "so each scene voiceover lasts 7 to 9 seconds, ensuring a total video runtime between 60 and 75 seconds."
        )
    )


class MultiLangEpisode(BaseModel):
    episode_summary_for_next_part: str = Field(
        description="2-3 sentence summary of this episode for continuity in the next part"
    )
    visual_prompts_english: list[str] = Field(
        description=(
            f"Exactly {NUM_SCENES} concise English visual prompts (UNDER 220 CHARACTERS EACH). "
            "Format: '3D Pixar Disney animation style, {short_character_desc}, {scene_action_setting}, vibrant cinematic lighting, 9:16 vertical'"
        )
    )
    kannada: LocalizedMetadata
    telugu: LocalizedMetadata
    hindi: LocalizedMetadata


LANGUAGE_CONFIG: dict[str, dict[str, str]] = {
    "kannada": {
        "voice": os.getenv("KN_VOICE", "kn-IN-GaganNeural"),
        "lang_code": "kn",
    },
    "telugu": {
        "voice": os.getenv("TE_VOICE", "te-IN-MohanNeural"),
        "lang_code": "te",
    },
    "hindi": {
        "voice": os.getenv("HI_VOICE", "hi-IN-MadhurNeural"),
        "lang_code": "hi",
    },
}


# =====================================================================
# 2. STORY VAULT & STATE MANAGEMENT
# =====================================================================
STORY_BANK_PATH = Path("story_bank.json")
STORY_STATE_PATH = Path("story_state.json")
JOB_HISTORY_PATH = Path("job_history.json")


def _atomic_json_write(path: Path, data: dict | list) -> None:
    """Write JSON atomically so an interrupted job never leaves a corrupt ledger."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as tmp:
        json.dump(data, tmp, indent=2, ensure_ascii=False)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def load_job_history() -> dict:
    if not JOB_HISTORY_PATH.exists():
        return {}
    with open(JOB_HISTORY_PATH, "r", encoding="utf-8") as f:
        history = json.load(f)
    if not isinstance(history, dict):
        raise ValueError("job_history.json must contain an object keyed by episode job ID.")
    return history


def save_job_history(history: dict) -> None:
    _atomic_json_write(JOB_HISTORY_PATH, history)


def make_job_key(story_id: str, episode: int) -> str:
    return f"{story_id}_ep{episode}"


def stable_prompt_hash(prompts: list[str]) -> str:
    canonical = "\n".join(p.strip() for p in prompts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_story_bank() -> list[dict]:
    if not STORY_BANK_PATH.exists():
        sys.exit("[FATAL] story_bank.json not found.")
    with open(STORY_BANK_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_story_state() -> dict:
    if not STORY_STATE_PATH.exists():
        sys.exit("[FATAL] story_state.json not found.")
    with open(STORY_STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_story_state(state: dict) -> None:
    _atomic_json_write(STORY_STATE_PATH, state)
    print("[State] ✅ story_state.json saved.")


def validate_episode_data(episode: MultiLangEpisode) -> None:
    """Reject malformed LLM output before spending image, render, or upload quota."""
    if len(episode.visual_prompts_english) != NUM_SCENES:
        raise ValueError(f"Expected {NUM_SCENES} visual prompts; got {len(episode.visual_prompts_english)}.")
    for idx, prompt in enumerate(episode.visual_prompts_english, start=1):
        if not prompt.strip() or len(prompt) > 240:
            raise ValueError(f"Visual prompt {idx} is empty or exceeds 240 characters.")

    script_ranges = {
        "kannada": ("\u0c80", "\u0cff"),
        "telugu": ("\u0c00", "\u0c7f"),
        "hindi": ("\u0900", "\u097f"),
    }
    for lang, (start, end) in script_ranges.items():
        meta: LocalizedMetadata = getattr(episode, lang)
        if len(meta.narration_scenes) != NUM_SCENES:
            raise ValueError(f"{lang} must contain exactly {NUM_SCENES} narration scenes.")
        if len(meta.tags) != 10:
            raise ValueError(f"{lang} must contain exactly 10 tags.")
        if len(meta.title) > 100 or len(meta.description) > 5000:
            raise ValueError(f"{lang} metadata exceeds YouTube limits.")
        for idx, scene in enumerate(meta.narration_scenes, start=1):
            native_chars = sum(start <= ch <= end for ch in scene)
            if not scene.strip() or native_chars < 12:
                raise ValueError(f"{lang} scene {idx} does not contain enough native-script text.")


def get_current_story(state: dict, bank: list[dict]) -> dict:
    story_id = state.get("current_story_id")
    for story in bank:
        if story["id"] == story_id:
            return story
    sys.exit(f"[FATAL] Story ID '{story_id}' not found in story_bank.json.")


def advance_to_next_story(state: dict, bank: list[dict]) -> None:
    completed = set(state.get("completed_story_ids", []))
    completed.add(state["current_story_id"])
    state["completed_story_ids"] = list(completed)

    for story in bank:
        if story["id"] not in completed:
            state["current_story_id"] = story["id"]
            state["current_episode"] = 1
            state["previous_summary"] = "None - This is Episode 1. Start the story with an exciting hook."
            print(f"[Vault] ➡️  Next story: '{story['series_title']}' ({story['id']})")
            return

    print("[Vault] ⚠️  All stories in vault completed! Triggering auto-refill...")
    refill_story_vault(bank, count=4)

    refreshed_bank = load_story_bank()
    for story in refreshed_bank:
        if story["id"] not in completed:
            state["current_story_id"] = story["id"]
            state["current_episode"] = 1
            state["previous_summary"] = "None - This is Episode 1. Start the story with an exciting hook."
            print(f"[Vault] ➡️  New story from refill: '{story['series_title']}' ({story['id']})")
            return

    sys.exit("[FATAL] Vault refill produced no new stories.")


# =====================================================================
# 3. AUTO-REFILL STORY VAULT WITH GEMINI
# =====================================================================
class NewStoryEntry(BaseModel):
    id: str = Field(description="Unique story ID like story_007")
    category: str = Field(description="Category like Panchatantra Fable, South Indian Folklore, Tenali Rama Wit")
    region: str = Field(description="Specific Indian region or tradition represented by the story")
    source_note: str = Field(description="Source or clear note that the plot is an original dramatization")
    series_title: str = Field(description="Catchy series title for YouTube")
    character_visual_anchor: str = Field(description="Detailed 3D Pixar-style character description")
    core_plot: str = Field(description="One-paragraph plot summary with moral theme")
    episode_beats: list[str] = Field(description="5 episode beat descriptions")


class StoryVaultRefill(BaseModel):
    new_stories: list[NewStoryEntry] = Field(description="List of new story entries")


def refill_story_vault(existing_bank: list[dict], count: int = 4) -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("[FATAL] GEMINI_API_KEY not set.")

    existing_titles = [s["series_title"] for s in existing_bank]
    existing_ids = [s["id"] for s in existing_bank]
    max_id_num = max(int(sid.split("_")[1]) for sid in existing_ids) if existing_ids else 0

    prompt = f"""You are a master Indian storyteller and children's content creator.
EXISTING STORIES (DO NOT REPEAT):
{json.dumps(existing_titles, indent=2)}

Generate exactly {count} BRAND NEW Indian story arcs (Panchatantra, Tenali Rama, Vikram Betal, regional legends).
Each story needs a character_visual_anchor and exactly 5 episode beats (Part 1 through 5).
Include a specific region and source_note. If a plot is invented or heavily adapted, source_note must say so instead of making a false historical claim.
Story IDs must be: story_{str(max_id_num + 1).zfill(3)}, story_{str(max_id_num + 2).zfill(3)}, etc.

OUTPUT RAW JSON ONLY:
{{
  "new_stories": [
    {{
      "id": "story_{str(max_id_num + 1).zfill(3)}",
      "category": "Tenali Rama Wit",
      "region": "Vijayanagara",
      "source_note": "Folk legend",
      "series_title": "Tenali Rama and the Mystery",
      "character_visual_anchor": "3D Pixar Disney animation style, clever Tenali Rama with dhoti, expressive smirk, vibrant cinematic lighting, 9:16 vertical",
      "core_plot": "Tenali Rama solves a palace mystery using his quick wit.",
      "episode_beats": [
        "Part 1: The mystery begins.",
        "Part 2: Clues emerge.",
        "Part 3: A false suspect.",
        "Part 4: The clever trap.",
        "Part 5: The truth revealed."
      ]
    }}
  ]
}}"""

    print(f"[Vault Refill] Generating {count} new stories with Gemini...")
    response = call_gemini_with_fallback(api_key, contents=prompt)
    clean_json = response.text.strip().removeprefix("```json").removesuffix("```").strip()
    refill = StoryVaultRefill.model_validate_json(clean_json)
    new_entries = [entry.model_dump() for entry in refill.new_stories]
    updated_bank = existing_bank + new_entries
    _atomic_json_write(STORY_BANK_PATH, updated_bank)
    print(f"[Vault Refill] ✅ Added {len(new_entries)} new stories to vault.")


# =====================================================================
# 4. GENERATE 65-75s MULTI-LANGUAGE EPISODE WITH GEMINI (2 FAST CALLS)
# =====================================================================
def generate_episode_data(state: dict, story: dict) -> MultiLangEpisode:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("[FATAL] GEMINI_API_KEY not set. Add it to .env or environment.")

    ep_num = state["current_episode"]
    max_ep = state["max_episodes_in_arc"]
    is_finale = ep_num == max_ep

    beat_index = ep_num - 1
    current_beat = story["episode_beats"][beat_index] if beat_index < len(story["episode_beats"]) else ""
    character_anchor = story["character_visual_anchor"]

    finale_instruction = (
        "End Scene 8 by resolving the story with a heartwarming moral lesson."
        if is_finale
        else f"End Scene 8 with an intense cliffhanger asking viewers to subscribe for Part {ep_num + 1}!"
    )

    # -------------------------------------------------------------
    # CALL 1: English Director Blueprint (~4s)
    # -------------------------------------------------------------
    print(f"[Gemini] Call 1/2: Generating English Director Blueprint ({story['series_title']} Part {ep_num})...")
    call1_prompt = f"""You are an elite Pixar/Disney animation director creating an Indian folklore animated Short.

STORY DETAILS:
- Series: {story['series_title']}
- Category: {story['category']}
- Region: {story.get('region', 'Indian folklore')}
- Core Plot: {story['core_plot']}
- Episode: Part {ep_num} of {max_ep}
- Beat to Follow: {current_beat}
- Previous Events: {state['previous_summary']}

CHARACTER ANCHOR:
{character_anchor}

TASK:
1. Brainstorm 3 candidate viral hooks for Scene 1 and select the strongest winning hook.
2. Direct exactly {NUM_SCENES} progressive visual scenes in English.
   Each scene must have:
   - `scene_number`: 1 to {NUM_SCENES}
   - `story_action_visual_prompt`: Under 220 characters: "3D Pixar Disney animation style, {character_anchor[:60]}, <action/setting>, vibrant cinematic lighting, 9:16 vertical"
   - `english_narration`: 2 full, dramatic spoken sentences (at least 20-25 words) advancing the mystery/drama.
   - `camera_movement_type`: One of ["push-in", "pull-out", "pan-right", "pan-left", "zoom-in"]
3. {finale_instruction}
4. Provide a 2-sentence `episode_summary_for_next_part`.

OUTPUT RAW JSON ONLY (No markdown codeblocks):
{{
  "winning_hook": "...",
  "episode_summary_for_next_part": "...",
  "scenes": [
    {{
      "scene_number": 1,
      "story_action_visual_prompt": "3D Pixar Disney animation style, {character_anchor[:55]}, staring into mysterious glowing well, vibrant cinematic lighting, 9:16 vertical",
      "english_narration": "Deep in the ruins of Hampi, young Putta heard an eerie whisper echoing from the well. The midnight waters began to glow with ancient gold.",
      "camera_movement_type": "push-in"
    }}
  ]
}}"""

    resp1 = call_gemini_with_fallback(api_key, call1_prompt)
    raw1 = resp1.text.strip().removeprefix("```json").removesuffix("```").strip()
    try:
        call1_data = json.loads(raw1)
    except Exception as e:
        print(f"[Gemini] Error parsing Call 1 JSON: {e}\nRaw: {raw1[:300]}")
        raise

    scenes = call1_data.get("scenes", [])
    if len(scenes) != NUM_SCENES:
        raise ValueError(f"Call 1 must return exactly {NUM_SCENES} scenes, got {len(scenes)}")

    visual_prompts_english = [
        s.get("story_action_visual_prompt", s.get("visual_prompt", ""))
        for s in scenes
    ]

    # -------------------------------------------------------------
    # CALL 2: Per-Language Regional Storytelling & YouTube Metadata (~2s each)
    # -------------------------------------------------------------
    scenes_text = "\n".join(
        f"Scene {s['scene_number']}: {s.get('english_narration', '')}" for s in scenes
    )

    lang_specs = {
        "kannada": {
            "name": "Kannada",
            "script": "Kannada script ಕನ್ನಡ",
            "example_title": f"{story['series_title']} Part {ep_num} | #Shorts",
            "example_desc": f"ಹಂಪಿಯ ನಿಗೂಢ ಕಥೆ Part {ep_num} #kannada #shorts #folklore",
            "example_tags": ["ಕನ್ನಡ", "ಹಂಪಿ", "ವಿಜಯನಗರ", "ಕಥೆಗಳು", "shorts", "kannada stories", "mystery", "viral", "trending", "folklore"],
        },
        "telugu": {
            "name": "Telugu",
            "script": "Telugu script తెలుగు",
            "example_title": f"{story['series_title']} Part {ep_num} | #Shorts",
            "example_desc": f"హంపీ రహస్య కథ Part {ep_num} #telugu #shorts #folklore",
            "example_tags": ["తెలుగు", "హంపీ", "కథలు", "shorts", "telugu stories", "mystery", "viral", "trending", "folklore", "india"],
        },
        "hindi": {
            "name": "Hindi",
            "script": "Devanagari script हिन्दी",
            "example_title": f"{story['series_title']} Part {ep_num} | #Shorts",
            "example_desc": f"हम्पी की रहस्यमयी कहानी Part {ep_num} #hindi #shorts #kahani",
            "example_tags": ["हिन्दी", "हम्पी", "कहानियां", "shorts", "hindi stories", "mystery", "viral", "trending", "folklore", "kahani"],
        },
    }

    localized_results: dict[str, LocalizedMetadata] = {}

    for lang_key, spec in lang_specs.items():
        print(f"[Gemini] Generating {spec['name']} narration and metadata...")
        call_prompt = f"""You are a master {spec['name']} storyteller and YouTube Shorts creator.

STORY: {story['series_title']} - Part {ep_num} of {max_ep}
ENGLISH SCENE NARRATIONS:
{scenes_text}

TASK:
1. Translate and dramatically adapt each of the {NUM_SCENES} scenes into native {spec['name']} ({spec['script']}).
   CRITICAL: EACH scene MUST have 2 full dramatic spoken sentences (at least 22-28 native words) for a 7-9 second voiceover per scene.
2. Provide a localized YouTube title (with Part {ep_num} and #Shorts), description (with 3-4 hashtags), and exactly 10 high-search regional tags.

OUTPUT RAW JSON ONLY (No markdown codeblocks):
{{
  "title": "{spec['example_title']}",
  "description": "{spec['example_desc']}",
  "tags": {json.dumps(spec['example_tags'], ensure_ascii=False)},
  "narration_scenes": [
    "Scene 1 {spec['name']} narration...",
    "Scene 2 {spec['name']} narration..."
  ]
}}"""

        resp = call_gemini_with_fallback(api_key, call_prompt)
        raw_json = resp.text.strip().removeprefix("```json").removesuffix("```").strip()
        try:
            data = json.loads(raw_json)
            meta = LocalizedMetadata.model_validate(data)
            localized_results[lang_key] = meta
        except Exception as e:
            print(f"[Gemini] Error parsing {spec['name']} JSON: {e}\nRaw: {raw_json[:200]}")
            raise

    episode = MultiLangEpisode(
        episode_summary_for_next_part=call1_data.get("episode_summary_for_next_part", ""),
        visual_prompts_english=visual_prompts_english,
        kannada=localized_results["kannada"],
        telugu=localized_results["telugu"],
        hindi=localized_results["hindi"],
    )
    validate_episode_data(episode)
    print("[Gemini] ✅ Multi-language episode data generated and validated successfully!")
    return episode


# =====================================================================
# 5. DOWNLOAD REAL 3D PIXAR IMAGES (NO SOLID COLOR FALLBACK!)
# =====================================================================
def _validate_image_content(content: bytes) -> bool:
    """Validate image bytes and reject undersized, corrupt, or near-solid frames."""
    if len(content) < 30_000:
        return False
    try:
        with Image.open(io.BytesIO(content)) as source:
            img = source.convert("RGB")
            img.load()
        return max(ImageStat.Stat(img).stddev) > 25
    except Exception:
        return False


def _find_generated_file(value: object) -> Optional[Path]:
    """Extract a local image path from the few response shapes Gradio Spaces use."""
    if isinstance(value, str):
        path = Path(value)
        return path if path.is_file() else None
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _find_generated_file(item)
            if found:
                return found
    if isinstance(value, dict):
        for item in value.values():
            found = _find_generated_file(item)
            if found:
                return found
    return None


def _download_via_hf_flux(prompt: str, img_path: str, seed: int) -> bool:
    """Best-effort free fallback through the FLUX.1-schnell Hugging Face Space."""
    try:
        from gradio_client import Client

        print("[Visuals] 🎨 Pollinations unavailable; trying Hugging Face FLUX.1-schnell...")
        token = os.getenv("HF_TOKEN", "").strip() or None
        # gradio_client >=1.x renamed the auth kwarg from `hf_token` to `token`.
        client = Client("black-forest-labs/FLUX.1-schnell", token=token)
        result = client.predict(
            prompt=prompt,
            seed=seed,
            randomize_seed=False,
            width=768,
            height=1365,
            num_inference_steps=4,
            api_name="/infer",
        )
        generated_path = _find_generated_file(result)
        if generated_path:
            image_bytes = generated_path.read_bytes()
            if _validate_image_content(image_bytes):
                Path(img_path).write_bytes(image_bytes)
                print(f"[Visuals] ✅ Scene saved via Hugging Face FLUX ({len(image_bytes) // 1024} KB)")
                return True
    except Exception as e:
        print(f"[Visuals] ⚠️  Hugging Face FLUX fallback failed: {e}")
    return False


def _coerce_image_bytes(data: object) -> Optional[bytes]:
    """Return raw image bytes whether the SDK handed us bytes or a base64 string."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        try:
            return base64.b64decode(data)
        except Exception:
            return None
    return None


def _extract_interaction_image(interaction: object) -> Optional[bytes]:
    """Pull image bytes out of an Interactions API response (direct or interleaved)."""
    out = getattr(interaction, "output_image", None)
    if out is not None:
        found = _coerce_image_bytes(getattr(out, "data", None))
        if found:
            return found
    for step in getattr(interaction, "steps", None) or []:
        for block in getattr(step, "content", None) or []:
            if getattr(block, "type", None) == "image":
                found = _coerce_image_bytes(getattr(block, "data", None))
                if found:
                    return found
    return None


def _extract_generate_content_image(resp: object) -> Optional[bytes]:
    """Pull inline image bytes out of a generate_content IMAGE-modality response."""
    for cand in getattr(resp, "candidates", None) or []:
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            if inline is not None:
                found = _coerce_image_bytes(getattr(inline, "data", None))
                if found:
                    return found
    return None


def _normalize_to_vertical(image_bytes: bytes, img_path: str) -> bool:
    """Cover-crop/resize arbitrary image bytes to the exact 1080x1920 JPG the renderer expects."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as src:
            img = src.convert("RGB")
            img.load()
    except Exception as e:
        print(f"[Visuals] ⚠️  Could not decode generated image: {e}")
        return False

    target_w, target_h = 1080, 1920
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        return False
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    img = img.crop((left, top, left + target_w, top + target_h))
    img.save(img_path, format="JPEG", quality=90)

    with open(img_path, "rb") as f:
        if _validate_image_content(f.read()):
            return True
    Path(img_path).unlink(missing_ok=True)
    return False


def _download_via_gemini_image(prompt: str, img_path: str, seed: int) -> bool:
    """Primary image source: Google Nano Banana image models via the same GEMINI_API_KEY.

    Tries the new Interactions API first (Google's recommended image path), then
    falls back to generate_content IMAGE modality, walking the model list so a
    single deprecated/unavailable model ID never aborts the episode.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("[Visuals] ⚠️  GEMINI_API_KEY not set; skipping Gemini image generation.")
        return False

    skip_codes = ("NOT_FOUND", "404", "429", "RESOURCE_EXHAUSTED", "PERMISSION_DENIED")
    interactions_supported = True

    print("[Visuals] 🎨 Generating scene via Google Nano Banana image model...")
    for model_name in GEMINI_IMAGE_MODELS:
        client: genai.Client | None = None
        try:
            client = _new_gemini_client(api_key)
            image_bytes: Optional[bytes] = None

            # --- Primary: Interactions API ---
            if interactions_supported and hasattr(client, "interactions"):
                try:
                    interaction = client.interactions.create(
                        model=model_name,
                        input=prompt,
                        response_format={
                            "type": "image",
                            "mime_type": "image/jpeg",
                            "aspect_ratio": "9:16",
                            "image_size": GEMINI_IMAGE_SIZE,
                        },
                    )
                    image_bytes = _extract_interaction_image(interaction)
                except (APIError, ServerError, ClientError) as exc:
                    print(f"[Visuals] [Gemini-img] {model_name} interactions error: {exc}")
                    if any(code in str(exc) for code in skip_codes):
                        continue  # model unavailable/quota -> next model
                except Exception as exc:
                    print(f"[Visuals] [Gemini-img] interactions API unavailable: {exc}")
            else:
                interactions_supported = False

            # --- Fallback: generate_content IMAGE modality ---
            if image_bytes is None:
                try:
                    resp = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_modalities=["TEXT", "IMAGE"],
                        ),
                    )
                    image_bytes = _extract_generate_content_image(resp)
                except (APIError, ServerError, ClientError) as exc:
                    print(f"[Visuals] [Gemini-img] {model_name} generate_content error: {exc}")
                    if any(code in str(exc) for code in skip_codes):
                        continue
                except Exception as exc:
                    print(f"[Visuals] [Gemini-img] {model_name} generate_content failed: {exc}")

            if image_bytes and _normalize_to_vertical(image_bytes, img_path):
                print(f"[Visuals] ✅ Scene saved via Gemini image model '{model_name}'.")
                return True
        except Exception as exc:
            print(f"[Visuals] [Gemini-img] {model_name} unexpected error: {exc}")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    print("[Visuals] ⚠️  All Gemini image models failed for this scene.")
    return False


def generate_shared_images(visual_prompts: list[str], output_dir: str) -> list[str]:
    """Generate prompt-hashed episode images via Gemini (Nano Banana), then free fallbacks."""
    os.makedirs(output_dir, exist_ok=True)
    image_paths: list[str] = []
    pollinations_key = os.getenv("POLLINATIONS_API_KEY", "").strip()

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    if pollinations_key:
        headers["Authorization"] = f"Bearer {pollinations_key}"

    for idx, prompt in enumerate(visual_prompts):
        prompt_hash = hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()
        img_path = os.path.join(output_dir, f"scene_{idx}_{prompt_hash[:8]}.jpg")

        # Skip if already downloaded and verified
        if os.path.isfile(img_path):
            with open(img_path, "rb") as f:
                if _validate_image_content(f.read()):
                    print(f"[Visuals] Scene {idx + 1}/{len(visual_prompts)} verified cached image exists, skipping.")
                    image_paths.append(img_path)
                    continue
            Path(img_path).unlink(missing_ok=True)

        # Keep prompt concise to prevent HTTP 414 / Cloudflare rejection
        clean_prompt = prompt.replace("\n", " ").strip()
        if len(clean_prompt) > 240:
            clean_prompt = clean_prompt[:235] + ", 9:16 vertical"
        encoded = urllib.parse.quote(clean_prompt)

        # Pollinations Flux first, then Turbo. No paid/obsolete provider fallback.
        key_query = f"&key={pollinations_key}" if pollinations_key else ""
        endpoints = [
            f"https://image.pollinations.ai/prompt/{encoded}?width=1080&height=1920&seed={100 + idx}&nologo=true&model=flux{key_query}",
            f"https://image.pollinations.ai/prompt/{encoded}?width=1080&height=1920&seed={100 + idx}&nologo=true&model=turbo{key_query}",
        ]

        print(f"[Visuals] Generating Scene {idx + 1}/{len(visual_prompts)} (provider={IMAGE_PROVIDER})...")

        saved = False

        # Primary: Google Nano Banana image models (same GEMINI_API_KEY).
        if IMAGE_PROVIDER in ("auto", "gemini"):
            saved = _download_via_gemini_image(clean_prompt, img_path, seed=100 + idx)

        # Secondary: Pollinations — only if an API key is configured.
        # The free unauthenticated endpoint now returns 402 Payment Required,
        # so skip it entirely without a key instead of burning retries on 402.
        if not saved and IMAGE_PROVIDER in ("auto", "pollinations") and pollinations_key:
            for attempt in range(1, 4):
                endpoint_url = endpoints[(attempt - 1) % len(endpoints)]
                try:
                    print(f"[Visuals] Pollinations fallback Scene {idx + 1} (attempt {attempt}/3)...")
                    resp = requests.get(endpoint_url, headers=headers, timeout=90)
                    resp.raise_for_status()
                    if _validate_image_content(resp.content):
                        with open(img_path, "wb") as f:
                            f.write(resp.content)
                        print(f"[Visuals] ✅ Scene {idx + 1} saved ({len(resp.content) // 1024} KB via Pollinations)")
                        saved = True
                        break
                    else:
                        print(f"[Visuals] ⚠️  Attempt {attempt} returned invalid/undersized image ({len(resp.content)} bytes).")
                except Exception as e:
                    wait = 5 * attempt
                    print(f"[Visuals] ⚠️  Attempt {attempt} failed: {e}. Retrying in {wait}s...")
                    time.sleep(wait)

        # Tertiary (or primary when IMAGE_PROVIDER=hf): free Hugging Face FLUX.1-schnell Space.
        if not saved and IMAGE_PROVIDER in ("auto", "hf"):
            saved = _download_via_hf_flux(clean_prompt, img_path, seed=100 + idx)

        if not saved:
            sys.exit(
                f"[FATAL] Failed to generate image for Scene {idx + 1} across all providers. "
                "Primary is Google Nano Banana (needs a valid GEMINI_API_KEY with image generation enabled); "
                "optional fallbacks are POLLINATIONS_API_KEY and the HF FLUX Space (HF_TOKEN). "
                "Never falling back to solid color."
            )

        image_paths.append(img_path)

    return image_paths


# =====================================================================
# 6. PILLOW-BASED 100% VISIBLE SUBTITLE OVERLAY GENERATOR
# =====================================================================
def _get_indic_font(lang: str, size: int = 46) -> ImageFont.FreeTypeFont:
    """Locate appropriate Indic font on macOS or Linux."""
    paths = {
        "kannada": [
            "/System/Library/Fonts/NotoSansKannada.ttc",
            "/System/Library/Fonts/Supplemental/Kannada Sangam MN.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansKannada-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansKannada-Regular.ttf",
        ],
        "telugu": [
            "/System/Library/Fonts/KohinoorTelugu.ttc",
            "/System/Library/Fonts/Supplemental/Telugu MN.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansTelugu-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansTelugu-Regular.ttf",
        ],
        "hindi": [
            "/System/Library/Fonts/Kohinoor.ttc",
            "/System/Library/Fonts/Supplemental/Devanagari Sangam MN.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        ],
    }
    for p in paths.get(lang, []):
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def create_subtitle_overlay_png(text: str, lang: str, out_png: str) -> str:
    """Generate a readable phrase subtitle card without dropping any words."""
    overlay = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    words = text.split()
    wrapped = text
    font = _get_indic_font(lang, size=54)
    for size in range(54, 27, -2):
        font = _get_indic_font(lang, size=size)
        lines: list[str] = []
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            if draw.textlength(trial, font=font) <= 920:
                current = trial
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        wrapped = "\n".join(lines)
        if len(lines) <= 3:
            break

    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=14, align="center")
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x = (1080 - w) // 2
    # Position in lower-middle safe zone (above YouTube Shorts description/buttons)
    y = 1920 - 360 - h

    # Semi-transparent dark pill background
    pad_x, pad_y = 32, 20
    draw.rounded_rectangle(
        [x - pad_x, y - pad_y, x + w + pad_x, y + h + pad_y],
        radius=18,
        fill=(0, 0, 0, 175),
    )

    # Bold yellow text (#FFE600) with thick black outline
    draw.multiline_text(
        (x, y),
        wrapped,
        font=font,
        fill="#FFE600",
        stroke_width=4,
        stroke_fill="black",
        spacing=14,
        align="center",
    )

    overlay.save(out_png, "PNG")
    return out_png


def subtitle_phrases(text: str, duration: float, max_words: int = 7) -> list[tuple[str, float, float]]:
    """Split narration into short, sequential cards spanning the measured TTS duration."""
    words = text.split()
    if not words:
        return []
    chunks = [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]
    total_words = max(1, len(words))
    current = 0.0
    timing: list[tuple[str, float, float]] = []
    for chunk in chunks:
        chunk_duration = duration * (len(chunk.split()) / total_words)
        end = min(duration, current + chunk_duration)
        timing.append((chunk, current, end))
        current = end
    phrase, start, _ = timing[-1]
    timing[-1] = (phrase, start, duration)
    return timing


# =====================================================================
# 7. REGIONAL AUDIO SYNTHESIS & BACKGROUND MUSIC
# =====================================================================
async def synthesize_scene_audio(text: str, voice: str, out_mp3: str) -> float:
    """Synthesize narration with edge-tts at natural dramatic pace (-5%)."""
    communicate = edge_tts.Communicate(text, voice, rate="-5%")
    await communicate.save(out_mp3)

    cmd = [
        FFPROBE,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        out_mp3,
    ]
    duration = float(subprocess.check_output(cmd).decode().strip())
    print(f"    Audio: {os.path.basename(out_mp3)} -> {duration:.2f}s (dramatic pace)")
    return duration


def ensure_bgm_track() -> str:
    """Ensure assets/bgm.mp3 exists; fallback generates soft ambient loop."""
    bgm_dir = Path("assets")
    bgm_dir.mkdir(parents=True, exist_ok=True)
    bgm_file = bgm_dir / "bgm.mp3"

    if bgm_file.exists() and bgm_file.stat().st_size > 10_000:
        return str(bgm_file.resolve())

    print("[BGM] Generating default ambient mystery loop via FFmpeg...")
    cmd = [
        FFMPEG, "-y",
        "-f", "lavfi",
        "-i", (
            "aevalsrc=sin(2*PI*146.83*t)*0.04*sin(2*PI*0.1*t)"
            "+sin(2*PI*220*t)*0.03*cos(2*PI*0.15*t)"
            "+sin(2*PI*293.66*t)*0.025*sin(2*PI*0.2*t)"
            "+sin(2*PI*440*t)*0.015*cos(2*PI*0.08*t):d=90:s=44100"
        ),
        "-c:a", "libmp3lame", "-b:a", "128k",
        str(bgm_file),
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    print(f"[BGM] ✅ Default BGM track ready: {bgm_file}")
    return str(bgm_file.resolve())


# =====================================================================
# 8. RENDER DYNAMIC ANIMATED VIDEO WITH OVERLAY SUBTITLES
# =====================================================================
async def build_language_video(
    lang_key: str,
    meta: LocalizedMetadata,
    image_paths: list[str],
    work_dir: str,
    bgm_path: Optional[str] = None,
) -> str:
    """Render a 60-75s Short with dynamic camera animations, burned-in subtitles, and BGM."""
    voice = LANGUAGE_CONFIG[lang_key]["voice"]
    lang_dir = os.path.join(work_dir, lang_key)
    os.makedirs(lang_dir, exist_ok=True)

    print(f"\n[{lang_key.upper()}] Building Short with voice {voice} ({len(meta.narration_scenes)} scenes)...")

    segment_files: list[str] = []
    total_running_time = 0.0

    if len(meta.narration_scenes) != len(image_paths):
        raise ValueError(f"{lang_key} narration/image scene counts do not match.")

    for i, (scene_text, img_path) in enumerate(zip(meta.narration_scenes, image_paths)):
        audio_path = os.path.join(lang_dir, f"audio_{i}.mp3")
        seg_path = os.path.join(lang_dir, f"seg_{i}.mp4")

        # 1. Synthesize audio
        duration = await synthesize_scene_audio(scene_text, voice, audio_path)

        # 2. Add 0.4s natural breath pause between scenes
        total_seg_duration = duration + 0.40
        total_running_time += total_seg_duration
        frames = int(total_seg_duration * 30) + 2

        # 3. Create phrase-timed subtitle overlays that cover the complete narration.
        phrase_timing = subtitle_phrases(scene_text, duration)
        overlay_paths: list[str] = []
        for phrase_idx, (phrase, _start, _end) in enumerate(phrase_timing):
            overlay_path = os.path.join(lang_dir, f"sub_{i}_{phrase_idx}.png")
            create_subtitle_overlay_png(phrase, lang_key, overlay_path)
            overlay_paths.append(overlay_path)

        # 4. Alternating dynamic camera movement (smooth push-in vs pull-out)
        if i % 2 == 0:
            # Even scenes: Smooth push-in zoom + horizontal motion
            cam_expr = (
                f"zoompan=z='min(zoom+0.002,1.35)':"
                f"x='iw/2-(iw/zoom/2)+sin(on/25)*40':"
                f"y='ih/2-(ih/zoom/2)':"
                f"d={frames}:s=1080x1920:fps=30"
            )
        else:
            # Odd scenes: Smooth pull-out zoom + vertical motion
            cam_expr = (
                f"zoompan=z='if(eq(on,1),1.35,max(1.0,zoom-0.002))':"
                f"x='iw/2-(iw/zoom/2)':"
                f"y='ih/2-(ih/zoom/2)+cos(on/25)*40':"
                f"d={frames}:s=1080x1920:fps=30"
            )

        fade_out_st = max(0.1, total_seg_duration - 0.3)
        filters = [f"[0:v]scale=1080:1920,{cam_expr}[bg]"]
        current_label = "bg"
        for phrase_idx, (_phrase, start, end) in enumerate(phrase_timing):
            next_label = f"caption_{i}_{phrase_idx}"
            filters.append(
                f"[{current_label}][{phrase_idx + 1}:v]overlay=0:0:"
                f"enable='between(t,{start:.3f},{end:.3f})'[{next_label}]"
            )
            current_label = next_label
        filters.append(
            f"[{current_label}]fade=t=in:st=0:d=0.3,fade=t=out:st={fade_out_st:.2f}:d=0.3,"
            "format=yuv420p[vout]"
        )
        vf_filter = ";".join(filters)

        ffmpeg_cmd = [
            FFMPEG, "-y",
            "-loop", "1", "-i", os.path.abspath(img_path),
        ]
        for overlay_path in overlay_paths:
            ffmpeg_cmd.extend(["-loop", "1", "-i", os.path.abspath(overlay_path)])
        ffmpeg_cmd.extend([
            "-i", os.path.abspath(audio_path),
            "-filter_complex", vf_filter,
            "-map", "[vout]",
            "-map", f"{len(overlay_paths) + 1}:a",
            "-t", f"{total_seg_duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast",
            "-af", "apad=pad_dur=0.40",
            "-c:a", "aac", "-b:a", "128k",
            "-ar", "44100", "-ac", "2",
            os.path.abspath(seg_path),
        ])
        subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        segment_files.append(seg_path)
        print(f"    ✅ Scene {i + 1}/{len(meta.narration_scenes)} rendered ({total_seg_duration:.2f}s, sub overlay applied)")

    # 5. Concatenate all segments
    concat_list = os.path.join(lang_dir, "concat.txt")
    with open(concat_list, "w") as f:
        for seg in segment_files:
            f.write(f"file '{os.path.abspath(seg)}'\n")

    concat_mp4 = os.path.join(lang_dir, "concat_raw.mp4")
    subprocess.run(
        [
            FFMPEG, "-y", "-f", "concat", "-safe", "0",
            "-i", concat_list, "-c", "copy", concat_mp4,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )

    # 6. Mix BGM at 10% volume
    final_mp4 = os.path.join(work_dir, f"final_{lang_key}.mp4")
    if bgm_path and os.path.isfile(bgm_path):
        subprocess.run(
            [
                FFMPEG, "-y",
                "-i", concat_mp4,
                "-stream_loop", "-1",
                "-i", bgm_path,
                "-filter_complex", (
                    "[1:a]volume=0.10[bgm];"
                    "[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]"
                ),
                "-map", "0:v",
                "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k",
                "-ar", "44100", "-ac", "2",
                "-shortest", final_mp4,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        print(f"[{lang_key.upper()}] 🎵 Mixed BGM at 10% volume.")
    else:
        shutil.copyfile(concat_mp4, final_mp4)

    # 7. Hard verification before an episode can enter the upload ledger.
    dur_cmd = [
        FFPROBE, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", final_mp4,
    ]
    final_duration = float(subprocess.check_output(dur_cmd).decode().strip())
    print(f"[{lang_key.upper()}] ⏱️  Final Short Duration: {final_duration:.2f} seconds")

    # Production Shorts (8 scenes) must land in 60-75s. A reduced scene count
    # (cheap smoke test) or an explicit override downgrades this to a warning so
    # the render still completes for inspection.
    relaxed = (
        NUM_SCENES < 8
        or os.getenv("RELAX_DURATION_CHECK", "").strip().lower() in ("1", "true", "yes")
    )
    if not 60.0 <= final_duration <= 75.0:
        msg = f"{lang_key} render is {final_duration:.2f}s; expected 60.0-75.0s."
        if not relaxed:
            raise ValueError(msg + " Refusing to upload an out-of-range Short.")
        print(f"[{lang_key.upper()}] ⚠️  {msg} Allowing anyway (relaxed: test / non-standard scene count).")
    else:
        print(f"[{lang_key.upper()}] 🎯 ✅ DURATION VERIFIED: {final_duration:.2f}s")

    print(f"[{lang_key.upper()}] ✅ Final Short -> {final_mp4}")
    return final_mp4


# =====================================================================
# 9. MAIN ORCHESTRATOR
# =====================================================================
async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Multi-Language YouTube Shorts Pipeline")
    parser.add_argument("--skip-upload", action="store_true", help="Render videos only; do not upload to YouTube")
    parser.add_argument("--dry-run", action="store_true", help="Run without persisting updated story state or vault")
    parser.add_argument("--episode", type=int, default=None, help="Force a specific episode number")
    parser.add_argument(
        "--advance-on-render", action="store_true",
        help="Offline-only: advance state after a successful render without YouTube uploads.",
    )
    args = parser.parse_args()

    if args.advance_on_render and not args.skip_upload:
        parser.error("--advance-on-render requires --skip-upload.")

    state = load_story_state()
    bank = load_story_bank()
    story = get_current_story(state, bank)

    if args.episode is not None:
        state["current_episode"] = args.episode

    ep_num = state["current_episode"]
    max_ep = state["max_episodes_in_arc"]
    job_key = make_job_key(story["id"], ep_num)
    work_dir = os.path.join("output_build", job_key)

    print("=" * 60)
    print(f"🎬 [{story['category']}] {story['series_title']}")
    print(f"   Episode {ep_num} of {max_ep} (Target Duration: 65-75 seconds)")
    print(f"   Character: {story['character_visual_anchor'][:80]}...")
    if args.skip_upload:
        print("   Upload: SKIPPED (--skip-upload)")
    if args.dry_run:
        print("   State Persistence: DISABLED (--dry-run)")
    print("=" * 60)

    # Step 1: Generate 8-scene 65-75s content with Gemini
    episode_data = generate_episode_data(state, story)

    # Step 2: Generate only inside this episode's immutable namespace.
    images = generate_shared_images(
        episode_data.visual_prompts_english,
        os.path.join(work_dir, "shared_images"),
    )

    # Step 3: Ensure background music track
    bgm_path = ensure_bgm_track()

    # Step 4: Build animated Shorts for each language (with Ken Burns, Pillow Subtitles, BGM)
    lang_map: dict[str, LocalizedMetadata] = {
        "kannada": episode_data.kannada,
        "telugu":  episode_data.telugu,
        "hindi":   episode_data.hindi,
    }

    video_paths: dict[str, str] = {}
    for lang_key, meta in lang_map.items():
        video_path = await build_language_video(
            lang_key, meta, images, work_dir, bgm_path=bgm_path
        )
        video_paths[lang_key] = video_path

        meta_path = os.path.join(work_dir, f"meta_{lang_key}.json")
        with open(meta_path, "w", encoding="utf-8") as mf:
            mf.write(meta.model_dump_json(indent=2))

    manifest = {
        "job_key": job_key,
        "story_id": story["id"],
        "episode": ep_num,
        "prompt_hash": stable_prompt_hash(episode_data.visual_prompts_english),
        "visual_prompts": episode_data.visual_prompts_english,
        "image_paths": images,
        "video_paths": video_paths,
        "metadata": {lang: meta.model_dump() for lang, meta in lang_map.items()},
    }
    _atomic_json_write(Path(work_dir) / "episode_manifest.json", manifest)

    history = load_job_history()
    job = history.setdefault(job_key, {})
    job.update({
        "story_id": story["id"],
        "episode": ep_num,
        "prompt_hash": manifest["prompt_hash"],
        "status": "rendered",
        "manifest_path": str(Path(work_dir) / "episode_manifest.json"),
    })
    uploads = job.setdefault("uploads", {})
    for lang_code in ("kn", "te", "hi"):
        uploads.setdefault(lang_code, {"status": "pending", "video_id": None})
    save_job_history(history)
    print(f"[Ledger] ✅ {job_key} recorded as rendered.")

    # Step 5: Upload to YouTube (if not skipped)
    if not args.skip_upload:
        from upload_to_youtube import upload_short

        for lang_key, vpath in video_paths.items():
            lang_code = LANGUAGE_CONFIG[lang_key]["lang_code"]
            meta = lang_map[lang_key]
            print(f"\n[Upload] Uploading {lang_key.upper()} Short to YouTube...")
            upload_short(
                video_file=vpath,
                title=meta.title,
                description=meta.description,
                tags=meta.tags,
                lang_code=lang_code,
                job_key=job_key,
                category_id="24",
            )
    else:
        print("\n[Upload] ⏭️  Skipped upload as requested (--skip-upload).")

    # Step 6: Advance only after the durable ledger confirms every channel upload.
    history = load_job_history()
    completed_uploads = all(
        history.get(job_key, {}).get("uploads", {}).get(code, {}).get("status") == "uploaded"
        and history[job_key]["uploads"][code].get("video_id")
        for code in ("kn", "te", "hi")
    )
    can_advance = completed_uploads or args.advance_on_render
    if completed_uploads:
        history[job_key]["status"] = "completed"
        save_job_history(history)

    if not args.dry_run and can_advance:
        if ep_num >= max_ep:
            print(f"\n🏁 Arc complete: '{story['series_title']}'")
            advance_to_next_story(state, bank)
        else:
            state["current_episode"] = ep_num + 1
            state["previous_summary"] = episode_data.episode_summary_for_next_part
            print(f"\n➡️  Advanced to Episode {state['current_episode']} of '{story['series_title']}'")

        save_story_state(state)

        # Refill is non-critical after completion: do not turn a published job into a failed run.
        completed = set(state.get("completed_story_ids", []))
        remaining = sum(1 for s in bank if s["id"] not in completed)
        if remaining < 2:
            try:
                print(f"\n[Vault] Only {remaining} stories remaining. Pre-emptive refill...")
                refill_story_vault(load_story_bank(), count=4)
            except Exception as exc:
                print(f"[Vault] ⚠️  Refill deferred: {exc}")
    elif not args.dry_run:
        print("[State] Uploads are incomplete; state was intentionally not advanced.")
    else:
        print("\n[State] ⏭️  Skipped state advancement (--dry-run).")

    print("\n" + "=" * 60)
    print("✅ Pipeline complete!")
    print(f"📁 Output files in: {os.path.abspath(work_dir)}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
