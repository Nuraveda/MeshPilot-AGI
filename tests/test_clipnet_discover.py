"""CLIPNET-DISCOVER: trending videos from independent creators, credited (spec 2026-09-26)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from meshpilot.agent.clipnet import discover as d
from meshpilot.agent.clipnet import publish
from meshpilot.agent.clipnet.campaigns import Campaign

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
CFG = {"min_subs": 10_000, "max_subs": 1_000_000, "min_minutes": 15, "max_minutes": 90}


def _video(vid="v1", ch="c1", *, views=50_000, dur="PT1H2M", cat="22", lang="en", live="none",
           published="2026-09-24T12:00:00Z"):
    return {"id": vid, "snippet": {"channelId": ch, "title": "Episode", "categoryId": cat,
                                   "defaultAudioLanguage": lang, "liveBroadcastContent": live,
                                   "publishedAt": published},
            "statistics": {"viewCount": str(views)}, "contentDetails": {"duration": dur}}


def _channel(title="Jane Builds", subs=120_000, hidden=False):
    st = {"hiddenSubscriberCount": hidden}
    if not hidden:
        st["subscriberCount"] = str(subs)
    return {"snippet": {"title": title}, "statistics": st}


def _why(video=None, channel=None, **kw):
    kw = {"blocked": set(), "recent_creators": set(), "known_videos": set(), **kw}
    return d.eligible(video or _video(), channel or _channel(), CFG, **kw)


def test_an_independent_creators_podcast_is_a_candidate():
    assert _why() is None


@pytest.mark.parametrize("title", ["Warner Records", "Some Artist - Topic", "Daily News Network",
                                   "BigShow Official", "Acme Studios", "LateNight TV"])
def test_production_channels_are_excluded(title):
    assert "production channel" in _why(channel=_channel(title=title))


def test_music_is_excluded_even_from_an_indie_creator():
    """Content ID claims the song, whoever uploaded it."""
    assert "music" in _why(video=_video(cat="10"))


@pytest.mark.parametrize("subs,ok", [(9_999, False), (10_000, True), (1_000_000, True), (1_000_001, False)])
def test_only_the_independent_creator_size_range(subs, ok):
    assert (_why(channel=_channel(subs=subs)) is None) is ok


def test_hidden_subscriber_counts_fail_closed():
    assert "hidden" in _why(channel=_channel(hidden=True))


def test_blocked_recent_known_live_foreign_and_wrong_length_are_all_rejected():
    assert "blocked" in _why(blocked={"c1"})
    assert "last 14 days" in _why(recent_creators={"c1"})
    assert "already queued" in _why(known_videos={"v1"})
    assert "live" in _why(video=_video(live="live"))
    assert "not English" in _why(video=_video(lang="ml"))
    assert "outside range" in _why(video=_video(dur="PT8M"))
    assert "outside range" in _why(video=_video(dur="PT2H30M"))


def test_iso_durations():
    assert d.iso_minutes("PT1H2M30S") == pytest.approx(62.5)
    assert d.iso_minutes("P1DT1H") == 1500
    assert d.iso_minutes("garbage") == 0.0


def test_rank_orders_by_views_per_day_and_takes_one_video_per_creator():
    fresh_hit = _video("a", "c1", views=30_000, published="2026-09-25T12:00:00Z")   # 30k/day
    same_creator = _video("b", "c1", views=90_000, published="2026-09-23T12:00:00Z")  # 30k/day, same channel
    older = _video("c", "c2", views=100_000, published="2026-09-06T12:00:00Z")       # 5k/day
    picked = d.rank([older, same_creator, fresh_hit], NOW, 3)
    assert [v["snippet"]["channelId"] for v in picked] == ["c1", "c2"]


class _Resp:
    def __init__(self, body):
        self._b = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._b


class _Http:
    def __init__(self):
        self.calls = []

    async def get(self, url, params=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[1], params))
        path = url.rsplit("/", 1)[1]
        if path == "search":
            return _Resp({"items": [{"id": {"videoId": "v1"}}, {"id": {"videoId": "v2"}}]})
        if path == "videos":
            return _Resp({"items": [_video("v1", "c1"), _video("v2", "c2")]})
        return _Resp({"items": [{"id": "c1", **_channel()}, {"id": "c2", **_channel("Other Pod")}]})


async def test_search_hydrates_videos_and_channels_with_the_niche_filters():
    http = _Http()
    videos, channels = await d.search(http, "k", {"queries": ["ai podcast"], "lookback_days": 30,
                                                  "video_duration": "long"}, NOW)
    assert [v["id"] for v in videos] == ["v1", "v2"] and set(channels) == {"c1", "c2"}
    s = http.calls[0][1]
    assert s["order"] == "viewCount" and s["videoDuration"] == "long" and s["regionCode"] == "US"
    assert s["publishedAfter"] == "2026-08-27T12:00:00Z"


# ── publishing organic clips ──────────────────────────────────────────────────────────────────────
ORGANIC = Campaign(slug="organic-ai-empire", brand_ids=("ai_empire",), subject="AI and startups",
                   required_hashtags=(), allowed_sources=(), submit_platforms=(), disclosure=None,
                   submit_window_min=10, max_clips_per_day=6, active=True, kind="organic",
                   discovery={"queries": ["x"]})


def _clip(creator=True):
    so = {"source": {"key": "youtube:abcdefghijk"}, "picks": [{"start": 10.0, "text": "anything"}]}
    if creator:
        so["creator"] = {"channel_id": "UC1", "name": "Jane Builds", "url": "https://www.youtube.com/channel/UC1"}
    return {"id": "x", "start_s": 10.0, "caption": "A founder on shipping fast.", "stage_outputs": so}


def test_an_organic_clip_passes_on_provenance_not_on_a_campaign_list():
    assert publish.gate(_clip(), ORGANIC) is None


def test_an_organic_clip_without_a_known_creator_is_blocked():
    assert "no discovered creator" in publish.gate(_clip(creator=False), ORGANIC)


def test_every_organic_caption_credits_the_creator_and_links_the_full_video():
    cap = publish.caption_for(_clip(), ORGANIC)
    assert cap.startswith("A founder on shipping fast.")
    assert "Credit: Jane Builds" in cap and "https://youtu.be/abcdefghijk" in cap


def test_paid_campaign_captions_are_left_alone():
    paid = Campaign(slug="lovable", brand_ids=("ai_empire",), subject="Lovable", required_hashtags=(),
                    allowed_sources=(), submit_platforms=(), disclosure=None, submit_window_min=10,
                    max_clips_per_day=5, active=True)
    assert publish.caption_for(_clip(), paid) == "A founder on shipping fast."


def test_non_latin_titles_are_rejected_even_without_a_language_tag():
    """Measured: Kannada and Korean titles arrived with no defaultAudioLanguage."""
    v = _video(); v["snippet"]["title"] = "ಪಲ್ಲವಿ ಹೇಳಿದ ಕೊನೆಯ ಮಾತಿದು"; del v["snippet"]["defaultAudioLanguage"]
    assert "not English" in _why(video=v)
    assert "not English" in _why(channel=_channel(title="한국e스포츠협회"))
    assert d.mostly_non_latin("Café Déjà vu") is False


def test_the_screen_keeps_only_ids_it_was_given_and_fails_closed():
    assert d.parse_screen('{"keep": ["a", "zzz", "a"]}', {"a", "b"}) == ["a"]
    assert d.parse_screen("not json", {"a"}) == []


async def test_screen_failure_queues_nothing(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    class Boom:
        async def post(self, *a, **k):
            raise RuntimeError("down")
    v = _video()
    assert await d.screen(Boom(), [v], {"c1": _channel()}, "AI") == []


@pytest.mark.parametrize("title", ["LET'S GROOVE TONIGHT (Kronk Dancing Meme) - VRChat", "Lyrics Reaction",
                                   "Minecraft but every block is a song", "Cover battle ft. Someone"])
def test_music_led_titles_are_rejected_by_code(title):
    v = _video(); v["snippet"]["title"] = title
    assert "music in the title" in _why(video=v)


def test_ordinary_titles_with_music_lookalikes_pass():
    v = _video(); v["snippet"]["title"] = "We Are The DUMBEST Builders..."
    assert _why(video=v) is None


@pytest.mark.parametrize("env, expected", [(None, "openai/gpt-6-luna-pro"),
                                           ("anthropic/claude-sonnet-5", "anthropic/claude-sonnet-5")])
async def test_screen_model_defaults_to_luna_pro_and_honours_the_override(monkeypatch, env, expected):
    """ROUTER-LUNA: the screen used a hardcoded Sonnet the router could not reach."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    if env:
        monkeypatch.setenv("CLIPNET_SCREEN_MODEL", env)
    else:
        monkeypatch.delenv("CLIPNET_SCREEN_MODEL", raising=False)
    sent = {}

    class R:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": '{"keep": []}'}}]}

    class H:
        async def post(self, url, **kw):
            sent.update(kw["json"])
            return R()
    await d.screen(H(), [_video()], {"c1": _channel()}, "AI")
    assert sent["model"] == expected
