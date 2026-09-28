"""CLIPNET auto publishing (A3/A4): the gate fails closed, the kill switch holds, and the worker
execution body carries the per-job overrides (spec § 5–6)."""
from types import SimpleNamespace

from meshpilot.agent.clipnet import publish, worker_client
from meshpilot.agent.clipnet.campaigns import Campaign

CAMPAIGN = Campaign(slug="lovable", brand_ids=("ai_empire",), subject="Lovable or its CEO Anton Osika",
                    required_hashtags=("#LovablePartner",), allowed_sources=("youtube:9FGMhz-e97k",),
                    submit_platforms=("instagram", "tiktok", "youtube"), disclosure="paid_promotion",
                    submit_window_min=10, max_clips_per_day=5, active=True)


def _clip(**over):
    base = {"id": "c1", "start_s": 316.2, "hook": "The 99 percent can build",
            "caption": "Anton on who gets to build now.\n\n#LovablePartner",
            "stage_outputs": {"source": {"key": "youtube:9FGMhz-e97k"},
                              "picks": [{"start": 316.2, "text": "With Lovable anyone can build software."}]}}
    base.update(over)
    return base


def test_a_compliant_clip_passes():
    assert publish.gate(_clip(), CAMPAIGN) is None


def test_missing_required_hashtag_blocks():
    assert "missing required hashtags" in publish.gate(_clip(caption="No tag here."), CAMPAIGN)


def test_hashtag_match_is_case_insensitive():
    assert publish.gate(_clip(caption="ok #lovablepartner"), CAMPAIGN) is None


def test_a_source_not_on_the_allow_list_blocks():
    so = {"source": {"key": "youtube:SOMEOTHERID"}, "picks": _clip()["stage_outputs"]["picks"]}
    assert "not on the campaign's allowed list" in publish.gate(_clip(stage_outputs=so), CAMPAIGN)


def test_an_unknown_source_blocks():
    so = {"picks": _clip()["stage_outputs"]["picks"]}
    assert "not on the campaign's allowed list" in publish.gate(_clip(stage_outputs=so), CAMPAIGN)


def test_off_subject_clip_text_blocks():
    so = {"source": {"key": "youtube:9FGMhz-e97k"}, "picks": [{"start": 316.2, "text": "Talking about lunch."}]}
    assert "never mentions the campaign subject" in publish.gate(_clip(stage_outputs=so), CAMPAIGN)


def _guest_so(host_ranges, guest="Anton Osika"):
    return {"source": {"key": "youtube:9FGMhz-e97k"},
            "picks": [{"start": 316.2, "text": "We built this to help people who could never build."}],
            "structure": {"host": "Lenny", "guest": guest, "host_ranges": host_ranges}}


def test_the_subject_speaking_as_guest_passes_without_saying_the_name():
    """Measured 2026-09-25: Anton explained Lovable's mission for a minute without saying 'Lovable'."""
    clip = _clip(stage_outputs=_guest_so([[300.0, 320.0]]), end_s=376.2)   # host holds 3.8 s of 60
    assert publish.gate(clip, CAMPAIGN) is None


def test_the_host_talking_about_the_guest_still_blocks():
    clip = _clip(stage_outputs=_guest_so([[316.2, 356.2]]), end_s=376.2)   # host holds 40 s of 60
    assert "never mentions the campaign subject" in publish.gate(clip, CAMPAIGN)


def test_a_guest_who_is_not_the_subject_does_not_pass():
    clip = _clip(stage_outputs=_guest_so([], guest="Someone Else"), end_s=376.2)
    assert "never mentions the campaign subject" in publish.gate(clip, CAMPAIGN)


def test_clip_text_is_matched_to_the_pick_by_start_time():
    so = {"picks": [{"start": 10.0, "text": "a"}, {"start": 316.3, "text": "b"}]}
    assert publish.clip_text(so, 316.2) == "b"
    assert publish.clip_text(so, 99.0) == ""


def test_subject_terms_drop_short_and_stop_words():
    assert publish.subject_terms("Lovable or its CEO Anton Osika") == {"lovable", "anton", "osika"}


def test_youtube_title_carries_the_hashtag_and_fits_100_chars():
    t = publish.youtube_title("x" * 120, ("#LovablePartner",))
    assert len(t) == 100
    assert publish.youtube_title("Demo, don't memo", ("#LovablePartner",)) == "Demo, don't memo #LovablePartner #shorts"


async def test_publish_is_a_no_op_while_the_kill_switch_is_off(monkeypatch):
    monkeypatch.setattr(publish, "enabled", lambda: False)
    out = await publish.publish_next("ai_empire", engine=object())
    assert "skipped" in out


def test_enabled_needs_both_switches(monkeypatch):
    import meshpilot.config as cfg

    for clipnet, pub, want in ((True, True, True), (True, False, False), (False, True, False)):
        monkeypatch.setattr(cfg, "settings", lambda c=clipnet, p=pub: SimpleNamespace(
            agent_clipnet_enabled=c, agent_publish_enabled=p))
        assert publish.enabled() is want


def test_routing_is_standard_buffer_for_x_tiktok_youtube_meta_for_the_rest():
    assert set(publish.BUFFER_SERVICE) == {"x", "tiktok", "youtube"}
    assert set(publish.PLATFORMS) - set(publish.BUFFER_SERVICE) == {"instagram", "facebook"}


def test_worker_execution_body_overrides_job_and_bucket():
    body = worker_client.run_body("job-1", "aie-media", 2)
    env = {e["name"]: e["value"] for e in body["overrides"]["containerOverrides"][0]["env"]}
    assert env == {"JOB_ID": "job-1", "BUCKET": "aie-media", "N_CLIPS": "2"}


def test_start_execution_raises_on_a_refusal():
    class _S:
        def post(self, *a, **k):
            return SimpleNamespace(status_code=403, text="denied", json=lambda: {})

    import pytest

    with pytest.raises(RuntimeError, match="403"):
        worker_client.start_execution("job-1", "aie-media", session=_S())


# ── CLIPNET-LEARN L1: every clip becomes a brand-memory episode ─────────────────────────────────

def _learn_clip():
    return {"id": "clip-1", "job_id": "job-1", "start_s": 316.2, "end_s": 373.9, "hook": "The 99 percent can build",
            "caption": "x #LovablePartner",
            "stage_outputs": {"source": {"key": "youtube:9FGMhz-e97k"},
                              "picks": [{"start": 316.2, "jev": 2.4, "why": "self-contained guest answer",
                                         "text": "With Lovable the 99 percent can finally build."}]}}


def test_a_published_episode_carries_hook_links_and_pick_reason():
    content, meta = publish.episode_for("ai_empire", _learn_clip(), "lovable", results={
        "instagram": {"status": "posted", "url": "https://ig/1", "error": None},
        "tiktok": {"status": "failed", "url": None, "error": "boom"}})
    assert "Posted clip (lovable) to instagram" in content and "self-contained guest answer" in content
    assert meta["outcome"] == "posted" and meta["links"] == {"instagram": "https://ig/1"}
    assert meta["failed"] == {"tiktok": "boom"} and meta["jev"] == 2.4 and meta["duration_s"] == 57.7
    assert meta["capability"] == "clipnet" and meta["clip_id"] == "clip-1" and meta["source"] == "youtube:9FGMhz-e97k"


def test_a_blocked_clip_is_remembered_with_its_reason():
    content, meta = publish.episode_for("ai_empire", _learn_clip(), "lovable", blocked_reason="missing #LovablePartner")
    assert content.startswith("Clip BLOCKED before posting (lovable)") and "missing #LovablePartner" in content
    assert meta["outcome"] == "blocked" and meta["links"] == {}


def test_pick_for_matches_on_start_time():
    so = {"picks": [{"start": 10.0, "why": "a"}, {"start": 316.25, "why": "b"}]}
    assert publish.pick_for(so, 316.2)["why"] == "b" and publish.pick_for(so, 99.0) == {}


async def test_a_memory_failure_never_raises(monkeypatch):
    import meshpilot.agent.memory.store as store

    async def boom(*a, **k):
        raise RuntimeError("embeddings down")

    monkeypatch.setattr(store, "remember", boom)
    assert await publish._remember("ai_empire", "c", {}) is False
