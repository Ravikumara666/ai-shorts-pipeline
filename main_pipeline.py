from __future__ import annotations

import os
import sys
import json
import asyncio
import subprocess
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import requests
from pydantic import BaseModel, Field
from google import genai
import edge_tts
from dotenv import load_dotenv

load_dotenv()

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


FFMPEG  = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")


# =====================================================================
# 1. STRUCTURED OUTPUT SCHEMA
# =====================================================================
class LocalizedMetadata(BaseModel):
    title: str = Field(description="YouTube title with Part X and #Shorts, max 100 chars")
    description: str = Field(description="YouTube description with 3-4 hashtags and SEO keywords")
    tags: list[str] = Field(description="Exactly 10 high-search regional SEO tags")
    narration_scenes: list[str] = Field(
        description="Exactly 5 voiceover narrations in native script, 2-3 sentences each"
    )


class MultiLangEpisode(BaseModel):
    episode_summary_for_next_part: str = Field(
        description="2-3 sentence summary of this episode for continuity in the next part"
    )
    visual_prompts_english: list[str] = Field(
        description="Exactly 5 detailed English image prompts. Each MUST begin with the character_visual_anchor text for visual consistency."
    )
    kannada: LocalizedMetadata
    telugu: LocalizedMetadata
    hindi: LocalizedMetadata


# Voice Mapping for Microsoft Edge-TTS (100% Free Neural Voices)
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
# 2. STORY VAULT MANAGEMENT
# =====================================================================
STORY_BANK_PATH = Path("story_bank.json")
STORY_STATE_PATH = Path("story_state.json")


def load_story_bank() -> list[dict]:
    """Load the story vault."""
    if not STORY_BANK_PATH.exists():
        sys.exit("[FATAL] story_bank.json not found.")
    with open(STORY_BANK_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_story_state() -> dict:
    """Load current pipeline state."""
    if not STORY_STATE_PATH.exists():
        sys.exit("[FATAL] story_state.json not found.")
    with open(STORY_STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_story_state(state: dict) -> None:
    """Persist pipeline state for the next cron run."""
    with open(STORY_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    print("[State] ✅ story_state.json saved.")


def get_current_story(state: dict, bank: list[dict]) -> dict:
    """Find the current story from the bank by ID."""
    story_id = state.get("current_story_id")
    for story in bank:
        if story["id"] == story_id:
            return story
    sys.exit(f"[FATAL] Story ID '{story_id}' not found in story_bank.json.")


def advance_to_next_story(state: dict, bank: list[dict]) -> None:
    """Move to the next unfinished story in the vault after completing an arc."""
    completed = set(state.get("completed_story_ids", []))
    completed.add(state["current_story_id"])
    state["completed_story_ids"] = list(completed)

    # Find next uncompleted story
    for story in bank:
        if story["id"] not in completed:
            state["current_story_id"] = story["id"]
            state["current_episode"] = 1
            state["previous_summary"] = "None - This is Episode 1. Start the story with an exciting hook."
            print(f"[Vault] ➡️  Next story: '{story['series_title']}' ({story['id']})")
            return

    # All stories completed — trigger vault refill
    print("[Vault] ⚠️  All stories in the vault are completed! Triggering auto-refill...")
    refill_story_vault(bank, count=4)

    # Reload and pick the first new story
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
    id: str = Field(description="Unique story ID like story_007, story_008, etc.")
    category: str = Field(description="Category like 'Panchatantra Fable', 'South Indian Folklore', 'Tenali Rama Wit', 'Mythological Adventure', 'Village Wisdom', 'Vijayanagara Mystery'")
    series_title: str = Field(description="Catchy series title for YouTube")
    character_visual_anchor: str = Field(
        description="Detailed 3D Pixar-style character description with specific clothing colors, facial features, accessories. Must be reusable across all 5 episodes for visual consistency."
    )
    core_plot: str = Field(description="One-paragraph plot summary with moral theme")
    episode_beats: list[str] = Field(
        description="Exactly 5 episode beat descriptions, each labeled Part 1 through Part 5"
    )


class StoryVaultRefill(BaseModel):
    new_stories: list[NewStoryEntry] = Field(
        description="List of new story entries to add to the vault"
    )


def refill_story_vault(existing_bank: list[dict], count: int = 4) -> None:
    """Use Gemini to generate new stories and append them to the vault."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("[FATAL] GEMINI_API_KEY not set.")

    client = genai.Client(api_key=api_key)

    existing_titles = [s["series_title"] for s in existing_bank]
    existing_ids = [s["id"] for s in existing_bank]
    max_id_num = max(int(sid.split("_")[1]) for sid in existing_ids) if existing_ids else 0

    prompt = f"""You are a master Indian storyteller and children's content creator.

EXISTING STORIES (DO NOT REPEAT THESE):
{json.dumps(existing_titles, indent=2)}

Generate exactly {count} BRAND NEW story entries for a YouTube Shorts vault. Requirements:

1. Each story must be from authentic Indian folklore, mythology, Panchatantra, Tenali Rama tales, regional legends, or village wisdom traditions.
2. Categories to choose from: 'Panchatantra Fable', 'South Indian Folklore', 'Tenali Rama Wit', 'Mythological Adventure', 'Village Wisdom', 'Vijayanagara Mystery', 'Rajasthani Legend', 'Bengali Folk Tale', 'Kerala Wisdom'
3. `character_visual_anchor`: Write a VERY detailed 3D Pixar-style character description including:
   - Exact age, skin tone, facial features
   - Specific clothing with EXACT colors (saffron, emerald, crimson, etc.)
   - Unique accessories or companions (parrot, staff, jewel, etc.)
   - Background setting hint
   - Must end with: '3D Pixar Disney animation style, vibrant lighting'
4. Each story needs exactly 5 episode beats (Part 1 through Part 5) that form a complete serialized arc with hooks and cliffhangers.
5. Part 5 must always end with a clear moral lesson.
6. Story IDs must be: story_{str(max_id_num + 1).zfill(3)}, story_{str(max_id_num + 2).zfill(3)}, etc.
7. Make stories DIVERSE — different regions, time periods, character types (animals, children, kings, villagers, mythological figures)."""

    print(f"[Vault Refill] Generating {count} new stories with Gemini...")
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "response_schema": StoryVaultRefill,
            "temperature": 0.9,
        },
    )

    refill = StoryVaultRefill.model_validate_json(response.text)
    new_entries = [entry.model_dump() for entry in refill.new_stories]

    # Append to existing bank
    updated_bank = existing_bank + new_entries
    with open(STORY_BANK_PATH, "w", encoding="utf-8") as f:
        json.dump(updated_bank, f, indent=4, ensure_ascii=False)

    print(f"[Vault Refill] ✅ Added {len(new_entries)} new stories to vault.")
    for entry in new_entries:
        print(f"    📖 {entry['series_title']} ({entry['category']})")


# =====================================================================
# 4. GENERATE MULTI-LANGUAGE EPISODE WITH GEMINI 2.5 FLASH
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
        "End Scene 5 by resolving the story with the moral lesson. "
        "Give a warm, satisfying conclusion."
        if is_finale
        else (
            f"End Scene 5 with an exciting cliffhanger asking viewers to "
            f"subscribe for Part {ep_num + 1}!"
        )
    )

    prompt = f"""You are a master Indian storyteller creating a high-retention 3D Pixar-style animated YouTube Shorts series for kids and families.

STORY DETAILS:
- Series: {story['series_title']}
- Category: {story['category']}
- Core Plot: {story['core_plot']}
- Current Episode: Part {ep_num} of {max_ep}
- Episode Beat to Follow: {current_beat}
- What happened previously: {state['previous_summary']}

CHARACTER VISUAL ANCHOR (use this EXACT description at the START of every visual prompt):
{character_anchor}

INSTRUCTIONS:
1. Create exactly 5 scenes for a ~50-second Short, following the episode beat above.
2. `visual_prompts_english`: Write 5 DETAILED English visual prompts.
   - Each prompt MUST START with the character visual anchor text above for visual consistency across episodes.
   - Then add the scene-specific action, setting, and emotion.
   - End each with: '9:16 vertical portrait layout, cinematic lighting'
3. Write the matching 5-scene voiceover narration in natural, engaging spoken:
   - KANNADA (Kannada script ಕನ್ನಡ)
   - TELUGU (Telugu script తెలుగు)
   - HINDI (Devanagari script हिन्दी)
   Each scene narration should be 2-3 sentences of dramatic storytelling.
4. Start Scene 1 with a strong hook. {finale_instruction}
5. Provide localized YouTube titles (include 'Part {ep_num}' and '#Shorts'), descriptions with 3-4 relevant hashtags, and exactly 10 high-search regional tags for each language.
6. `episode_summary_for_next_part`: Write a 2-3 sentence summary of this episode's events for continuity."""

    print(
        f"[Gemini] Generating '{story['series_title']}' Part {ep_num} "
        f"({story['category']})..."
    )
    response = client.models.generate_content(
        model="gemini-2.5-flash",
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
# 5. DOWNLOAD 5 AI IMAGES ONCE (POLLINATIONS FREE FLUX API)
# =====================================================================
def generate_shared_images(
    visual_prompts: list[str], output_dir: str
) -> list[str]:
    os.makedirs(output_dir, exist_ok=True)
    image_paths: list[str] = []

    for idx, prompt in enumerate(visual_prompts):
        img_path = os.path.join(output_dir, f"scene_{idx}.jpg")

        # Skip if already downloaded (re-run resilience)
        if os.path.isfile(img_path) and os.path.getsize(img_path) > 10_000:
            print(f"[Visuals] Scene {idx + 1}/5 already exists, skipping.")
            image_paths.append(img_path)
            continue

        encoded = urllib.parse.quote(prompt)
        url = (
            f"https://image.pollinations.ai/prompt/{encoded}"
            f"?width=1080&height=1920&seed={100 + idx}&nologo=true&model=flux"
        )

        # Retry with exponential backoff: 5s, 10s, 20s
        for attempt in range(1, 4):
            try:
                print(
                    f"[Visuals] Downloading Scene {idx + 1}/5 "
                    f"(attempt {attempt}/3)..."
                )
                resp = requests.get(url, timeout=120)
                resp.raise_for_status()

                # Validate we got an actual image
                content_type = resp.headers.get("content-type", "")
                if "image" not in content_type and len(resp.content) < 10_000:
                    raise ValueError(
                        f"Response is not an image (content-type: {content_type}, "
                        f"size: {len(resp.content)} bytes)"
                    )

                with open(img_path, "wb") as f:
                    f.write(resp.content)
                print(
                    f"[Visuals] ✅ Scene {idx + 1} saved "
                    f"({len(resp.content) // 1024} KB)"
                )
                break
            except (requests.RequestException, IOError, ValueError) as e:
                wait = 5 * (2 ** (attempt - 1))  # 5, 10, 20
                print(
                    f"[Visuals] ⚠️  Attempt {attempt} failed: {e}. "
                    f"Retrying in {wait}s..."
                )
                if attempt == 3:
                    sys.exit(
                        f"[FATAL] Failed to download Scene {idx + 1} "
                        f"after 3 attempts."
                    )
                time.sleep(wait)

        image_paths.append(img_path)

    return image_paths


# =====================================================================
# 6. GENERATE REGIONAL VOICEOVER & EXACT DURATION
# =====================================================================
async def synthesize_scene_audio(
    text: str, voice: str, out_mp3: str
) -> float:
    """Synthesize TTS audio and return exact duration in seconds."""
    communicate = edge_tts.Communicate(text, voice, rate="+5%")
    await communicate.save(out_mp3)

    cmd = [
        FFPROBE,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        out_mp3,
    ]
    duration = float(subprocess.check_output(cmd).decode().strip())
    print(f"    Audio: {os.path.basename(out_mp3)} -> {duration:.2f}s")
    return duration


# =====================================================================
# 7. RENDER LANGUAGE-SPECIFIC SHORT (KEN BURNS + CONCATENATE)
# =====================================================================
async def build_language_video(
    lang_key: str,
    meta: LocalizedMetadata,
    image_paths: list[str],
    work_dir: str,
) -> str:
    """Build a single language Short from shared images + TTS narration."""
    voice = LANGUAGE_CONFIG[lang_key]["voice"]
    lang_dir = os.path.join(work_dir, lang_key)
    os.makedirs(lang_dir, exist_ok=True)

    print(f"\n[{lang_key.upper()}] Building Short with voice {voice}...")

    segment_files: list[str] = []
    for i, (scene_text, img_path) in enumerate(
        zip(meta.narration_scenes, image_paths)
    ):
        audio_path = os.path.join(lang_dir, f"audio_{i}.mp3")
        seg_path = os.path.join(lang_dir, f"seg_{i}.mp4")

        duration = await synthesize_scene_audio(scene_text, voice, audio_path)
        frames = int(duration * 30) + 15  # 30 fps + slight buffer

        ffmpeg_cmd = [
            FFMPEG, "-y",
            "-loop", "1", "-i", img_path,
            "-i", audio_path,
            "-vf", (
                f"scale=1080:1920,"
                f"zoompan=z='min(zoom+0.0012,1.3)'"
                f":d={frames}"
                f":x='iw/2-(iw/zoom/2)'"
                f":y='ih/2-(ih/zoom/2)'"
                f":s=1080x1920:fps=30,"
                f"format=yuv420p"
            ),
            "-c:v", "libx264", "-preset", "veryfast",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", seg_path,
        ]
        subprocess.run(
            ffmpeg_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        segment_files.append(seg_path)
        print(f"    ✅ Segment {i + 1}/5 rendered")

    # Concatenate all segments into final Short
    concat_list = os.path.join(lang_dir, "concat.txt")
    with open(concat_list, "w") as f:
        for seg in segment_files:
            f.write(f"file '{os.path.abspath(seg)}'\n")

    final_mp4 = os.path.join(work_dir, f"final_{lang_key}.mp4")
    subprocess.run(
        [
            FFMPEG, "-y", "-f", "concat", "-safe", "0",
            "-i", concat_list, "-c", "copy", final_mp4,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )

    print(f"[{lang_key.upper()}] ✅ Final Short -> {final_mp4}")
    return final_mp4


# =====================================================================
# 8. MAIN ORCHESTRATOR
# =====================================================================
def get_mock_episode_data(story: dict, ep_num: int) -> MultiLangEpisode:
    """Generate mock episode data for offline or testing runs without calling Gemini."""
    anchor = story["character_visual_anchor"]
    beat = story["episode_beats"][ep_num - 1] if ep_num <= len(story["episode_beats"]) else ""
    return MultiLangEpisode(
        episode_summary_for_next_part=f"Summary of {story['series_title']} Part {ep_num}: {beat}",
        visual_prompts_english=[
            f"{anchor}, scene {i+1}, standing near the historic ruins, 9:16 vertical portrait layout, cinematic lighting"
            for i in range(5)
        ],
        kannada=LocalizedMetadata(
            title=f"{story['series_title']} - Part {ep_num} | ಕನ್ನಡ ಕಥೆಗಳು #Shorts",
            description=f"ಕನ್ನಡದಲ್ಲಿ ಸುಂದರ ಕಥೆ - {story['series_title']} ಭಾಗ {ep_num}. #Shorts #KannadaStories #Folklore",
            tags=["ಕನ್ನಡ ಕಥೆಗಳು", "Kannada Stories", "Moral Stories", "Panchatantra", "Shorts", "Trending", "Kids Stories", "Folklore", "Hampi", "Karnataka"],
            narration_scenes=[
                "ಒಂದು ಕಾಲದಲ್ಲಿ ಹಂಪಿಯ ಪವಿತ್ರ ನೆಲದಲ್ಲಿ ಒಂದು ಅದ್ಭುತ ರಹಸ್ಯ ಅಡಗಿತ್ತು.",
                "ಬುದ್ಧಿವಂತ ಪುಟ್ಟ ತನ್ನ ಪುಟ್ಟ ಗಿಳಿಯೊಂದಿಗೆ ಹಳೆಯ ಬಾವಿಯ ಬಳಿ ನಿಂತಿದ್ದನು.",
                "ನೀರಿನ ಒಳಗಿನಿಂದ ನಿಗೂಢ ಧ್ವನಿಯೊಂದು ಅವನ ಹೆಸರನ್ನು ಕರೆಯುತ್ತಿತ್ತು.",
                "ಬಾವಿಯ ಮೆಟ್ಟಿಲುಗಳ ಮೇಲೆ ಚಿನ್ನದ ಕೀಲಿಯೊಂದು ಪ್ರಕಾಶಮಾನವಾಗಿ ಹೊಳೆಯುತ್ತಿತ್ತು.",
                "ಆದರೆ ಆ ಕೀಲಿಯನ್ನು ಮುಟ್ಟಿದರೆ ಏನಾಗುತ್ತದೆ? ಮುಂದಿನ ಭಾಗವನ್ನು ನೋಡಲು ಈಗಲೇ ಸಬ್ಸ್ಕ್ರೈಬ್ ಮಾಡಿ!"
            ]
        ),
        telugu=LocalizedMetadata(
            title=f"{story['series_title']} - Part {ep_num} | తెలుగు కథలు #Shorts",
            description=f"తెలుగులో అద్భుతమైన కథ - {story['series_title']} భాగం {ep_num}. #Shorts #TeluguStories #Folklore",
            tags=["తెలుగు కథలు", "Telugu Stories", "Moral Stories", "Panchatantra", "Shorts", "Trending", "Kids Stories", "Folklore", "Tenali Rama", "Andhra"],
            narration_scenes=[
                "ఒకప్పుడు హంపీ పవిత్ర భూమిలో ఒక అద్భుతమైన రహస్యం దాగి ఉంది.",
                "తెలివైన పుట్టా తన చిన్న చిలుకతో కలిసి పాత బావి వద్ద నిలబడ్డాడు.",
                "నీటి లోపలి నుండి ఒక వింత శబ్దం అతని పేరును పిలుస్తోంది.",
                "బావి మెట్లపై బంగారు తాళంచెవి కాంతివంతంగా మెరుస్తోంది.",
                "కానీ ఆ తాళంచెవిని ముట్టుకుంటే ఏమవుతుంది? తరువాతి భాగం కోసం ఇప్పుడే సబ్‌స్క్రైబ్ చేయండి!"
            ]
        ),
        hindi=LocalizedMetadata(
            title=f"{story['series_title']} - Part {ep_num} | हिंदी कहानियाँ #Shorts",
            description=f"हम्पी की जादुई लोककथा - {story['series_title']} भाग {ep_num}. #Shorts #HindiKahaniya #Folklore",
            tags=["हिंदी कहानियां", "Hindi Kahaniya", "Moral Stories", "Panchatantra", "Shorts", "Trending", "Kids Stories", "Folklore", "Tenali Raman", "Indian Myths"],
            narration_scenes=[
                "एक समय की बात है, हम्पी की पवित्र भूमि में एक अनोखा रहस्य छिपा हुआ था।",
                "चतुर पुट्टा अपने छोटे तोते के साथ पुराने कुएं के पास खड़ा था।",
                "कुएं के गहरे पानी से एक रहस्यमयी आवाज उसका नाम पुकार रही थी।",
                "सीढ़ियों पर एक जादुई सुनहरी चाबी दिव्य प्रकाश से चमक रही थी।",
                "लेकिन उस चाबी को छूते ही क्या होगा? अगला भाग देखने के लिए अभी सब्सक्राइब करें!"
            ]
        )
    )


async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Multi-Language YouTube Shorts Pipeline")
    parser.add_argument("--skip-upload", action="store_true", help="Render videos only; do not upload to YouTube")
    parser.add_argument("--dry-run", action="store_true", help="Run without persisting updated story state or vault")
    parser.add_argument("--mock", action="store_true", help="Use built-in mock script (no Gemini API calls)")
    parser.add_argument("--episode", type=int, default=None, help="Force a specific episode number")
    args = parser.parse_args()

    # Load state and vault
    state = load_story_state()
    bank = load_story_bank()
    story = get_current_story(state, bank)

    if args.episode is not None:
        state["current_episode"] = args.episode

    ep_num = state["current_episode"]
    max_ep = state["max_episodes_in_arc"]

    print("=" * 60)
    print(f"🎬 [{story['category']}] {story['series_title']}")
    print(f"   Episode {ep_num} of {max_ep}")
    print(f"   Character: {story['character_visual_anchor'][:80]}...")
    if args.mock:
        print("   Mode: MOCK DATA (No Gemini API call)")
    if args.skip_upload:
        print("   Upload: SKIPPED (--skip-upload)")
    if args.dry_run:
        print("   State Persistence: DISABLED (--dry-run)")
    print("=" * 60)

    # Step 1: Generate all content with Gemini (or mock)
    if args.mock:
        episode_data = get_mock_episode_data(story, ep_num)
    else:
        episode_data = generate_episode_data(state, story)

    # Step 2: Download shared images ONCE (with character visual anchoring)
    work_dir = "output_build"
    images = generate_shared_images(
        episode_data.visual_prompts_english,
        os.path.join(work_dir, "shared_images"),
    )

    # Step 3: Build Shorts for each language
    lang_map: dict[str, LocalizedMetadata] = {
        "kannada": episode_data.kannada,
        "telugu":  episode_data.telugu,
        "hindi":   episode_data.hindi,
    }

    video_paths: dict[str, str] = {}
    for lang_key, meta in lang_map.items():
        video_path = await build_language_video(
            lang_key, meta, images, work_dir
        )
        video_paths[lang_key] = video_path

        # Save localized metadata for the uploader
        meta_path = os.path.join(work_dir, f"meta_{lang_key}.json")
        with open(meta_path, "w", encoding="utf-8") as mf:
            mf.write(meta.model_dump_json(indent=2))

    # Step 4: Upload to YouTube (each language to its own channel)
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
                    category_id="24",  # Entertainment
                )
        except ImportError:
            print("[Upload] ⚠️  upload_to_youtube.py not found — skipping upload.")
        except Exception as e:
            print(f"[Upload] ⚠️  Upload failed: {e}")
    else:
        print("\n[Upload] ⏭️  Skipped upload as requested (--skip-upload).")

    # Step 5: Advance Story State
    if not args.dry_run:
        if ep_num >= max_ep:
            print(f"\n🏁 Arc complete: '{story['series_title']}'")
            advance_to_next_story(state, bank)
        else:
            state["current_episode"] = ep_num + 1
            state["previous_summary"] = episode_data.episode_summary_for_next_part
            print(
                f"\n➡️  Advanced to Episode {state['current_episode']} of "
                f"'{story['series_title']}'"
            )

        save_story_state(state)

        # Step 6: Pre-emptive vault refill check
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
