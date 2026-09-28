"""Post alerts: every successful publish, on any platform, is announced in the brand's Discord
`#posts-<brand>` channel (operator request 2026-09-25: "alerts whenever the agent posts anything
for any brand").

Called from the four platform publishers themselves — `publish_instagram`, `publish_facebook`,
`buffer.create_post` and `youtube.upload_video` — rather than from each pipeline, because those are
the one choke point every path goes through (CLIPNET, Drive-to-social, campaigns, syndication,
manual test posts). A pipeline added later is covered without knowing this module exists.

Channel: `<PREFIX>_POSTS_DISCORD_CHANNEL_ID`. Token: the agent's own DISCORD_BOT_TOKEN.
Best effort, bounded: a missing channel is a silent no-op, and a slow or failing Discord call is
cut off after ALERT_TIMEOUT_S and logged — an alert can never delay or fail the post it describes.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from contextvars import ContextVar

import structlog

log = structlog.get_logger(__name__)
ALERT_TIMEOUT_S = 5.0
_PREVIEW = 180

# ALERTS-ONE-CHANNEL (2026-09-28): a pipeline that sends its OWN richer summary to this channel wraps
# its publisher calls in `suppressed()`, so one publish is one message. CLIPNET does this — its
# summary carries every platform, failures and the Whop submit deadline, which this alert cannot.
_SUPPRESSED: ContextVar[bool] = ContextVar("post_alerts_suppressed", default=False)


@contextlib.contextmanager
def suppressed():
    token = _SUPPRESSED.set(True)
    try:
        yield
    finally:
        _SUPPRESSED.reset(token)


def channel_for(brand_id: str | None) -> str:
    if not brand_id:
        return ""
    from meshpilot.config import brand_env

    # brand_env RAISES for a brand the registry does not know. An alert lookup must never be able
    # to fail the post it describes, so any lookup failure means "no alerts channel".
    try:
        return brand_env("POSTS_DISCORD_CHANNEL_ID", brand_id)
    except Exception:
        return ""


def format_alert(brand_id: str, platform: str, *, url: str | None, ref: str | None,
                 text: str | None) -> str:
    where = url or (f"queued on Buffer · post `{ref}` (link follows once {platform} publishes)" if ref
                    else "published (no link returned)")
    preview = " ".join((text or "").split())
    if len(preview) > _PREVIEW:
        preview = preview[:_PREVIEW - 1] + "…"
    lines = [f"📣 **{brand_id}** posted to **{platform}**", where]
    if preview:
        lines.append(f"> {preview}")
    return "\n".join(lines)


async def announce_post(brand_id: str | None, platform: str, *, url: str | None = None,
                        ref: str | None = None, text: str | None = None, send=None) -> bool:
    """Announce one successful post. Returns True if the alert was sent. Never raises."""
    if _SUPPRESSED.get():
        return False
    return await announce_text(brand_id, format_alert(str(brand_id), platform, url=url, ref=ref, text=text),
                               send=send, platform=platform)


async def announce_text(brand_id: str | None, content: str, *, send=None, platform: str = "") -> bool:
    """Send pre-formatted text to the brand's posts channel. Same bounds as `announce_post`."""
    channel = channel_for(brand_id)
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not channel or not token:
        return False
    if send is None:
        from meshpilot.comms.discord import post_message as send
    try:
        await asyncio.wait_for(send(token=token, channel_id=channel, content=content), timeout=ALERT_TIMEOUT_S)
        return True
    except Exception as exc:  # includes the timeout: the post already succeeded, so just log it
        log.warning("post_alert.failed", brand_id=brand_id, platform=platform, error=str(exc)[:200])
        return False
