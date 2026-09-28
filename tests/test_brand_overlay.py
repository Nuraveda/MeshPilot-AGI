"""The overlay that puts a REAL mark on model-rendered artwork.

The model draws the headline now; it cannot draw our logo. These tests pin the division: the
overlay composites assets and must never become the typesetter again.
"""

from io import BytesIO

import pytest
from PIL import Image

from meshpilot.media.render import brand_overlay


def _art(w: int = 1280, h: int = 720, colour=(30, 30, 30)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, format="PNG")
    return buf.getvalue()


def _mark(colour=(255, 0, 0, 255), size=(200, 200)) -> Image.Image:
    return Image.new("RGBA", size, colour)


def _open(data: bytes) -> Image.Image:
    return Image.open(BytesIO(data)).convert("RGB")


class TestItComposites:
    def test_a_logo_actually_lands_on_the_image(self):
        before = _open(_art())
        after = _open(brand_overlay.apply(_art(), logo=_mark()))
        assert after.size == before.size
        assert list(after.getdata()) != list(before.getdata())

    def test_the_logo_lands_in_the_corner_it_was_asked_for(self):
        """Corner placement is the whole contract — the prompts leave specific corners clean."""
        w, h = 1280, 720
        for corner, probe in [("bottom-left", (60, h - 60)), ("top-right", (w - 60, 60))]:
            out = _open(brand_overlay.apply(_art(w, h), logo=_mark(), logo_corner=corner))
            assert out.getpixel(probe) != (30, 30, 30), f"nothing was drawn at {corner}"

    def test_every_icon_is_drawn_not_just_the_first(self):
        """A loop that forgets to advance its cursor stacks the icons and still 'works'.

        ⚠️ scrim=False deliberately. With the scrim on, this test PASSED against a cursor that
        never advanced: the scrim is sized from the whole icon block, so it darkened the full
        strip regardless and swamped the pixel count. Measure the icons, not the backdrop.
        """
        red = (255, 0, 0)
        count = lambda im: sum(1 for px in im.getdata() if px == red)  # noqa: E731
        one = count(_open(brand_overlay.apply(
            _art(), icons=[_mark(size=(64, 64))], scrim=False)))
        four = count(_open(brand_overlay.apply(
            _art(), icons=[_mark(size=(64, 64))] * 4, scrim=False)))
        assert one > 0, "no icon was drawn at all"
        assert four >= one * 3.5, (
            f"four icons covered {four}px but one covers {one}px — they were stacked on one spot")

    def test_the_channel_name_is_rendered(self):
        plain = _open(brand_overlay.apply(_art(), logo=_mark()))
        named = _open(brand_overlay.apply(_art(), logo=_mark(), channel_name="EXAMPLE BRAND"))
        assert list(named.getdata()) != list(plain.getdata())


class TestItDegrades:
    """A missing asset must never fail a post — same rule the card path follows."""

    def test_no_assets_at_all_still_returns_the_artwork(self):
        out = brand_overlay.apply(_art())
        assert _open(out).size == (1280, 720)

    def test_a_missing_logo_file_loads_as_none(self, tmp_path):
        assert brand_overlay.load(tmp_path / "nope.png") is None

    def test_a_corrupt_logo_file_loads_as_none(self, tmp_path):
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"not a png")
        assert brand_overlay.load(bad) is None

    def test_none_entries_in_the_icon_list_are_skipped(self):
        out = brand_overlay.apply(_art(), icons=[None, _mark(size=(64, 64)), None])
        assert _open(out).size == (1280, 720)


class TestItIsNotATypesetter:
    def test_it_exposes_no_headline_or_body_parameter(self):
        """Guards the split. Body copy belongs to the model or to layouts.py — if a headline
        argument reappears here, the rejected flat-type card is growing back."""
        import inspect
        params = set(inspect.signature(brand_overlay.apply).parameters)
        for banned in ("headline", "subhead", "kicker", "body", "text"):
            assert banned not in params, f"brand_overlay.apply grew a {banned!r} parameter"


@pytest.mark.parametrize("fmt", [(1280, 720), (1080, 1080), (1080, 1350)])
def test_it_works_at_every_shipped_aspect(fmt):
    w, h = fmt
    out = brand_overlay.apply(_art(w, h), logo=_mark(), channel_name="EXAMPLE BRAND",
                              icons=[_mark(size=(64, 64))] * 3)
    assert _open(out).size == (w, h)
