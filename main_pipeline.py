from __future__ import annotations

import os
import sys
import json
import asyncio
import subprocess
import shutil
import time
import urllib.parse
import io
import textwrap
from pathlib import Path
from typing import Optional

import requests
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field
from google import genai
import edge_tts
from dotenv import load_dotenv

load_dotenv()

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
NUM_SCENES = 8


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


def call_gemini_with_fallback(client: genai.Client, contents: str, config: dict):
    """Call Gemini API with gemini-3.8-flash, falling back to gemini-2.5-flash if needed."""
    models_to_try = [GEMINI_MODEL]
    if "3.8" in GEMINI_MODEL:
        models_to_try.append("gemini-2.5-flash")
    else:
        models_to_try.append("gemini-3.8-flash")

    last_err = None
    for model_name in models_to_try:
        try:
            return client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
        except Exception as e:
            last_err = e
            if "NOT_FOUND" in str(e) or "404" in str(e):
                continue
            raise
    raise last_err or RuntimeError("Gemini model generation failed.")


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
    with open(STORY_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    print("[State] ✅ story_state.json saved.")


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

    client = genai.Client(api_key=api_key)
    existing_titles = [s["series_title"] for s in existing_bank]
    existing_ids = [s["id"] for s in existing_bank]
    max_id_num = max(int(sid.split("_")[1]) for sid in existing_ids) if existing_ids else 0

    prompt = f"""You are a master Indian storyteller and children's content creator.
EXISTING STORIES (DO NOT REPEAT):
{json.dumps(existing_titles, indent=2)}

Generate exactly {count} BRAND NEW authentic Indian folklore story arcs (Panchatantra, Tenali Rama, Vikram Betal, regional legends).
Each story needs a character_visual_anchor and exactly 5 episode beats (Part 1 through 5).
Story IDs must be: story_{str(max_id_num + 1).zfill(3)}, story_{str(max_id_num + 2).zfill(3)}, etc."""

    print(f"[Vault Refill] Generating {count} new stories with Gemini...")
    response = call_gemini_with_fallback(
        client,
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "response_schema": StoryVaultRefill,
            "temperature": 0.9,
        },
    )
    refill = StoryVaultRefill.model_validate_json(response.text)
    new_entries = [entry.model_dump() for entry in refill.new_stories]
    updated_bank = existing_bank + new_entries
    with open(STORY_BANK_PATH, "w", encoding="utf-8") as f:
        json.dump(updated_bank, f, indent=4, ensure_ascii=False)
    print(f"[Vault Refill] ✅ Added {len(new_entries)} new stories to vault.")


# =====================================================================
# 4. GENERATE 65-75s MULTI-LANGUAGE EPISODE WITH GEMINI
# =====================================================================
def generate_episode_data(state: dict, story: dict) -> MultiLangEpisode:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("[FATAL] GEMINI_API_KEY not set. Add it to .env or environment.")

    client = genai.Client(api_key=api_key)

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

    prompt = f"""You are a master Indian storyteller creating an animated YouTube Shorts episode.

STORY DETAILS:
- Series: {story['series_title']}
- Category: {story['category']}
- Core Plot: {story['core_plot']}
- Current Episode: Part {ep_num} of {max_ep}
- Beat to Follow: {current_beat}
- Previous Events: {state['previous_summary']}

CHARACTER ANCHOR:
{character_anchor}

CRITICAL DURATION & LENGTH REQUIREMENTS (TARGET: 65 TO 75 SECONDS TOTAL):
1. You MUST generate exactly {NUM_SCENES} progressive scenes (Scene 1 to Scene {NUM_SCENES}).
2. In EACH of the {NUM_SCENES} scenes, write 2 to 3 full, rich, dramatic spoken sentences (at least 22-28 words per scene) in:
   - KANNADA (Kannada script ಕನ್ನಡ)
   - TELUGU (Telugu script తెలుగు)
   - HINDI (Devanagari script हिन्दी)
   Each scene narration must take 7 to 9 seconds when spoken aloud ({NUM_SCENES} scenes × 8.5s = ~68 seconds total runtime).
3. Do NOT write brief 1-sentence summaries. Write vivid atmospheric descriptions, suspenseful narration, and character dialogue.
4. `visual_prompts_english`: Write exactly {NUM_SCENES} concise English visual prompts (UNDER 220 CHARACTERS EACH).
   Format: "3D Pixar Disney animation style, {character_anchor[:60]}, {{scene action/setting}}, vibrant cinematic lighting, 9:16 vertical"
5. Start Scene 1 with a powerful hook. {finale_instruction}
6. Provide localized YouTube titles (with Part {ep_num} and #Shorts), descriptions, and exactly 10 high-search regional tags.
7. `episode_summary_for_next_part`: Write a 2-3 sentence summary for continuity."""

    print(f"[Gemini] Generating '{story['series_title']}' Part {ep_num} ({NUM_SCENES} scenes for ~70s runtime)...")
    response = call_gemini_with_fallback(
        client,
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "response_schema": MultiLangEpisode,
            "temperature": 0.75,
        },
    )
    episode = MultiLangEpisode.model_validate_json(response.text)
    print("[Gemini] ✅ Episode data generated successfully.")
    return episode


# =====================================================================
# 5. DOWNLOAD REAL 3D PIXAR IMAGES (NO SOLID COLOR FALLBACK!)
# =====================================================================
def _validate_image_content(content: bytes) -> bool:
    """Validate that downloaded bytes form a real, non-solid-color image."""
    if len(content) < 30_000:
        return False
    try:
        img = Image.open(io.BytesIO(content))
        extrema = img.convert("L").getextrema()
        # Ensure variance between min and max pixel brightness (not a flat solid color)
        return (extrema[1] - extrema[0]) >= 25
    except Exception:
        return False


def _download_via_imagen3(prompt: str, img_path: str, gemini_api_key: str) -> bool:
    """Fallback: Generate real 3D Pixar 9:16 vertical image using Google Imagen 3 via Gemini API."""
    try:
        print("[Visuals] 🎨 Attempting Google Imagen 3 fallback (imagen-3.0-generate-002)...")
        client = genai.Client(api_key=gemini_api_key)
        resp = client.models.generate_images(
            model="imagen-3.0-generate-002",
            prompt=prompt,
            config=dict(
                number_of_images=1,
                aspect_ratio="9:16",
                output_mime_type="image/jpeg",
            ),
        )
        if resp.generated_images:
            img_bytes = resp.generated_images[0].image.image_bytes
            if _validate_image_content(img_bytes):
                with open(img_path, "wb") as f:
                    f.write(img_bytes)
                print(f"[Visuals] ✅ Scene saved via Google Imagen 3 ({len(img_bytes) // 1024} KB)")
                return True
    except Exception as e:
        print(f"[Visuals] ⚠️  Google Imagen 3 fallback attempt failed: {e}")
    return False


def generate_shared_images(visual_prompts: list[str], output_dir: str, gemini_api_key: str = "") -> list[str]:
    """Download real 3D Pixar vertical images from Pollinations with Imagen 3 fallback and strict validation."""
    os.makedirs(output_dir, exist_ok=True)
    image_paths: list[str] = []
    pollinations_key = os.getenv("POLLINATIONS_API_KEY", "").strip()

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    if pollinations_key:
        headers["Authorization"] = f"Bearer {pollinations_key}"

    for idx, prompt in enumerate(visual_prompts):
        img_path = os.path.join(output_dir, f"scene_{idx}.jpg")

        # Skip if already downloaded and verified
        if os.path.isfile(img_path):
            with open(img_path, "rb") as f:
                if _validate_image_content(f.read()):
                    print(f"[Visuals] Scene {idx + 1}/{len(visual_prompts)} verified cached image exists, skipping.")
                    image_paths.append(img_path)
                    continue

        # Keep prompt concise to prevent HTTP 414 / Cloudflare rejection
        clean_prompt = prompt.replace("\n", " ").strip()
        if len(clean_prompt) > 240:
            clean_prompt = clean_prompt[:235] + ", 9:16 vertical"
        encoded = urllib.parse.quote(clean_prompt)

        # Endpoints to try (Flux -> Turbo -> Standard)
        key_query = f"&key={pollinations_key}" if pollinations_key else ""
        endpoints = [
            f"https://image.pollinations.ai/prompt/{encoded}?width=1080&height=1920&seed={100 + idx}&nologo=true&model=flux{key_query}",
            f"https://image.pollinations.ai/prompt/{encoded}?width=1080&height=1920&seed={100 + idx}&nologo=true&model=turbo{key_query}",
            f"https://image.pollinations.ai/prompt/{encoded}?width=1080&height=1920&seed={100 + idx}&nologo=true{key_query}",
        ]

        saved = False
        hit_402 = False

        for attempt in range(1, 4):
            endpoint_url = endpoints[(attempt - 1) % len(endpoints)]
            try:
                print(f"[Visuals] Downloading Scene {idx + 1}/{len(visual_prompts)} (attempt {attempt}/3)...")
                resp = requests.get(endpoint_url, headers=headers, timeout=90)
                resp.raise_for_status()

                if _validate_image_content(resp.content):
                    with open(img_path, "wb") as f:
                        f.write(resp.content)
                    print(f"[Visuals] ✅ Scene {idx + 1} saved ({len(resp.content) // 1024} KB real artwork)")
                    saved = True
                    break
                else:
                    print(f"[Visuals] ⚠️  Attempt {attempt} returned invalid/undersized image ({len(resp.content)} bytes).")
            except requests.exceptions.HTTPError as http_err:
                if resp.status_code == 402:
                    print(f"[Visuals] ⚠️  Pollinations returned 402 (Payment/Key Required).")
                    hit_402 = True
                    break
                wait = 4 * attempt
                print(f"[Visuals] ⚠️  Attempt {attempt} failed: {http_err}. Retrying in {wait}s...")
                time.sleep(wait)
            except Exception as e:
                wait = 4 * attempt
                print(f"[Visuals] ⚠️  Attempt {attempt} failed: {e}. Retrying in {wait}s...")
                time.sleep(wait)

        # If Pollinations failed or hit 402, try Google Imagen 3 fallback
        if not saved and gemini_api_key:
            saved = _download_via_imagen3(clean_prompt, img_path, gemini_api_key)

        if not saved:
            sys.exit(
                f"[FATAL] Failed to download real 3D Pixar image for Scene {idx + 1}. "
                "Check network connection, Pollinations API key, or Google Imagen 3 access. Never falling back to solid color."
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
    """Generate a transparent 1080x1920 PNG with bold yellow text, black outline, and dark pill."""
    overlay = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _get_indic_font(lang, size=46)

    # Wrap to ~28-30 characters per line
    lines = textwrap.wrap(text, width=30)
    if len(lines) > 3:
        lines = lines[:3]  # Keep to 3 lines max for visual clarity
    wrapped = "\n".join(lines)

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

    for i, (scene_text, img_path) in enumerate(zip(meta.narration_scenes, image_paths)):
        audio_path = os.path.join(lang_dir, f"audio_{i}.mp3")
        overlay_png = os.path.join(lang_dir, f"sub_{i}.png")
        seg_path = os.path.join(lang_dir, f"seg_{i}.mp4")

        # 1. Synthesize audio
        duration = await synthesize_scene_audio(scene_text, voice, audio_path)

        # 2. Add 0.4s natural breath pause between scenes
        total_seg_duration = duration + 0.40
        total_running_time += total_seg_duration
        frames = int(total_seg_duration * 30) + 2

        # 3. Create Pillow subtitle overlay PNG
        create_subtitle_overlay_png(scene_text, lang_key, overlay_png)

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
        vf_filter = (
            f"[0:v]scale=1080:1920,{cam_expr}[bg];"
            f"[bg][1:v]overlay=0:0,"
            f"fade=t=in:st=0:d=0.3,fade=t=out:st={fade_out_st:.2f}:d=0.3,"
            f"format=yuv420p[vout]"
        )

        ffmpeg_cmd = [
            FFMPEG, "-y",
            "-loop", "1", "-i", os.path.abspath(img_path),
            "-i", os.path.abspath(overlay_png),
            "-i", os.path.abspath(audio_path),
            "-t", f"{total_seg_duration:.3f}",
            "-filter_complex", vf_filter,
            "-map", "[vout]",
            "-map", "2:a",
            "-c:v", "libx264", "-preset", "veryfast",
            "-af", "apad=pad_dur=0.40",
            "-c:a", "aac", "-b:a", "128k",
            "-ar", "44100", "-ac", "2",
            os.path.abspath(seg_path),
        ]
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

    # 7. Hard Verification of Video Duration (MUST BE >= 60.0s)
    dur_cmd = [
        FFPROBE, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", final_mp4,
    ]
    final_duration = float(subprocess.check_output(dur_cmd).decode().strip())
    print(f"[{lang_key.upper()}] ⏱️  Final Short Duration: {final_duration:.2f} seconds")

    if final_duration < 60.0:
        print(f"[{lang_key.upper()}] ⚠️  WARNING: Duration ({final_duration:.2f}s) is under 60 seconds target.")
    else:
        print(f"[{lang_key.upper()}] 🎯 ✅ DURATION VERIFIED: {final_duration:.2f}s (>= 60s target achieved!)")

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
    args = parser.parse_args()

    state = load_story_state()
    bank = load_story_bank()
    story = get_current_story(state, bank)

    if args.episode is not None:
        state["current_episode"] = args.episode

    ep_num = state["current_episode"]
    max_ep = state["max_episodes_in_arc"]

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

    # Step 2: Download real 3D Pixar images from Pollinations with Imagen 3 fallback
    work_dir = "output_build"
    images = generate_shared_images(
        episode_data.visual_prompts_english,
        os.path.join(work_dir, "shared_images"),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
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

    # Step 5: Upload to YouTube (if not skipped)
    if not args.skip_upload:
        try:
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
                    category_id="24",
                )
        except ImportError:
            print("[Upload] ⚠️  upload_to_youtube.py not found — skipping upload.")
        except Exception as e:
            print(f"[Upload] ⚠️  Upload failed: {e}")
    else:
        print("\n[Upload] ⏭️  Skipped upload as requested (--skip-upload).")

    # Step 6: Advance Story State
    if not args.dry_run:
        if ep_num >= max_ep:
            print(f"\n🏁 Arc complete: '{story['series_title']}'")
            advance_to_next_story(state, bank)
        else:
            state["current_episode"] = ep_num + 1
            state["previous_summary"] = episode_data.episode_summary_for_next_part
            print(f"\n➡️  Advanced to Episode {state['current_episode']} of '{story['series_title']}'")

        save_story_state(state)

        # Pre-emptive vault refill check
        completed = set(state.get("completed_story_ids", []))
        remaining = sum(1 for s in bank if s["id"] not in completed)
        if remaining < 2:
            print(f"\n[Vault] Only {remaining} stories remaining. Pre-emptive refill...")
            refill_story_vault(load_story_bank(), count=4)
    else:
        print("\n[State] ⏭️  Skipped state advancement (--dry-run).")

    print("\n" + "=" * 60)
    print("✅ Pipeline complete!")
    print(f"📁 Output files in: {os.path.abspath(work_dir)}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
