"""PLATFORM-TIMING: per-platform best-time slots, a minimum gap, and per-platform captions."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from meshpilot.agent.clipnet import publish
from meshpilot.agent.clipnet.campaigns import Campaign
from meshpilot.agent.social import timing

NY = "America/New_York"


def _at(hh, mm, day=28):   # 2026-09-28 is a Monday; New York is UTC-4 in September
    return datetime(2026, 9, day, hh + 4, mm, tzinfo=UTC)


def test_a_slot_is_open_for_its_window_only():
    times = ["09:00", "19:30"]
    assert timing.due_slot(times, NY, _at(9, 0)) == "2026-09-28 09:00"
    assert timing.due_slot(times, NY, _at(9, 24)) == "2026-09-28 09:00"
    assert timing.due_slot(times, NY, _at(9, 25)) is None
    assert timing.due_slot(times, NY, _at(8, 59)) is None
    assert timing.due_slot(times, NY, _at(19, 40)) == "2026-09-28 19:30"


def test_the_gap_rule():
    now = _at(15, 0)
    assert timing.gap_ok(None, now, 3)
    assert timing.gap_ok(now - timedelta(hours=3), now, 3)
    assert not timing.gap_ok(now - timedelta(hours=2, minutes=59), now, 3)


@pytest.mark.parametrize("platform", sorted(timing.STANDARD))
def test_every_standard_schedule_keeps_three_hours_between_posts(platform):
    """Operator rule 2026-09-26: at least 3-4 hours between posts on a platform."""
    assert timing.spaced(timing.STANDARD[platform]["post_times"], 3)
    assert len(timing.STANDARD[platform]["post_times"]) == 3


def test_a_missing_profile_falls_back_to_the_standard():
    prof = timing.with_standard("tiktok", {})
    assert prof["post_times"] == ["12:00", "16:00", "19:30"] and prof["min_gap_hours"] == 3
    assert timing.with_standard("tiktok", {"post_times": ["10:00"]})["post_times"] == ["10:00"]


# ── per-platform captions ─────────────────────────────────────────────────────────────────────────
ORGANIC = Campaign(slug="organic-ai-empire", brand_ids=("ai_empire",), subject="AI", required_hashtags=(),
                   allowed_sources=(), submit_platforms=(), disclosure=None, submit_window_min=10,
                   max_clips_per_day=6, active=True, kind="organic",
                   discovery={"hashtags": ["#AI", "#AITools", "#ArtificialIntelligence", "#AIAgents", "#TechPodcast"],
                              "youtube_category": "28"})
PAID = Campaign(slug="lovable", brand_ids=("ai_empire",), subject="Lovable", required_hashtags=("#LovablePartner",),
                allowed_sources=(), submit_platforms=("instagram",), disclosure=None, submit_window_min=10,
                max_clips_per_day=5, active=True)


def _clip(caption="Seven Grok agents run his whole week, from inbox to invoices."):
    return {"id": "c", "hook": "7 agents I use daily", "caption": caption,
            "stage_outputs": {"source": {"key": "youtube:abcdefghijk"},
                              "creator": {"channel_id": "UC1", "name": "How I AI"}}}


def _tags(text):
    return [w for w in text.split() if w.startswith("#")]


def test_instagram_gets_the_body_first_then_credit_then_at_most_five_tags():
    cap = publish.platform_caption(_clip(), ORGANIC, "instagram", {"hashtag_max": 5, "max_chars": 2200})
    assert cap.startswith("Seven Grok agents")
    assert "Credit: How I AI" in cap and len(_tags(cap)) == 5
    assert cap.index("Credit") < cap.index("#AI")


def test_facebook_caps_tags_lower_and_x_keeps_one():
    assert len(_tags(publish.platform_caption(_clip(), ORGANIC, "facebook", {"hashtag_max": 3}))) == 3
    assert len(_tags(publish.platform_caption(_clip(), ORGANIC, "x", {"hashtag_max": 1}))) == 1


def test_youtube_always_carries_shorts_first():
    tags = _tags(publish.platform_caption(_clip(), ORGANIC, "youtube", {"hashtag_max": 5}))
    assert tags[0] == "#Shorts" and len(tags) == 5


def test_x_fits_280_counting_links_as_23():
    long = "word " * 120
    cap = publish.platform_caption(_clip(long), ORGANIC, "x", {"hashtag_max": 1})
    link = "https://youtu.be/abcdefghijk"
    assert len(cap) - len(link) + 23 <= 280 and cap.split("\n")[0].endswith("…")


def test_a_paid_campaigns_required_tag_survives_any_cap():
    cap = publish.platform_caption(_clip("Demo, don't memo. #LovablePartner"), PAID, "x", {"hashtag_max": 0})
    assert _tags(cap) == ["#LovablePartner"] and "Credit" not in cap


# ── the slot publisher ────────────────────────────────────────────────────────────────────────────
async def test_publish_due_is_a_no_op_while_the_kill_switch_is_off(monkeypatch):
    monkeypatch.setattr(publish, "enabled", lambda: False)
    assert "skipped" in await publish.publish_due("ai_empire", engine=object())


async def test_nothing_is_due_outside_every_slot(monkeypatch):
    monkeypatch.setattr(publish, "enabled", lambda: True)

    async def prof(brand, p, engine=None):
        return {"post_times": ["09:00"], "post_tz": NY, "min_gap_hours": 3}
    out = await publish.publish_due("ai_empire", engine=object(), platforms=("instagram", "x"),
                                    profile_fn=prof, now=_at(14, 0))
    assert out == {}


def test_the_2026_09_28_x_rejection_now_fits_under_every_counting_rule():
    """Buffer refused 285 raw / 281 X-weighted chars: the 🎥 in the credit weighs 2 on X, and the old
    count treated it as 1 and the link as exactly 23."""
    body = ("Jessie reveals her ex tried to get her served with a restraining order while she was recording "
            "Call Her Daddy. She found out about the divorce filing through a TMZ text before she could even "
            "file herself.")
    clip = _clip(body)
    clip["stage_outputs"] = {"source": {"key": "youtube:5g9Ou-mmnK0"},                 # the real clip's source
                             "creator": {"channel_id": "UC2", "name": "JUST TRISH PODCAST"}}
    old_count = len(body) + 1 + len(publish.credit_line(publish.creator_of(clip), "youtube:5g9Ou-mmnK0")) + 1 + 8
    assert old_count > 280                                   # the untrimmed caption really was too long
    cap = publish.platform_caption(clip, ORGANIC, "x", {"hashtag_max": 1})
    assert publish._x_len(cap) <= 280
    assert len(cap) <= 280                                   # also fits if Buffer counts the raw link
    assert cap.split("\n")[0].endswith("…") and "Credit" in cap


def test_x_len_weighs_emoji_double_and_links_at_least_23():
    assert publish._x_len("abc") == 3
    assert publish._x_len("🎥") == 2
    assert publish._x_len("https://t.co/x") == 23                      # short link still costs 23
    assert publish._x_len("see https://youtu.be/5g9Ou-mmnK0") == 4 + 28  # long link costs its real length


def test_a_short_x_caption_is_untouched():
    cap = publish.platform_caption(_clip("Short and sweet."), ORGANIC, "x", {"hashtag_max": 1})
    assert cap.startswith("Short and sweet.") and "…" not in cap
