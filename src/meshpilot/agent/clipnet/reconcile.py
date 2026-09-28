"""CLIPNET reservation reconcile: a `reserved` ledger row whose publish never settled.

A row is RESERVED before each platform call and settled to `posted`/`failed` after it. If the process
dies in between, the row stays `reserved` forever. Measured 2026-09-28 00:07Z: the API was OOM-killed
mid-publish and two YouTube rows sat reserved for 5 h — while Buffer showed BOTH posts `sent` one
second before the kill. So a stale reservation must never be retried blindly (`failed` rows ARE
retried by `_RESERVE`, which would post a duplicate). It is resolved against the platform:

  Buffer platforms (youtube, x, tiktok) — look for a post on the brand's channel in the window after
    the reservation:
      exactly one `sent` post, not already recorded      → settle `posted` with its id + link
      nothing at all                                      → `failed` (it never left; safe to retry)
      a post still in flight, an ambiguous match, or a
      Buffer-side failure we cannot attribute             → wait / leave for a human
  Meta platforms (instagram, facebook) have no lookup here — the row stays reserved (no duplicate
    risk) and #clip-queue is told ONCE, via the row's `error` field.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text

log = structlog.get_logger(__name__)

STALE_MIN = 20                     # a publish that has not settled in 20 min is not in flight
WINDOW_BEFORE = timedelta(minutes=2)
WINDOW_AFTER = timedelta(minutes=15)
IN_FLIGHT = {"sending", "scheduled", "buffer", "draft", "needs_approval", "pending"}
MANUAL_NOTE = "stale reservation: the publish crashed before settling; verify on the platform manually"

_STALE = text(
    "SELECT p.clip_id, p.platform, p.updated_at, p.error, c.hook FROM clipnet_post p "
    "JOIN clipnet_clip c ON c.id = p.clip_id JOIN clipnet_job j ON j.id = c.job_id "
    "WHERE j.brand_id = :b AND p.status = 'reserved' "
    "AND p.updated_at < now() - make_interval(mins => :stale)"
)
_KNOWN = text(
    "SELECT p.external_id FROM clipnet_post p JOIN clipnet_clip c ON c.id = p.clip_id "
    "JOIN clipnet_job j ON j.id = c.job_id "
    "WHERE j.brand_id = :b AND p.platform = :p AND p.external_id IS NOT NULL"
)
# Each write re-checks status='reserved' AND the exact updated_at it decided on, so a publish that
# settles the row concurrently always wins over the reconcile.
_SETTLE_POSTED = text(
    "UPDATE clipnet_post SET status='posted', external_id=:x, permalink=:u, error=NULL "
    "WHERE clip_id=:id AND platform=:p AND status='reserved' AND updated_at=:t"
)
_SETTLE_FAILED = text(
    "UPDATE clipnet_post SET status='failed', error=:e, updated_at=now() "
    "WHERE clip_id=:id AND platform=:p AND status='reserved' AND updated_at=:t"
)
_MARK_MANUAL = text(
    "UPDATE clipnet_post SET error=:e WHERE clip_id=:id AND platform=:p AND status='reserved' "
    "AND updated_at=:t AND error IS NULL"
)


def _ts(v: Any) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def decide(reserved_at: datetime, posts: list[dict], known_ids: set[str]) -> tuple[str, Any]:
    """('posted', post) | ('failed', reason) | ('wait', reason). Pure; `posts` are Buffer nodes."""
    lo, hi = reserved_at - WINDOW_BEFORE, reserved_at + WINDOW_AFTER
    near = [p for p in posts if p.get("createdAt") and lo <= _ts(p["createdAt"]) <= hi
            and p.get("id") not in known_ids]
    if not near:
        return "failed", "stale reservation: no Buffer post found for it, so it never left — safe to retry"
    status = [(p.get("status") or "").lower() for p in near]
    if any(s in IN_FLIGHT for s in status):
        return "wait", "a Buffer post in the window is still in flight"
    sent = [p for p, s in zip(near, status, strict=True) if s == "sent"]
    if len(sent) == 1 and len(near) == 1:
        return "posted", sent[0]
    return "wait", f"{len(near)} unattributed Buffer posts in the window ({status}); needs a human"


async def buffer_posts(brand_id: str, service: str, first: int = 25) -> list[dict]:
    from meshpilot.platforms import buffer

    channel = await buffer._channel_id_for_service(brand_id, service)
    info = await buffer.list_channels(brand_id)
    data = await buffer._graphql(
        buffer._buffer_token(brand_id),
        "query($input: PostsInput!, $first: Int) { posts(input: $input, first: $first) "
        "{ edges { node { id status createdAt externalLink } } } }",
        {"input": {"organizationId": info["organization_id"], "filter": {"channelIds": [channel]}},
         "first": first})
    return [e["node"] for e in (data.get("posts") or {}).get("edges") or []]


async def reconcile(brand_id: str, *, engine: Any, lookup: Any = None, notify_fn: Any = None) -> dict:
    """Resolve this brand's stale reservations. Never raises: a failed lookup leaves the row as is."""
    from meshpilot.agent.clipnet.publish import BUFFER_SERVICE, _notify

    lookup, notify_fn = lookup or buffer_posts, notify_fn or _notify
    out: dict[str, list] = {"posted": [], "failed": [], "waiting": [], "manual": []}
    async with engine.connect() as conn:
        rows = [dict(r) for r in (await conn.execute(_STALE, {"b": brand_id, "stale": STALE_MIN})).mappings()]
    for r in rows:
        key = {"id": r["clip_id"], "p": r["platform"], "t": r["updated_at"]}
        label = f'{r["platform"]}:{r["clip_id"]}'
        try:
            if r["platform"] not in BUFFER_SERVICE:
                if r["error"] is None:
                    async with engine.begin() as conn:
                        marked = (await conn.execute(_MARK_MANUAL, {**key, "e": MANUAL_NOTE})).rowcount
                    if marked:
                        await notify_fn(brand_id, f"⚠️ **Publish crashed mid-post** on {r['platform']}: "
                                                  f"{r['hook']}\nCheck the platform; it may already be live.")
                out["manual"].append(label)
                continue
            posts = await lookup(brand_id, BUFFER_SERVICE[r["platform"]])
            async with engine.connect() as conn:
                known = {x[0] for x in (await conn.execute(_KNOWN, {"b": brand_id, "p": r["platform"]})).all()}
            verdict, detail = decide(_ts(r["updated_at"]), posts, known)
            async with engine.begin() as conn:
                if verdict == "posted":
                    await conn.execute(_SETTLE_POSTED, {**key, "x": detail["id"], "u": detail.get("externalLink")})
                elif verdict == "failed":
                    await conn.execute(_SETTLE_FAILED, {**key, "e": detail})
            out["waiting" if verdict == "wait" else verdict].append(label)
            log.info("clipnet.reservation_reconciled", brand_id=brand_id, clip_id=str(r["clip_id"]),
                     platform=r["platform"], verdict=verdict)
        except Exception as exc:  # noqa: BLE001 — a lookup failure must not break the publish tick
            log.warning("clipnet.reconcile_failed", brand_id=brand_id, row=label, error=str(exc)[:200])
            out["waiting"].append(label)
    return out
