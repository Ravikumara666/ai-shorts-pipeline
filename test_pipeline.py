from __future__ import annotations

"""
Smoke test: validates the Edge-TTS → FFmpeg pipeline locally
without needing any API keys or network for image generation.

Creates a single test scene (synthetic image + TTS audio + Ken Burns video).

Usage:
    python3 -m test_pipeline
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile

import edge_tts


def _find_tool(name: str) -> str:
    for candidate in [f"/opt/homebrew/bin/{name}", shutil.which(name) or ""]:
        if candidate and os.path.isfile(candidate):
            return candidate
    sys.exit(f"[FATAL] {name} not found.")


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")

# Test narrations — one per language
TEST_SCENES: dict[str, dict[str, str]] = {
    "kannada": {
        "voice": "kn-IN-GaganNeural",
        "text": "ಒಂದು ಕಾಲದಲ್ಲಿ, ಹಂಪಿಯ ಹಳೇ ದೇವಸ್ಥಾನದ ಹತ್ತಿರ ಒಂದು ಮಾಯಾ ನವಿಲು ವಾಸಿಸುತ್ತಿತ್ತು.",
    },
    "telugu": {
        "voice": "te-IN-MohanNeural",
        "text": "ఒకప్పుడు, హంపీ పురాతన దేవాలయం సమీపంలో ఒక మాయా నెమలి నివసించేది.",
    },
    "hindi": {
        "voice": "hi-IN-MadhurNeural",
        "text": "एक समय की बात है, हम्पी के प्राचीन मंदिर के पास एक जादुई मोर रहता था।",
    },
}


async def test_tts(lang: str, voice: str, text: str, work_dir: str) -> str:
    """Synthesize TTS and return the mp3 path."""
    mp3_path = os.path.join(work_dir, f"test_{lang}.mp3")
    communicate = edge_tts.Communicate(text, voice, rate="+5%")
    await communicate.save(mp3_path)

    cmd = [
        FFPROBE, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", mp3_path,
    ]
    duration = float(subprocess.check_output(cmd).decode().strip())
    print(f"  ✅ {lang.upper()} TTS: {duration:.2f}s -> {mp3_path}")
    return mp3_path


def create_test_image(work_dir: str) -> str:
    """Generate a solid-color 1080x1920 test image with FFmpeg."""
    img_path = os.path.join(work_dir, "test_image.png")
    subprocess.run(
        [
            FFMPEG, "-y",
            "-f", "lavfi", "-i",
            "color=c=0x2E4057:s=1080x1920:d=1",
            "-frames:v", "1",
            img_path,
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    size_kb = os.path.getsize(img_path) // 1024
    print(f"  ✅ Test image: {img_path} ({size_kb} KB)")
    return img_path


def test_ken_burns(img_path: str, mp3_path: str, out_mp4: str, duration: float) -> None:
    """Render a single Ken Burns segment."""
    frames = int(duration * 30) + 15
    cmd = [
        FFMPEG, "-y",
        "-loop", "1", "-i", img_path,
        "-i", mp3_path,
        "-vf", (
            f"scale=1080:1920,"
            f"zoompan=z='min(zoom+0.0012,1.3)'"
            f":d={frames}"
            f":x='iw/2-(iw/zoom/2)'"
            f":y='ih/2-(ih/zoom/2)'"
            f":s=1080x1920:fps=30,"
            f"format=yuv420p"
        ),
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", out_mp4,
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    size_kb = os.path.getsize(out_mp4) // 1024
    print(f"  ✅ Ken Burns video: {out_mp4} ({size_kb} KB)")


async def main() -> None:
    print("=" * 60)
    print("🧪 SMOKE TEST: Edge-TTS → FFmpeg Pipeline")
    print("=" * 60)

    work_dir = os.path.join(tempfile.gettempdir(), "yt_shorts_test")
    os.makedirs(work_dir, exist_ok=True)
    print(f"\nWork directory: {work_dir}\n")

    # Step 1: Create a test image
    print("[Step 1] Creating test image (1080x1920)...")
    img_path = create_test_image(work_dir)

    # Step 2: Test TTS for all 3 languages
    print("\n[Step 2] Testing Edge-TTS for all languages...")
    mp3_paths: dict[str, str] = {}
    for lang, config in TEST_SCENES.items():
        mp3_paths[lang] = await test_tts(
            lang, config["voice"], config["text"], work_dir
        )

    # Step 3: Render one Ken Burns video (Hindi only, to keep it quick)
    print("\n[Step 3] Testing Ken Burns video render (Hindi)...")
    duration_cmd = [
        FFPROBE, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", mp3_paths["hindi"],
    ]
    duration = float(subprocess.check_output(duration_cmd).decode().strip())
    out_mp4 = os.path.join(work_dir, "test_output.mp4")
    test_ken_burns(img_path, mp3_paths["hindi"], out_mp4, duration)

    # Step 4: Verify output
    print("\n[Step 4] Verifying output video...")
    probe_cmd = [
        FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,codec_name",
        "-of", "json", out_mp4,
    ]
    import json
    probe_out = json.loads(subprocess.check_output(probe_cmd).decode())
    stream = probe_out["streams"][0]
    print(f"  Codec: {stream['codec_name']}")
    print(f"  Resolution: {stream['width']}x{stream['height']}")
    print(f"  Frame rate: {stream['r_frame_rate']}")

    # Verify audio
    audio_cmd = [
        FFPROBE, "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,sample_rate",
        "-of", "json", out_mp4,
    ]
    audio_out = json.loads(subprocess.check_output(audio_cmd).decode())
    a_stream = audio_out["streams"][0]
    print(f"  Audio codec: {a_stream['codec_name']}")
    print(f"  Sample rate: {a_stream['sample_rate']}")

    # Final duration
    dur_cmd = [
        FFPROBE, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", out_mp4,
    ]
    vid_duration = float(subprocess.check_output(dur_cmd).decode().strip())
    print(f"  Duration: {vid_duration:.2f}s")

    passed = (
        stream["codec_name"] == "h264"
        and int(stream["width"]) == 1080
        and int(stream["height"]) == 1920
        and a_stream["codec_name"] == "aac"
        and vid_duration > 1.0
    )

    print("\n" + "=" * 60)
    if passed:
        print("✅ ALL SMOKE TESTS PASSED!")
        print("   The TTS → FFmpeg pipeline is working correctly.")
        print(f"   Test artifacts in: {work_dir}")
    else:
        print("❌ SOME TESTS FAILED — check output above.")
    print("=" * 60)

    # Cleanup
    # Uncomment to auto-cleanup: shutil.rmtree(work_dir)


if __name__ == "__main__":
    asyncio.run(main())

