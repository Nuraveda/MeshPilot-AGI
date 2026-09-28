"""Brand overlay for FINISHED artwork.

The model renders the whole thumbnail now, headline typography included — nano-banana-pro spells
display copy correctly, and a side-by-side against the Pillow card was decided on the generated
one. What the model still cannot do is reproduce a REAL mark: it approximates a logo, and an
approximated logo is worse than none. So this module composites only the things that must be
exact — the brand mark, the channel name beside it, and the social icon row — onto an image that
already carries its own copy.

Deliberately NOT a layout engine. It does not wrap, fit or flow text, because there is no longer
any body copy to place; `layouts.py` still owns the flat-card path for posts that have no
generated artwork. Keeping the two apart is what stops this from growing back into the compositor
whose output was rejected.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from meshpilot.media.render.card import _font

Corner = str  # "bottom-left" | "bottom-right" | "top-left" | "top-right"


def _scrim(img: Image.Image, box: tuple[int, int, int, int], strength: int = 110) -> None:
    """Soften the artwork under a mark so it stays legible.

    A generated thumbnail is deliberately busy — embers, explosions, blown highlights — and a mark
    dropped straight onto that disappears. The scrim is a rounded, feathered darkening rather than
    a solid plate so it reads as part of the art.
    """
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return
    pad = max(8, (y1 - y0) // 4)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        (x0 - pad, y0 - pad, x1 + pad, y1 + pad),
        radius=max(12, (y1 - y0) // 3), fill=(0, 0, 0, strength))
    img.alpha_composite(layer)


def _anchor(corner: Corner, w: int, h: int, bw: int, bh: int, margin: int) -> tuple[int, int]:
    x = margin if corner.endswith("left") else w - margin - bw
    y = margin if corner.startswith("top") else h - margin - bh
    return x, y


def apply(
    image: bytes,
    *,
    logo: Image.Image | None = None,
    channel_name: str = "",
    icons: list[Image.Image] | None = None,
    logo_corner: Corner = "bottom-left",
    icons_corner: Corner = "bottom-right",
    scrim: bool = True,
) -> bytes:
    """Paste the brand lockup and social icons onto finished artwork. Returns PNG bytes.

    Every element is optional and a missing one is simply skipped — a thumbnail must still ship
    when a brand has no icon set, exactly as the card path degrades without a logo.
    """
    img = Image.open(BytesIO(image)).convert("RGBA")
    w, h = img.size
    margin = int(w * 0.025)
    icons = [i for i in (icons or []) if i is not None]

    if logo is not None or channel_name:
        mark_h = int(h * 0.11)
        mark_w = 0
        mark = None
        if logo is not None:
            mark = logo.convert("RGBA").copy()
            mark.thumbnail((mark_h * 3, mark_h), Image.LANCZOS)
            mark_w = mark.width
        gap = int(mark_h * 0.32) if (mark is not None and channel_name) else 0
        font: ImageFont.FreeTypeFont | None = None
        text_w = text_h = 0
        if channel_name:
            font = _font(True, int(mark_h * 0.46))
            bbox = ImageDraw.Draw(img).textbbox((0, 0), channel_name, font=font)
            text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        block_w, block_h = mark_w + gap + text_w, max(mark_h if mark else 0, text_h)
        x, y = _anchor(logo_corner, w, h, block_w, block_h, margin)
        if scrim:
            _scrim(img, (x, y, x + block_w, y + block_h))
        if mark is not None:
            img.alpha_composite(mark, (x, y + (block_h - mark.height) // 2))
        if channel_name and font is not None:
            d = ImageDraw.Draw(img)
            ty = y + (block_h - text_h) // 2
            d.text((x + mark_w + gap, ty), channel_name, font=font,
                   fill=(255, 255, 255, 255), stroke_width=max(2, mark_h // 24),
                   stroke_fill=(0, 0, 0, 220))

    if icons:
        size = int(h * 0.075)
        gap = int(size * 0.35)
        block_w = len(icons) * size + (len(icons) - 1) * gap
        x, y = _anchor(icons_corner, w, h, block_w, size, margin)
        if scrim:
            _scrim(img, (x, y, x + block_w, y + size))
        for ic in icons:
            tile = ic.convert("RGBA").copy()
            tile.thumbnail((size, size), Image.LANCZOS)
            img.alpha_composite(tile, (x, y + (size - tile.height) // 2))
            x += size + gap

    buf = BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def load(path: str | Path) -> Image.Image | None:
    """Load a mark from disk, or None when it is missing — a missing asset must not fail a post."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return Image.open(p).convert("RGBA")
    except OSError:
        return None
