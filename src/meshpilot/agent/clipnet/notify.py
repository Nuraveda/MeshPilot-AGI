"""CLIPNET Discord notices to the brand's #clip-queue channel: blocked clips and creator notices.

The "📣 Posted" summary built by `published_message` goes to #posts-<brand> instead (see
`publish._announce`), so posting alerts land in exactly one channel.

The channel is `<PREFIX>_CLIPNET_DISCORD_CHANNEL_ID`; the bot token is the agent's own
DISCORD_BOT_TOKEN (agent infrastructure, like the Discord bridge — not a brand credential).
Best effort: a notice that fails to send never undoes a post or a job transition.
"""
from __future__ import annotations

import os

import structlog

log = structlog.get_logger(__name__)


def channel_for(brand_id: str) -> str:
    from meshpilot.config import brand_env

    return brand_env("CLIPNET_DISCORD_CHANNEL_ID", brand_id)


async def notify(brand_id: str, content: str, *, send=None) -> bool:
    channel = channel_for(brand_id)
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not channel or not token:
        log.info("clipnet.notify_skipped", brand_id=brand_id, has_channel=bool(channel), has_token=bool(token))
        return False
    if send is None:
        from meshpilot.comms.discord import post_message as send
    try:
        await send(token=token, channel_id=channel, content=content)
        return True
    except Exception as exc:
        log.warning("clipnet.notify_failed", brand_id=brand_id, error=str(exc)[:200])
        return False


def published_message(hook: str, results: dict[str, dict], submit_platforms: tuple[str, ...],
                      submit_by: str) -> str:
    lines = [f"📣 **Posted:** {hook}"]
    for p, r in results.items():
        mark = "✅" if r["status"] == "posted" else "❌"
        lines.append(f"{mark} {p}: {r['url'] or r['error'] or 'no link yet'}")
    submit = [p for p in submit_platforms if results.get(p, {}).get("url")]
    if submit:
        lines.append(f"⏱️ **Submit to Whop by {submit_by}**: {', '.join(submit)}")
    return "\n".join(lines)
