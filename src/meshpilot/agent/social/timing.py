"""When to post — the shared, per-platform schedule standard (PLATFORM-TIMING, 2026-09-26).

Each platform's slots live on its `platform_profile` row (`post_times` in `post_tz`, plus
`min_gap_hours`); the `_default` rows hold the researched standard every brand inherits and a brand
row overrides it. Anything that publishes on a clock asks this module whether a platform is due,
so every channel follows one rulebook. Research: docs/plans/2026-09-26-platform-timing.md.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SLOT_WINDOW_MIN = 25
# The researched standard (mirrors the `_default` platform_profile rows). Used only when a profile
# read fails or a row predates the timing columns — a DB hiccup must not silently stop posting.
STANDARD: dict[str, dict] = {
    "instagram": {"post_times": ["09:00", "12:30", "19:30"], "hashtag_max": 5},
    "tiktok":    {"post_times": ["12:00", "16:00", "19:30"], "hashtag_max": 5},
    "youtube":   {"post_times": ["11:00", "15:00", "20:00"], "hashtag_max": 5},
    "facebook":  {"post_times": ["09:00", "15:00", "20:00"], "hashtag_max": 3},
    "x":         {"post_times": ["08:00", "12:00", "21:00"], "hashtag_max": 1},
}


def with_standard(platform: str, prof: dict | None) -> dict:
    """The profile with its timing filled from STANDARD wherever the row leaves it empty."""
    prof = dict(prof or {})
    std = STANDARD.get((platform or "").lower(), {})
    if not prof.get("post_times"):
        prof["post_times"] = std.get("post_times", [])
    if prof.get("hashtag_max") is None:
        prof["hashtag_max"] = std.get("hashtag_max")
    prof["post_tz"] = prof.get("post_tz") or "America/New_York"
    prof["min_gap_hours"] = float(prof.get("min_gap_hours") or 3)
    return prof   # a slot stays open this long, so a 10-minute publisher tick can never miss it


def due_slot(post_times: list[str] | tuple[str, ...], tz: str, now: datetime,
             *, window_min: int = SLOT_WINDOW_MIN) -> str | None:
    """The slot open at `now` ('YYYY-MM-DD HH:MM' in `tz`), or None. `now` must be timezone-aware."""
    local = now.astimezone(ZoneInfo(tz or "America/New_York"))
    for t in post_times or ():
        try:
            hh, mm = (int(x) for x in str(t).split(":", 1))
        except ValueError:
            continue
        start = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if start <= local < start + timedelta(minutes=window_min):
            return start.strftime("%Y-%m-%d %H:%M")
    return None


def gap_ok(last_post_at: datetime | None, now: datetime, min_gap_hours: float) -> bool:
    """True when the platform's previous post is at least `min_gap_hours` old (or there is none)."""
    return last_post_at is None or (now - last_post_at) >= timedelta(hours=float(min_gap_hours or 0))


def spaced(post_times: list[str] | tuple[str, ...], min_gap_hours: float) -> bool:
    """True when consecutive slots in one day are at least `min_gap_hours` apart — a schedule that
    breaks its own gap rule would silently skip slots."""
    mins = sorted(int(h) * 60 + int(m) for h, m in (str(t).split(":", 1) for t in post_times or ()))
    return all(b - a >= min_gap_hours * 60 for a, b in zip(mins, mins[1:]))
