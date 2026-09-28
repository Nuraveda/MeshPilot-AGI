"""CLIPNET dispatcher (sub-lane A3): turn queued jobs into worker executions, and heal stuck ones.

Runs as the `clipnet_dispatch` capability on a short cron, per brand. One tick:
  1. REQUEUE  in-progress jobs whose worker heartbeat is stale (the execution died or timed out).
  2. FAIL     queued jobs that have used up MAX_ATTEMPTS.
  3. START    the oldest queued job — only if no job for this brand is running, and only if it was
              not started in the last DISPATCH_GRACE_MIN (a started execution needs a minute to
              claim its lease; starting it again in that window would double-run it).
  4. PAUSE    instead of starting, when the OpenRouter balance is under MIN_CREDIT_USD. Measured
              2026-09-27: the balance ran out mid-morning, three jobs died on "402 Payment Required"
              and each was retried to MAX_ATTEMPTS — re-downloading through the paid proxy every
              time. A paused job keeps its attempts; it starts on the first tick after a top-up.

A started job is marked by setting heartbeat_at while status stays `queued`: the worker's own claim
needs `lease_until` free, so the dispatcher must not touch the lease.
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text

log = structlog.get_logger(__name__)
MAX_ATTEMPTS = 3
STALE_HEARTBEAT_MIN = 4
DISPATCH_GRACE_MIN = 10
MIN_CREDIT_USD = 1.0      # one job is ~$0.07 of model calls; stop well before a 402 can land mid-job


def min_credit_usd() -> float:
    """The pause threshold. CLIPNET_MIN_CREDIT_USD lowers it when Claude runs on a BYOK key (then only
    Whisper + Jev, ~$0.015 a job, draw on the OpenRouter balance)."""
    import os

    try:
        return float(os.environ.get("CLIPNET_MIN_CREDIT_USD") or MIN_CREDIT_USD)
    except ValueError:
        return MIN_CREDIT_USD
PAUSED = "paused: OpenRouter credit"
ACTIVE = ("fetching", "transcribing", "selecting", "rendering")

_REQUEUE = text(
    "UPDATE clipnet_job SET status='queued', attempt=attempt+1, lease_until=NULL, "
    "error='stale heartbeat: the worker stopped reporting', updated_at=now() "
    "WHERE brand_id=:b AND status = ANY(:active) "
    "AND coalesce(heartbeat_at, updated_at) < now() - make_interval(mins => :stale) RETURNING id"
)
_FAIL = text(
    "UPDATE clipnet_job SET status='failed', updated_at=now() "
    "WHERE brand_id=:b AND status='queued' AND attempt >= :max RETURNING id"
)
_RUNNING = text(
    "SELECT count(*) FROM clipnet_job WHERE brand_id=:b AND ("
    "status = ANY(:active) OR (status='queued' AND heartbeat_at > now() - make_interval(mins => :grace)))"
)
_NEXT = text(
    "UPDATE clipnet_job SET heartbeat_at=now(), updated_at=now() WHERE id = ("
    "SELECT id FROM clipnet_job WHERE brand_id=:b AND status='queued' AND attempt < :max "
    "AND (heartbeat_at IS NULL OR heartbeat_at < now() - make_interval(mins => :grace)) "
    "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id"
)
_NEW_BLOCKED = text(
    "UPDATE clipnet_job SET stage_outputs = stage_outputs || '{\"notified_blocked\": true}'::jsonb "
    "WHERE brand_id=:b AND status='blocked' AND NOT (stage_outputs ? 'notified_blocked') "
    "RETURNING id, url, error"
)
_FAILED_INFO = text("SELECT id, url, error FROM clipnet_job WHERE id = ANY(CAST(:ids AS uuid[]))")
_UNMARK = text("UPDATE clipnet_job SET heartbeat_at=NULL, error=:e, updated_at=now() WHERE id=:id")
_ERROR_OF = text("SELECT error FROM clipnet_job WHERE id=:id")


async def credit_left() -> float | None:
    """Remaining OpenRouter balance in USD, or None when it cannot be read (then dispatch proceeds:
    a flaky balance check must not halt a week of posting)."""
    import os

    import httpx

    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.get("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"})
            r.raise_for_status()
            d = r.json()["data"]
        return float(d["total_credits"]) - float(d["total_usage"])
    except Exception as exc:
        log.warning("clipnet.credit_check_failed", error=str(exc)[:200])
        return None


def _engine_or(engine: Any):
    from meshpilot.db.session import _engine

    return engine or _engine()


_SET_GUIDANCE = text(
    "UPDATE clipnet_job SET stage_outputs = jsonb_set(stage_outputs, '{guidance}', to_jsonb(CAST(:g AS text))) "
    "WHERE id = :id")

async def dispatch(brand_id: str, *, engine: Any = None, start: Any = None, bucket: str | None = None,
                   notify_fn: Any = None, credit_fn: Any = None) -> dict:
    from meshpilot.agent.clipnet import worker_client
    from meshpilot.media.generation.storage import bucket_for

    start = start or worker_client.start_execution
    bucket = bucket or bucket_for(brand_id)
    eng = _engine_or(engine)
    p = {"b": brand_id, "active": list(ACTIVE), "stale": STALE_HEARTBEAT_MIN,
         "max": MAX_ATTEMPTS, "grace": DISPATCH_GRACE_MIN}
    async with eng.begin() as conn:
        requeued = [str(r[0]) for r in (await conn.execute(_REQUEUE, p)).all()]
        failed = [str(r[0]) for r in (await conn.execute(_FAIL, p)).all()]
        running = int((await conn.execute(_RUNNING, p)).scalar() or 0)
        job_id = None if running else (await conn.execute(_NEXT, p)).scalar()
        newly_blocked = [dict(r) for r in (await conn.execute(_NEW_BLOCKED, p)).mappings().all()]
        failed_info = ([dict(r) for r in (await conn.execute(_FAILED_INFO, {"ids": failed})).mappings().all()]
                       if failed else [])
    if notify_fn is None:
        from meshpilot.agent.clipnet.notify import notify as notify_fn
    started, paused = None, None
    if job_id is not None:
        left = await (credit_fn or credit_left)()
        floor = min_credit_usd()
        if left is not None and left < floor:
            async with eng.begin() as conn:
                prev = (await conn.execute(_ERROR_OF, {"id": job_id})).scalar() or ""
                await conn.execute(_UNMARK, {"id": job_id, "e": f"{PAUSED} ${left:.2f} < ${floor:.2f}"})
            if not prev.startswith(PAUSED):   # once per job, not every 10 minutes
                await notify_fn(brand_id, f"⏸️ **Clip jobs paused — OpenRouter balance is ${left:.2f}.** "
                                          "Top up at openrouter.ai/settings/credits; jobs resume on their own.")
            paused, job_id = f"{left:.2f}", None
    if job_id is not None:
        # CLIPNET-LEARN L3: hand the worker's clip picker what this brand knows and has learned. The
        # worker is compute-only and never reads memory itself; guidance rides on the job row.
        from meshpilot.agent.clipnet.learn import guidance

        g = await guidance(brand_id, engine=eng)
        if g:
            async with eng.begin() as conn:
                await conn.execute(_SET_GUIDANCE, {"id": job_id, "g": g})
        try:
            start(str(job_id), bucket)
            started = str(job_id)
        except Exception as exc:  # the worker never started: clear the mark so the next tick retries
            async with eng.begin() as conn:
                await conn.execute(_UNMARK, {"id": job_id, "e": f"dispatch failed: {exc}"[:500]})
            log.warning("clipnet.dispatch_failed", brand_id=brand_id, job_id=str(job_id), error=str(exc)[:200])
    for j in failed_info:
        await notify_fn(brand_id, f"❌ **Clip job failed after {MAX_ATTEMPTS} attempts:** {j['url']}\n{(j['error'] or '')[:300]}")
    for j in newly_blocked:
        await notify_fn(brand_id, f"🛑 **Clip job blocked — needs you:** {j['url']}\n{(j['error'] or '')[:300]}")
    out = {"requeued": requeued, "failed": failed, "running": running, "started": started, "paused": paused}
    log.info("clipnet.dispatch", brand_id=brand_id, **out)
    return out
