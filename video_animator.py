"""
video_animator.py — Free Image-to-Video (I2V) scene animation via Hugging Face ZeroGPU.

`animate_scene_to_video()` turns a still scene image into a short real-motion `.mp4` using
free Hugging Face ZeroGPU Gradio Spaces (primary: Lightricks/LTX-Video), authenticated with
HF_TOKEN. This is the $0 path chosen for the pipeline.

Everything is wrapped defensively: ZeroGPU quota limits, queue timeouts, a Space that is down
or has changed its API, a missing token, or a bad response all cause a clean fall-through. If
no Space produces a valid clip, it returns ``None`` and the caller keeps the static image with
Ken Burns motion — the render pipeline never crashes.

Design note: free ZeroGPU is quota-limited and queue-backed, so some scenes WILL fall back.
That is expected and acceptable. If reliable animation on every scene is later required, add a
paid provider (e.g. fal.ai) ahead of these Spaces.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

# Free HF ZeroGPU image-to-video Spaces, tried in order. All $0 (ZeroGPU + HF_TOKEN).
HF_I2V_SPACES = [
    "Lightricks/LTX-Video",            # primary — light & fast, best fit for free ZeroGPU
    "Lightricks/ltx-video-distilled",  # distilled LTX variant (if the above moved)
    "Wan-AI/Wan2.2-I2V-A14B",          # backup — higher quality but heavier
]

# A valid clip must be at least this big; smaller means an error page / stub, not a video.
MIN_VALID_BYTES = 50_000
_VIDEO_EXTS = (".mp4", ".webm", ".mov", ".m4v")


def _valid_mp4(path: str) -> bool:
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > MIN_VALID_BYTES
    except OSError:
        return False


def _build_motion_prompt(motion_prompt: str) -> str:
    base = (motion_prompt or "gentle cinematic camera move").strip().strip(",")
    return (
        f"Smooth 3D Pixar animated film shot, {base}, subtle natural character breathing and "
        f"blinking, expressive movement, cinematic lighting, 24fps, stable camera"
    )


def _find_video_file(result: object) -> Optional[str]:
    """Walk an arbitrary Gradio return value for a local video file path."""
    def walk(v: object) -> Optional[str]:
        if isinstance(v, str):
            return v if v.lower().endswith(_VIDEO_EXTS) and os.path.isfile(v) else None
        if isinstance(v, (list, tuple)):
            for it in v:
                found = walk(it)
                if found:
                    return found
        if isinstance(v, dict):
            for key in ("video", "path", "name", "url", "value"):
                if key in v:
                    found = walk(v[key])
                    if found:
                        return found
            for it in v.values():
                found = walk(it)
                if found:
                    return found
        return None

    return walk(result)


def _pick_i2v_endpoint(api: dict):
    """From a Gradio view_api() dict, find an endpoint that accepts an image/file input."""
    try:
        named = (api or {}).get("named_endpoints", {})
        for ep, meta in named.items():
            for p in meta.get("parameters", []):
                ptype = str((p.get("python_type", {}) or {}).get("type", "") or p.get("type", "")).lower()
                comp = str(p.get("component", "")).lower()
                if "filepath" in ptype or comp in ("image", "video"):
                    return ep, p.get("parameter_name") or p.get("label")
    except Exception:
        pass
    return None, None


def _try_space(space_id: str, image_path: str, prompt: str, out_path: str, token: Optional[str]) -> bool:
    """Best-effort single-Space I2V call. Returns True only if a valid clip was saved."""
    try:
        from gradio_client import Client
    except Exception as e:
        print(f"[VideoGen]   gradio_client unavailable: {e}")
        return False
    try:
        from gradio_client import handle_file
    except Exception:
        handle_file = None  # older gradio_client passes the path directly

    img_arg = handle_file(image_path) if handle_file else image_path

    try:
        print(f"[VideoGen] HF ZeroGPU Space: {space_id} ...")
        client = Client(space_id, token=token)
        try:
            api = client.view_api(return_format="dict")
        except Exception:
            api = {}
        endpoint, img_param = _pick_i2v_endpoint(api)

        attempts = []
        if endpoint:
            attempts.append((endpoint, {(img_param or "image"): img_arg, "prompt": prompt}))
        attempts += [
            ("/infer", {"image": img_arg, "prompt": prompt}),
            ("/generate", {"image": img_arg, "prompt": prompt}),
            ("/image_to_video", {"image": img_arg, "prompt": prompt}),
            ("/predict", {"image": img_arg, "prompt": prompt}),
        ]

        result = None
        for ep, kwargs in attempts:
            try:
                result = client.predict(api_name=ep, **kwargs)
                if result:
                    break
            except Exception as e2:
                msg = str(e2).lower()
                if "quota" in msg or "gpu" in msg or "exceeded" in msg:
                    print(f"[VideoGen]   {space_id} ZeroGPU quota/limit: {str(e2)[:120]}")
                    return False
                continue
        if result is None:
            # Last resort: positional (image, prompt).
            try:
                result = client.predict(img_arg, prompt)
            except Exception:
                result = None

        vid = _find_video_file(result)
        if vid and os.path.isfile(vid):
            shutil.copyfile(vid, out_path)
            if _valid_mp4(out_path):
                print(f"[VideoGen] ✅ Animated clip saved via {space_id}.")
                return True
    except Exception as e:
        print(f"[VideoGen]   HF Space {space_id} failed: {str(e)[:140]}")
    return False


def animate_scene_to_video(
    image_path: str, motion_prompt: str, output_mp4_path: str, duration_sec: float
) -> Optional[str]:
    """Animate one scene image into a short motion clip using free HF ZeroGPU Spaces.

    Returns the mp4 path on success, or None to signal the caller should fall back to Ken Burns
    image motion for this scene. Never raises.
    """
    if not os.path.isfile(image_path):
        print(f"[VideoGen] source image missing: {image_path}")
        return None

    token = os.getenv("HF_TOKEN", "").strip() or None
    if not token:
        print("[VideoGen] HF_TOKEN not set; free ZeroGPU I2V unavailable — using Ken Burns.")
        return None

    prompt = _build_motion_prompt(motion_prompt)
    os.makedirs(os.path.dirname(os.path.abspath(output_mp4_path)) or ".", exist_ok=True)

    for space_id in HF_I2V_SPACES:
        try:
            if _try_space(space_id, image_path, prompt, output_mp4_path, token) and _valid_mp4(output_mp4_path):
                return output_mp4_path
        except Exception as e:
            print(f"[VideoGen] {space_id} unexpected error: {str(e)[:140]}")
        # Clean up any partial/invalid file before the next Space.
        if os.path.isfile(output_mp4_path) and not _valid_mp4(output_mp4_path):
            try:
                os.remove(output_mp4_path)
            except OSError:
                pass

    print(f"[VideoGen] ⚠️  Free I2V unavailable for this scene (quota/timeout); "
          f"falling back to Ken Burns for {os.path.basename(output_mp4_path)}.")
    return None
