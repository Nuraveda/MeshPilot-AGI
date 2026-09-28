"""Image generation produces text-free PLATES via MUapi. FLUX is gone.

The failures worth pinning are silent ones: a plate that comes back square when the
caller asked for 16:9, or one with the model's own gibberish captions baked in under
the Pillow text the caller is about to draw.
"""
from __future__ import annotations

import pathlib

import httpx
import pytest

from meshpilot.media import image_gen as ig

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_http(monkeypatch, handler):
    """Route httpx through a MockTransport.

    ⚠️ Must capture the real class FIRST — a lambda that calls httpx.AsyncClient
    after patching httpx.AsyncClient recurses until the stack dies.
    """
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)))


class TestFluxIsGone:
    def test_no_flux_model_anywhere_in_the_image_pipeline(self):
        """FLUX schnell was ~$0.003 and could not compose or render type. A thumbnail
        is the one image that decides the click, so that was the wrong saving."""
        # Checks for a flux MODEL SLUG, not the word — the docstring deliberately
        # explains why FLUX was dropped, and that history is worth keeping.
        src = pathlib.Path(ig.__file__).read_text().lower()
        for slug in ('fal-ai/flux', 'flux-2-', 'flux-dev', 'flux-schnell', 'flux/schnell'):
            assert slug not in src, slug

    def test_the_text_in_image_route_is_removed(self):
        """gpt-image-2 existed to bake type INTO the image. Plates carry no type —
        Pillow does it — so keeping the function invites someone to undo the policy."""
        assert not hasattr(ig, "generate_designed_image")
        assert not hasattr(ig, "_generate_via_gpt_image_2")


class TestAspect:
    def test_ratios_are_muapi_values_not_fal_enums(self):
        assert ig._ASPECT_MAP["16:9"] == "16:9"
        assert "square_hd" not in ig._ASPECT_MAP.values()

    async def test_aspect_is_sent_as_a_parameter_not_left_to_the_prompt(self, monkeypatch):
        """MEASURED 2026-09-26: asking for '16:9 landscape' in prose returns a SQUARE.
        The ratio only takes effect as a parameter."""
        seen = {}

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                import json
                seen.update(json.loads(req.content))
                return httpx.Response(200, json={"request_id": "r1"})
            return httpx.Response(200, json={"status": "completed",
                                             "outputs": ["https://x/img.png"]})

        monkeypatch.setattr(ig.settings(), "muapi_api_key", "k")
        monkeypatch.setattr(ig, "MUAPI_POLL_INTERVAL_S", 0)
        _patch_http(monkeypatch, handler)
        url = await ig._generate_via_muapi("a dark arena", "16:9", model="nano-banana-pro")
        assert url == "https://x/img.png"
        assert seen["aspect_ratio"] == "16:9"


class TestTextSuppressionIsOptIn:
    """The no-text negative is a PLATE feature, not a global rule.

    ⚠️ This inverted on 2026-09-26. Suppressing type everywhere was inherited from FLUX, which
    baked gibberish captions into images. nano-banana-pro spells display copy correctly, and on
    the same brief the generated thumbnail beat the Pillow-typeset card outright — so finished
    artwork is the default and only the card compositor's backdrop asks for a bare plate.
    """

    def test_the_negative_prompt_forbids_text_and_logos(self):
        for banned in ("text", "letters", "words", "typography", "watermark", "logo"):
            assert banned in ig._PLATE_NEG

    @staticmethod
    def _capture(monkeypatch):
        seen = {}

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                import json
                seen.update(json.loads(req.content))
                return httpx.Response(200, json={"request_id": "r1"})
            return httpx.Response(200, json={"status": "completed", "outputs": ["https://x/i.png"]})

        monkeypatch.setattr(ig.settings(), "muapi_api_key", "k")
        monkeypatch.setattr(ig, "MUAPI_POLL_INTERVAL_S", 0)
        _patch_http(monkeypatch, handler)
        return seen

    async def test_a_plate_suppresses_text(self, monkeypatch):
        seen = self._capture(monkeypatch)
        await ig._generate_via_muapi("neon skyline", "1:1", model="nano-banana-pro", plate=True)
        assert "text" in seen["negative_prompt"]

    async def test_finished_artwork_does_not(self, monkeypatch):
        """The default must let the model render its own headline — that is the whole point of
        the change. Sending the negative here is what produced the output that got rejected."""
        seen = self._capture(monkeypatch)
        await ig._generate_via_muapi("thumbnail reading 17 KILLS", "16:9",
                                     model="nano-banana-pro")
        assert "negative_prompt" not in seen, (
            f"finished artwork was sent a no-text negative: {seen.get('negative_prompt')!r}")

    async def test_backgrounds_stay_text_free(self, monkeypatch):
        """generate_background feeds the Pillow compositor, so it must keep opting in even though
        the global default flipped — otherwise a backdrop comes back with baked captions under
        the drawn type, which is the exact bug the plate rule was written for."""
        captured = {}

        async def fake(prompt, brand_id, aspect="1:1", *, plate=False):
            captured["plate"] = plate
            return pathlib.Path("/tmp/x.png")

        monkeypatch.setattr(ig, "generate_image", fake)
        # Pin dispatch_mode: settings() is a shared singleton and another test leaving it on
        # dry_run made generate_background short-circuit before the fallback, so this passed
        # alone and failed in the suite.
        monkeypatch.setattr(ig.settings(), "dispatch_mode", "live")
        monkeypatch.setattr(ig.settings(), "leonardo_api_key", "")
        monkeypatch.setattr(ig.settings(), "muapi_api_key", "k")
        await ig.generate_background("a dark backdrop", "example", aspect="16:9")
        assert captured.get("plate") is True, "the Leonardo fallback stopped asking for a plate"


class TestFailures:
    async def test_a_missing_key_is_refused_clearly(self, monkeypatch):
        monkeypatch.setattr(ig.settings(), "muapi_api_key", "")
        with pytest.raises(ig.ImageGenError, match="MUAPI_API_KEY"):
            await ig._generate_via_muapi("x", "1:1", model="nano-banana-pro")

    async def test_a_failed_job_raises_rather_than_returning_nothing(self, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return httpx.Response(200, json={"request_id": "r1"})
            return httpx.Response(200, json={"status": "failed", "error": "nsfw"})

        monkeypatch.setattr(ig.settings(), "muapi_api_key", "k")
        monkeypatch.setattr(ig, "MUAPI_POLL_INTERVAL_S", 0)
        _patch_http(monkeypatch, handler)
        with pytest.raises(ig.ImageGenError, match="failed"):
            await ig._generate_via_muapi("x", "1:1", model="nano-banana-pro")


class TestStatementDoesNotOverrunTheFooter:
    """The plates are text-free now, so ALL type is drawn by Pillow. If the headline runs into the
    footer band it collides with the wordmark and the brand logo — which is exactly what the
    generated-text problem was supposed to stop. Measured end-to-end on a real render first."""

    @staticmethod
    def _spec(headline: str, subhead: str = "", fmt: str = "16:9"):
        from meshpilot.media.render import layouts
        from meshpilot.media.render.card import Palette
        return layouts.Spec(
            content=layouts.Content(headline=headline, kicker="SOLO VS SQUAD",
                                    subhead=subhead, wordmark="EXAMPLE BRAND"),
            fmt=fmt, palette=Palette())

    def test_headline_plus_subhead_stays_above_the_footer(self):
        from meshpilot.media.render import layouts
        g = layouts.statement_geometry(
            self._spec("17 KILLS IN A CONQUEROR LOBBY", "Gyro • 4 fingers • no resets"))
        assert g["bottom"] <= g["h"] - g["footer"], (
            f"content ends at {g['bottom']}px, footer band starts at {g['h'] - g['footer']}px "
            f"— the headline would collide with the wordmark and logo")

    def test_holds_for_long_copy_in_every_format(self):
        from meshpilot.media.render import layouts
        from meshpilot.media.render.card import SIZES
        long_hl = "SEVENTEEN KILLS IN A CONQUEROR LOBBY SOLO AGAINST FULL SQUADS NO RESETS"
        long_sub = "Gyro on, four fingers, no resets, no teammates, one single match start to end"
        for fmt in SIZES:
            g = layouts.statement_geometry(self._spec(long_hl, long_sub, fmt=fmt))
            assert g["bottom"] <= g["h"] - g["footer"], (
                f"{fmt}: content ends at {g['bottom']}px, footer starts at "
                f"{g['h'] - g['footer']}px")

    def test_subhead_height_is_actually_reserved(self):
        """A subhead must cost the headline room when the space is contested.

        Asserted with copy long enough to make it bite: short copy fits either way, so comparing
        the two there would pass whether or not the subhead were reserved at all.
        """
        from meshpilot.media.render import layouts
        long_hl = "SEVENTEEN KILLS IN A CONQUEROR LOBBY SOLO AGAINST FULL SQUADS NO RESETS"
        without = layouts.statement_geometry(self._spec(long_hl))
        with_sub = layouts.statement_geometry(
            self._spec(long_hl, "Gyro on, four fingers, no resets, one match start to end"))
        hl_h = lambda g: len(g["lines"]) * g["leading"]  # noqa: E731
        assert hl_h(with_sub) < hl_h(without), (
            f"headline took {hl_h(with_sub)}px with a subhead and {hl_h(without)}px without — "
            "the subhead's space is not being reserved")

    def test_footer_tracks_the_logo_lockup_not_a_flat_fraction(self):
        """The reserved footer must cover the mark `_wordmark` actually draws. It is sized off the
        WIDTH and offset a margin from the bottom, so a flat fraction of the HEIGHT under-reserved
        at 16:9 and the copy landed on the logo."""
        from PIL import Image

        from meshpilot.media.render import layouts
        from meshpilot.media.render.card import SIZES
        mark = Image.new("RGBA", (400, 400), (255, 255, 255, 255))
        for fmt, (w, h) in SIZES.items():
            spec = self._spec("17 KILLS IN A CONQUEROR LOBBY", "Gyro • 4 fingers", fmt=fmt)
            spec.wordmark_logo = mark
            margin = int(w * 0.09)
            lockup_top = h - margin - int(w * 0.085)   # exactly what _wordmark uses
            footer = layouts._footer_h(spec, w, h, margin)
            assert h - footer <= lockup_top, (
                f"{fmt}: reserved footer starts at {h - footer}px but the logo lockup starts at "
                f"{lockup_top}px — copy would be drawn over the mark")
