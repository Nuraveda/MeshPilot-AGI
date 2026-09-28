"""CLIPNET-UNATTENDED: keep clip storage bounded so the channels can run for a week untended.

Measured 2026-09-27: nothing ever deleted a rendered clip. 21 clips held 699 MB and discovery adds
~12 a day (~400 MB), so the media buckets grow without bound. The platforms keep their own copy
once a clip is posted, so our file is only needed until every platform has it.

A clip's file is deleted (and its media_url cleared, which also takes it out of every platform's
queue — `_NEXT_FOR_PLATFORM` needs a media_url) when:
  - it is POSTED on every platform the brand can post to, and the last post is KEEP_AFTER_POST_H
    old (Buffer and Meta fetch the file asynchronously; the margin covers a late fetch);
  - it was BLOCKED by the gate KEEP_AFTER_POST_H ago (it will never be posted); or
  - it is older than MAX_AGE_DAYS (stale for a trend channel, whatever is still pending).
Never while any of its posts is `reserved` (a post in flight still needs the file).
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text

log = structlog.get_logger(__name__)
KEEP_AFTER_POST_H = 24
MAX_AGE_DAYS = 7

_TARGETS = text(
    "SELECT c.id, c.media_url FROM clipnet_clip c JOIN clipnet_job j ON j.id = c.job_id "
    "WHERE j.brand_id = :b AND c.media_url IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM clipnet_post p WHERE p.clip_id = c.id AND p.status = 'reserved') AND ("
    "  c.created_at < now() - make_interval(days => :age)"
    "  OR (c.gate_status = 'blocked' AND c.created_at < now() - make_interval(hours => :keep))"
    "  OR ((SELECT count(DISTINCT p.platform) FROM clipnet_post p WHERE p.clip_id = c.id AND p.status = 'posted'"
    "        AND p.platform = ANY(:plats)) = cardinality(CAST(:plats AS text[]))"
    "      AND (SELECT max(p.updated_at) FROM clipnet_post p WHERE p.clip_id = c.id)"
    "          < now() - make_interval(hours => :keep)))"
)
_CLEAR = text("UPDATE clipnet_clip SET media_url = NULL WHERE id = ANY(CAST(:ids AS uuid[]))")


def object_path(media_url: str, supabase_url: str, bucket: str) -> str | None:
    """The object path inside `bucket` for one of OUR public clip URLs, else None.

    Only files under the brand's own bucket and `clipnet/` prefix are ever deleted — a URL that
    points anywhere else (another bucket, another host) is left alone."""
    prefix = f"{supabase_url.rstrip('/')}/storage/v1/object/public/{bucket}/"
    if not media_url.startswith(prefix):
        return None
    path = media_url[len(prefix):].split("?", 1)[0]
    return path if path.startswith("clipnet/") and ".." not in path else None


async def _delete_objects(bucket: str, paths: list[str]) -> None:
    import httpx

    from meshpilot.media.generation.storage import _headers, _supabase

    url, key = _supabase()
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.request("DELETE", f"{url}/storage/v1/object/{bucket}", headers=_headers(key),
                               json={"prefixes": paths})
        r.raise_for_status()


async def purge(brand_id: str, *, engine: Any = None, delete: Any = None,
                platforms: tuple[str, ...] | None = None, bucket: str | None = None) -> dict:
    import os

    from meshpilot.agent.clipnet.publish import _engine_or, configured_platforms
    from meshpilot.media.generation.storage import bucket_for

    eng, delete = _engine_or(engine), delete or _delete_objects
    plats = list(platforms if platforms is not None else configured_platforms(brand_id))
    bucket = bucket or bucket_for(brand_id)
    supa = (os.environ.get("SUPABASE_URL") or "").strip()
    async with eng.begin() as conn:
        rows = (await conn.execute(_TARGETS, {"b": brand_id, "plats": plats, "keep": KEEP_AFTER_POST_H,
                                              "age": MAX_AGE_DAYS})).all()
    paths = {str(r[0]): object_path(r[1], supa, bucket) for r in rows}
    ids = [cid for cid, p in paths.items() if p]
    if ids:
        await delete(bucket, [paths[i] for i in ids])     # delete first: a cleared URL is never retried
        async with eng.begin() as conn:
            await conn.execute(_CLEAR, {"ids": ids})
    skipped = [cid for cid, p in paths.items() if not p]
    out = {"deleted": len(ids), "skipped_foreign_url": len(skipped)}
    log.info("clipnet.purge", brand_id=brand_id, **out)
    return out
