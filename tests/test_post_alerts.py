"""Post alerts: every publish is announced in the brand's #posts channel, and an alert can never
delay or fail the post it describes."""
import asyncio

from meshpilot.comms import post_alerts


def test_alert_names_brand_platform_and_link():
    msg = post_alerts.format_alert("ai_empire", "instagram", url="https://ig/1", ref="m1", text="hello world")
    assert "**ai_empire** posted to **instagram**" in msg and "https://ig/1" in msg and "> hello world" in msg


def test_buffer_alert_without_link_says_queued():
    msg = post_alerts.format_alert("ev", "tiktok", url=None, ref="b123", text="")
    assert "queued on Buffer" in msg and "b123" in msg and ">" not in msg


def test_long_caption_is_trimmed_to_one_line():
    msg = post_alerts.format_alert("x", "x", url="u", ref=None, text="word\n" * 200)
    preview = msg.splitlines()[-1]
    assert preview.startswith("> ") and len(preview) <= post_alerts._PREVIEW + 2 and preview.endswith("…")


async def test_no_channel_is_a_silent_no_op(monkeypatch):
    monkeypatch.setattr(post_alerts, "channel_for", lambda b: "")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    assert await post_alerts.announce_post("ai_empire", "x", send=lambda **k: None) is False


async def test_sends_to_the_brand_channel(monkeypatch):
    sent = {}

    async def send(**k):
        sent.update(k)

    monkeypatch.setattr(post_alerts, "channel_for", lambda b: "555")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    assert await post_alerts.announce_post("ai_empire", "facebook", url="u", send=send) is True
    assert sent["channel_id"] == "555" and "facebook" in sent["content"]


async def test_a_failing_discord_never_raises(monkeypatch):
    async def send(**k):
        raise RuntimeError("discord down")

    monkeypatch.setattr(post_alerts, "channel_for", lambda b: "555")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    assert await post_alerts.announce_post("ai_empire", "x", send=send) is False


async def test_a_hanging_discord_is_cut_off(monkeypatch):
    async def send(**k):
        await asyncio.sleep(60)

    monkeypatch.setattr(post_alerts, "channel_for", lambda b: "555")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setattr(post_alerts, "ALERT_TIMEOUT_S", 0.05)
    assert await post_alerts.announce_post("ai_empire", "x", send=send) is False


async def test_buffer_create_post_announces(monkeypatch):
    from meshpilot.platforms import buffer

    seen = []

    async def _graphql(token, query, variables=None, **k):
        return {"createPost": {"__typename": "PostActionSuccess", "post": {"id": "p9", "status": "sending"}}}

    async def _channel(brand_id, service):
        return "chan-1"

    async def _announce(brand_id, platform, **kw):
        seen.append((brand_id, platform, kw.get("ref")))
        return True

    monkeypatch.setattr(buffer, "_graphql", _graphql)
    monkeypatch.setattr(buffer, "_channel_id_for_service", _channel)
    monkeypatch.setattr(buffer, "_buffer_token", lambda b: "tok")
    monkeypatch.setattr(post_alerts, "announce_post", _announce)
    await buffer.create_post("ai_empire", "tiktok", text="hi", media_url="https://cdn/c.mp4")
    assert seen == [("ai_empire", "tiktok", "p9")]


def test_an_unknown_brand_never_raises_from_the_lookup():
    """brand_env raises KeyError for an unregistered brand; the alert path must swallow it."""
    assert post_alerts.channel_for("no_such_brand_anywhere") == ""


# ── ALERTS-ONE-CHANNEL: one clip publish = one message, in #posts-<brand> ─────────────────────────
async def test_suppressed_blocks_the_per_platform_alert_and_restores_after(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.setattr(post_alerts, "channel_for", lambda b: "555")
    sent = []

    async def send(**k):
        sent.append(k)
    with post_alerts.suppressed():
        assert await post_alerts.announce_post("ai_empire", "x", url="u", send=send) is False
    assert sent == []
    assert await post_alerts.announce_post("ai_empire", "x", url="u", send=send) is True
    assert len(sent) == 1


async def test_clipnet_publishes_quietly_and_sends_its_summary_to_the_posts_channel(monkeypatch):
    """The bug: every platform publisher alerted #posts-<brand> AND ClipNet sent its own
    "📣 Posted" to #clip-queue. Now the publisher's alert is suppressed for ClipNet, and ClipNet's
    summary lands in the posts channel — nothing reaches #clip-queue for a successful post."""
    from meshpilot.agent.clipnet import notify as clip_notify
    from meshpilot.agent.clipnet import publish

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.setattr(post_alerts, "channel_for", lambda b: "POSTS")
    monkeypatch.setattr(clip_notify, "channel_for", lambda b: "CLIPQ")
    sent = []

    async def fake_send(*, token, channel_id, content):
        sent.append(channel_id)
    import meshpilot.comms.discord as discord
    monkeypatch.setattr(discord, "post_message", fake_send)

    async def publisher(p, brand_id, clip, campaign):      # a real publisher alerts on success
        await post_alerts.announce_post(brand_id, p, url="https://x/1")
        return "ext-1", "https://x/1"
    assert await publish._post_quietly(publisher, "x", "ai_empire", {}, None) == ("ext-1", "https://x/1")
    await publish._announce("ai_empire", "📣 **Posted:** hook")
    assert sent == ["POSTS"]
