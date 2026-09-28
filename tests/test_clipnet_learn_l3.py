"""CLIPNET-LEARN L3: the clip channels score their own results, learn, and tune their discovery."""
from __future__ import annotations

import pytest

from meshpilot.agent.clipnet import learn


def _clip(cid, views, *, creator="Jane", ch="UC1", query="ai podcast", hook="h", seconds=45):
    return {"clip_id": cid, "hook": hook, "creator": creator, "channel_id": ch, "query": query,
            "seconds": seconds, "views": views}


def test_an_all_zero_platform_is_unmeasured_not_a_failure():
    """Measured 2026-09-27: TikTok and X read 0 on every clip. They must not drag scores to zero."""
    clips = [_clip("a", {"instagram": 200, "tiktok": 0}), _clip("b", {"instagram": 100, "tiktok": 0})]
    assert learn.measured_platforms(clips) == {"instagram"}
    s = {c["clip_id"]: c["score"] for c in learn.score(clips)}
    assert s == {"a": pytest.approx(200 / 150, abs=1e-3), "b": pytest.approx(100 / 150, abs=1e-3)}


def test_scores_average_across_measured_platforms_against_the_brands_own_median():
    clips = [_clip("a", {"instagram": 100, "youtube": 90}), _clip("b", {"instagram": 100, "youtube": 10}),
             _clip("c", {"instagram": 100, "youtube": 50})]
    s = {c["clip_id"]: c["score"] for c in learn.score(clips)}
    assert s["a"] == pytest.approx((1 + 90 / 50) / 2, abs=1e-3) and s["c"] == pytest.approx(1.0)


def test_creator_boosts_need_two_clips_and_stay_bounded():
    scored = [{"channel_id": "hot", "score": 9.0}, {"channel_id": "hot", "score": 7.0},
              {"channel_id": "cold", "score": 0.1}, {"channel_id": "cold", "score": 0.1},
              {"channel_id": "once", "score": 5.0}]
    m = learn.creator_multipliers(scored)
    assert m == {"hot": 2.0, "cold": 0.5}           # 'once' has too little data to move


def test_only_a_consistent_flop_is_auto_blocked():
    flop = [{"channel_id": "x", "creator": "Flop", "score": 0.05}] * 4
    few = [{"channel_id": "y", "creator": "New", "score": 0.0}] * 3
    fine = [{"channel_id": "z", "creator": "Ok", "score": 0.9}] * 5
    assert learn.auto_block_candidates(flop + few + fine) == [("x", "Flop", 0.05)]


def test_lessons_are_parsed_bounded_and_keyed_as_clip_lessons():
    raw = 'x [{"key": "Name The Tool!", "content": "Hooks naming a tool beat abstract ones.", "importance": 3}] y'
    out = learn.parse_lessons(raw)
    assert out == [{"key": "clip:lesson:name-the-tool", "content": "Hooks naming a tool beat abstract ones.",
                    "importance": 1.0}]
    assert learn.parse_lessons("nothing") == []


async def test_no_lesson_is_written_below_the_sample_threshold(monkeypatch):
    async def few(brand_id, engine=None):
        return [{"clip_id": str(i), "score": 1.0, "hook": "h", "seconds": 40} for i in range(5)]
    monkeypatch.setattr(learn, "scored_clips", few)
    wrote = []

    async def remember(*a, **k):
        wrote.append(a)
    out = await learn.learn("ai_empire", engine=object(), remember_fn=remember,
                            complete=lambda p: pytest.fail("the model must not be asked"))
    assert out["need"] == learn.MIN_MEASURED and wrote == []


def test_query_stats_and_bounded_plan():
    scored = [{"query": "bad q", "score": 0.1}] * 3 + [{"query": "good q", "score": 2.0}] * 3
    qs = ["bad q", "good q", "fresh q"]
    stats = learn.query_stats(scored, qs)
    assert stats["bad q"] == {"n": 3, "mean": 0.1} and stats["fresh q"] == {"n": 0, "mean": None}
    new, retired, added = learn.plan_queries(qs, stats, ["ai agent founder interview", "x", "good q", "a" * 80])
    assert retired == ["bad q"] and added == ["ai agent founder interview"]
    assert new == ["good q", "fresh q", "ai agent founder interview"]


def test_the_plan_never_strips_a_niche_below_the_floor_or_past_the_cap():
    qs = ["a b", "c d", "e f"]
    stats = {q: {"n": 5, "mean": 0.0} for q in qs}
    new, retired, _ = learn.plan_queries(qs, stats, [])
    assert new == qs and retired == []        # nothing to replace them with → all three stay
    many = [f"topic {i} podcast" for i in range(20)]
    new, _, added = learn.plan_queries(["x y"], {"x y": {"n": 0, "mean": None}}, many)
    assert len(new) == learn.MAX_QUERIES


def test_clips_from_rows_joins_platform_readings_per_clip():
    so = {"creator": {"name": "How I AI", "channel_id": "UC9"}, "discovered": {"query": "ai podcast"}}
    rows = [{"clip_id": "c", "hook": "h", "start_s": 10, "end_s": 55, "campaign": "organic-ai-empire",
             "stage_outputs": so, "platform": "instagram", "video_views": 120, "impressions": 130},
            {"clip_id": "c", "hook": "h", "start_s": 10, "end_s": 55, "campaign": "organic-ai-empire",
             "stage_outputs": so, "platform": "x", "video_views": None, "impressions": 40}]
    (c,) = learn.clips_from_rows(rows)
    assert c["views"] == {"instagram": 120, "x": 40} and c["creator"] == "How I AI" and c["seconds"] == 45
    assert c["query"] == "ai podcast"
