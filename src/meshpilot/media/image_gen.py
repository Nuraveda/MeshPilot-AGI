"""AI image generation via MUapi.

Primary use: LinkedIn image posts (and later Twitter/Instagram) for text brands
that want visual pairing. Generates a PNG from a prompt and returns the local
path; the publisher uploads the image.

Default model is `nano-banana-pro` (Gemini 3 Pro Image) via MUapi. FLUX was the
default until 2026-09-26 and is GONE: at ~$0.003/image it was optimising the wrong
thing. A thumbnail is the single image that decides whether anyone clicks, and FLUX
cannot compose a scene or render legible type. nano-banana-pro costs ~$0.13 and can.

⚠️ EVERY image produced here is a PLATE: background, subject, light, texture — and
NO TEXT AND NO LOGOS. Not because the model cannot draw letters (nano-banana-pro is
good at them) but because it cannot be RELIED ON to, and it does not have the brand's
mark, palette or font. Type and logos are composited afterwards with Pillow
(media/render/), which is exact and repeatable. "AI for visuals, code for typography."
Swap the model via MUAPI_IMAGE_MODEL. All calls go through a
tenacity retry so transient network/503s don't drop an image.

Outputs land under `{settings.video_storage_path}/images/{brand_id}/` with a
UUID filename. Re-runs never overwrite; each call produces a new file.
"""
from __future__ import annotations

import asyncio
import pathlib
import uuid
from typing import Literal

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from meshpilot.config import settings

log = structlog.get_logger(__name__)

# MUapi takes the ratio verbatim. ⚠️ It must be passed as a PARAMETER — asking for
# "16:9 landscape" in the prompt text is ignored and you get a square back (measured
# 2026-09-26 on nano-banana-pro).
_ASPECT_MAP: dict[str, str] = {
    "1:1":  "1:1",
    "4:5":  "4:5",
    "16:9": "16:9",               # YouTube thumbnail / Twitter
}

# Appended to EVERY plate. The old code only sent this to Leonardo, so the FLUX path
# happily baked gibberish captions into images the caller was about to draw real text
# onto.
#
# ⚠️ ADVISORY, NOT ENFORCED. nano-banana-pro is a Gemini-family model with no true negative
# conditioning — MUapi folds this into the prompt, so naming a thing can even summon it.
# Measured 2026-09-26: a plate asked for empty negative space still came back with faint
# hallucinated game-HUD fragments (minimap, ammo counter). That is tolerable only because
# `media/render/composite.prepare()` scrims the backdrop before any type is drawn, which
# sinks residual junk behind the copy. Do not drop the scrim on the assumption that plates
# come back clean — they do not.
MUAPI_POLL_ATTEMPTS = 60
MUAPI_POLL_INTERVAL_S = 4.0

_PLATE_NEG = (
    "text, letters, words, captions, subtitles, typography, watermark, signature, "
    "logo, brand mark, numbers, UI, interface, borders, frame"
)

AspectRatio = Literal["1:1", "4:5", "16:9"]


class ImageGenError(RuntimeError):
    """Raised when the provider returns no image or the download fails."""


async def generate_image(
    prompt: str,
    brand_id: str,
    aspect: AspectRatio = "1:1",
    *,
    plate: bool = False,
) -> pathlib.Path:
    """Generate an image via MUapi, download it, return the local path.

    `plate=True` asks for a TEXT-FREE backdrop for the Pillow card compositor, which draws the
    typography itself. The default renders FINISHED artwork, headline included.

    ⚠️ The default flipped 2026-09-26, and the reason is worth keeping. The no-text rule was
    inherited from FLUX, which baked gibberish captions into images. nano-banana-pro is a
    Gemini-family model and is good at typography — measured side by side on the same brief, it
    spelled "CONQUEROR LOBBY", "17 KILLS" and "1V4 CLUTCH" correctly with gradients, outlines and
    drop shadows, while the Pillow card rendered flat type that the operator rejected outright.
    Suppressing text by default was throwing away the better output to avoid a FLUX-era problem
    this model does not have.

    Pillow is still the ONLY way a real brand mark or a social icon reaches an image: the model
    approximates a logo, and an approximated logo is worse than none.

    Raises ImageGenError on failure. DISPATCH_MODE=dry_run short-circuits with
    a placeholder path that doesn't exist on disk (caller should skip upload
    in dry-run mode anyway).
    """
    s = settings()
    if s.is_dry_run:
        log.info("image_gen.dry_run", brand_id=brand_id, prompt=prompt[:80])
        return pathlib.Path(f"/tmp/dry-run-image-{uuid.uuid4().hex[:8]}.png")

    if not s.muapi_api_key:
        raise ImageGenError("MUAPI_API_KEY is not set")

    out_dir = pathlib.Path(s.video_storage_path) / "images" / brand_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{uuid.uuid4().hex}.png"

    image_url = await _generate_via_muapi(prompt, aspect, model=s.muapi_image_model,
                                          plate=plate)
    await _download(image_url, out_path)

    log.info(
        "image_gen.done",
        brand_id=brand_id,
        path=str(out_path),
        size_kb=out_path.stat().st_size // 1024,
    )
    return out_path


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type((httpx.HTTPError, asyncio.TimeoutError, ImageGenError)),
)
async def _generate_via_muapi(prompt: str, aspect: AspectRatio, model: str,
                              *, plate: bool = False) -> str:
    """Submit to MUapi, poll for the result, return the image URL.

    MUapi is submit+poll, not request/response: POST {base}/{model} returns a
    request_id, then GET {base}/predictions/{id}/result until it completes. The model
    slug goes straight in the path, so a new model needs no code change.
    """
    import json as _json

    s_ = settings()
    key = s_.muapi_api_key
    if not key:
        raise ImageGenError("MUAPI_API_KEY is not set")
    base = (s_.muapi_api_base or "https://api.muapi.ai/api/v1").rstrip("/")
    headers = {"x-api-key": key, "Content-Type": "application/json"}
    payload = {
        "prompt": prompt,
        # PARAMETER, not prose — see _ASPECT_MAP.
        "aspect_ratio": _ASPECT_MAP.get(aspect, "1:1"),
        # Only a plate suppresses type — finished artwork is SUPPOSED to carry it.
        **({"negative_prompt": _PLATE_NEG} if plate else {}),
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=180.0)) as client:
        sub = await client.post(f"{base}/{model}", headers=headers, content=_json.dumps(payload))
        if sub.status_code >= 400:
            raise ImageGenError(f"muapi submit {model} -> {sub.status_code}: {sub.text[:300]}")
        rid = (sub.json() or {}).get("request_id")
        if not rid:
            raise ImageGenError(f"muapi submit {model}: no request_id in {sub.text[:200]}")

        # Generous ceiling: nano-banana-pro is slower than FLUX was, which is the
        # trade being made deliberately.
        for _ in range(MUAPI_POLL_ATTEMPTS):
            await asyncio.sleep(MUAPI_POLL_INTERVAL_S)
            res = await client.get(f"{base}/predictions/{rid}/result", headers=headers)
            if res.status_code >= 400:
                raise ImageGenError(f"muapi poll {rid} -> {res.status_code}: {res.text[:200]}")
            body = res.json() or {}
            status = body.get("status")
            if status == "completed":
                outs = body.get("outputs") or []
                if not outs:
                    raise ImageGenError(f"muapi {model} completed with no outputs")
                return outs[0]
            if status == "failed":
                raise ImageGenError(f"muapi {model} failed: {str(body)[:300]}")
    raise ImageGenError(f"muapi {model} did not finish within "
                        f"{MUAPI_POLL_ATTEMPTS * MUAPI_POLL_INTERVAL_S:.0f}s")


async def _download(url: str, out_path: pathlib.Path) -> None:
    """Stream-download an image URL to disk."""
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        out_path.write_bytes(resp.content)


# ---------------------------------------------------------------------------
# Leonardo.ai — poster/illustration backgrounds
#
# Use for slide / quote-card BACKGROUND assets only — never ask Leonardo to
# render real copy. Text is rendered by Pillow on top of the background.
# This is the "AI for visuals, code for typography" pattern: AI image models
# are not layout engines, so we only ask them for the parts they're good at.
#
# Leonardo's REST API is async: POST /generations creates a job, then we
# poll GET /generations/<id> until status="COMPLETE" and image URLs appear.
# Phoenix model is poster-grade illustration; Vision XL is photoreal.
# ---------------------------------------------------------------------------

# Pixel dimensions per aspect — Leonardo accepts 512–1536 on each axis. We
# generate at 1080-line dimensions so backgrounds match Pillow slide size
# (1080×1350) without large up/down-scaling.
_LEONARDO_PX: dict[str, tuple[int, int]] = {
    "1:1":  (1024, 1024),
    "4:5":  (1080, 1344),    # Leonardo rounds to 64 — 1344 ≈ 1350
    "4:3":  (1024, 768),
    "16:9": (1280, 720),
}


async def generate_background(
    prompt: str,
    brand_id: str,
    *,
    aspect: AspectRatio = "1:1",
    negative_prompt: str | None = None,
) -> pathlib.Path:
    """Generate a background image via Leonardo.ai. Returns local PNG path.

    The prompt should describe COMPOSITION, MOOD, COLOR, TEXTURE — not text.
    We hard-append a negative prompt that forbids text/letters/words so
    Leonardo doesn't bake gibberish typography in. The caller (carousel /
    quote_card) overlays real copy with Pillow afterward.

    Without LEONARDO_API_KEY it uses MUapi (nano-banana-pro) instead — never a
    weaker model, and never one that writes its own text.
    """
    s = settings()
    if s.is_dry_run:
        log.info("image_gen.bg.dry_run", brand_id=brand_id, prompt=prompt[:80])
        return pathlib.Path(f"/tmp/dry-run-bg-{uuid.uuid4().hex[:8]}.png")

    if not s.leonardo_api_key:
        # No Leonardo key: go straight to MUapi rather than a weaker model. The old
        # code fell back to FLUX here, which is how a "background" ended up with
        # baked-in gibberish captions under the Pillow text.
        log.info("image_gen.bg.no_leonardo_key.using_muapi", model=s.muapi_image_model)
        # plate=True: this is a BACKDROP for the Pillow compositor, so it must stay text-free
        # even though finished artwork is now the default everywhere else.
        return await generate_image(prompt=prompt, brand_id=brand_id, aspect=aspect, plate=True)

    out_dir = pathlib.Path(s.video_storage_path) / "images" / brand_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{uuid.uuid4().hex}.png"

    image_url = await _generate_via_leonardo(
        prompt=prompt, aspect=aspect, negative_prompt=negative_prompt,
    )
    await _download(image_url, out_path)

    log.info(
        "image_gen.bg.done",
        brand_id=brand_id, provider="leonardo",
        path=str(out_path), size_kb=out_path.stat().st_size // 1024,
    )
    return out_path


# Default negative prompt — keeps Leonardo from rendering text/UI noise that
# would clash with the Pillow overlay. Per-call override possible.
_LEONARDO_DEFAULT_NEG = (
    "text, letters, words, captions, typography, watermark, logo, "
    "signature, ui, interface, buttons, menu, low quality, blurry, "
    "jpeg artifacts, cartoon, clipart, stock photo, people, faces, hands"
)


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type((httpx.HTTPError, asyncio.TimeoutError, ImageGenError)),
)
async def _generate_via_leonardo(
    *,
    prompt: str,
    aspect: AspectRatio,
    negative_prompt: str | None,
) -> str:
    """POST to Leonardo, poll until complete, return the first image URL.

    Total wall time typically 6-15s on Phoenix; we cap at 90s to keep the
    carousel pipeline (parallel slide gen) bounded.
    """
    s = settings()
    width, height = _LEONARDO_PX.get(aspect, (1024, 1024))
    base = s.leonardo_base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {s.leonardo_api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {
        "modelId": s.leonardo_model_id,
        "prompt": prompt,
        "negative_prompt": negative_prompt or _LEONARDO_DEFAULT_NEG,
        "width": width,
        "height": height,
        "num_images": 1,
        # Phoenix-specific knobs — safe to send to other models too, ignored.
        "alchemy": True,
        "contrast": 3.5,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(f"{base}/generations", headers=headers, json=payload)
        if resp.status_code >= 400:
            raise ImageGenError(
                f"Leonardo POST /generations {resp.status_code}: {resp.text[:400]}"
            )
        data = resp.json()
        gen_id = (data.get("sdGenerationJob") or {}).get("generationId")
        if not gen_id:
            raise ImageGenError(f"Leonardo: no generationId in response: {data!r}")

        # Poll for completion. Phoenix typically 6-15s; we check every 2s up to 90s.
        deadline = asyncio.get_event_loop().time() + 90
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
            poll = await client.get(f"{base}/generations/{gen_id}", headers=headers)
            if poll.status_code >= 400:
                raise ImageGenError(
                    f"Leonardo GET /generations/{gen_id} {poll.status_code}: {poll.text[:400]}"
                )
            body = poll.json().get("generations_by_pk") or {}
            status = body.get("status")
            if status == "COMPLETE":
                images = body.get("generated_images") or []
                if not images:
                    raise ImageGenError(f"Leonardo COMPLETE but no images: {body!r}")
                url = images[0].get("url")
                if not url:
                    raise ImageGenError(f"Leonardo image had no URL: {images[0]!r}")
                return url
            if status == "FAILED":
                raise ImageGenError(f"Leonardo generation FAILED: {body!r}")

    raise ImageGenError(f"Leonardo generation {gen_id} timed out after 90s")
