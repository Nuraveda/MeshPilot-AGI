"""CLIPNET-LEARN L3 — the clip channels learn from what their clips actually did.

Spec: docs/plans/2026-09-25-clipnet-learn.md (L3) and docs/plans/2026-09-27-clipnet-learn-l3.md.
Operator, 2026-09-27: "make sure he picks better videos and gets stronger with time — that's why we
built MeshPilot."

Three things, all from the 24 h readings in `clipnet_post_metric`:

1. **Scores.** A clip's score is its 24 h views relative to the brand's own median on each platform,
   averaged over the platforms that are actually MEASURED for that brand. A platform whose every
   reading is 0 (measured 2026-09-27: TikTok and X read 0 on every post, most likely not publicly
   visible or not reported) is treated as unmeasured, never as "this clip failed there".
2. **Lessons.** `learn()` distills the scored clips into at most 5 durable lessons (kind=fact,
   key `clip:lesson:<slug>`, source=`curator` — never operator-verified, so it can never override the
   brand facts). Below MIN_MEASURED scored clips it writes nothing: a handful of posts must not teach
   the brand something false.
3. **Guidance.** `guidance()` is what the rest of the pipeline reads: verified brand facts, the
   learned lessons, and the best/worst past hooks. The dispatcher hands it to the worker's clip
   picker; discovery hands it to its screen and ranks creators by `creator_multipliers()`.

Self-changes (lessons, query changes, auto-blocks) are logged as episodes and posted to the brand's
Discord channel so the operator can see and undo them.
"""
from __future__ import annotations

import json
import re
import statistics
from typing import Any

import structlog
from sqlalchemy import text

log = structlog.get_logger(__name__)

MIN_MEASURED = 20            # scored clips before any lesson is written (spec L3, settled 2026-09-25)
MULT_MIN_CLIPS = 2           # clips from one creator before its multiplier moves
MULT_BOUNDS = (0.5, 2.0)     # one lucky clip can never dominate discovery
AUTO_BLOCK_MIN_CLIPS = 4
AUTO_BLOCK_BELOW = 0.2

_ROWS = text(
    "SELECT c.id::text AS clip_id, c.hook, c.start_s, c.end_s, j.campaign, j.stage_outputs, "
    "       m.platform, m.video_views, m.impressions "
    "FROM clipnet_post_metric m JOIN clipnet_clip c ON c.id = m.clip_id JOIN clipnet_job j ON j.id = c.job_id "
    "WHERE j.brand_id = :b AND m.age_bucket = '24h'"
)


def _views(r: dict) -> int:
    return int(r.get("video_views") if r.get("video_views") is not None else (r.get("impressions") or 0))


def clips_from_rows(rows: list[dict]) -> list[dict]:
    """One record per clip: hook, creator, query, length, and {platform: 24 h views}."""
    by: dict[str, dict] = {}
    for r in rows:
        so = r.get("stage_outputs") or {}
        if isinstance(so, str):
            so = json.loads(so)
        c = by.setdefault(r["clip_id"], {
            "clip_id": r["clip_id"], "hook": r.get("hook") or "", "campaign": r.get("campaign"),
            "creator": ((so.get("creator") or {}).get("name")), "channel_id": ((so.get("creator") or {}).get("channel_id")),
            "query": ((so.get("discovered") or {}).get("query")),
            "seconds": round(float(r.get("end_s") or 0) - float(r.get("start_s") or 0)), "views": {}})
        c["views"][r["platform"]] = _views(r)
    return list(by.values())


def measured_platforms(clips: list[dict]) -> set[str]:
    """Platforms with at least one non-zero reading. All-zero platforms are unmeasured, not failures."""
    return {p for c in clips for p, v in c["views"].items() if v > 0}


def score(clips: list[dict]) -> list[dict]:
    """Attach `score` (mean of views / brand median, over measured platforms) to every scorable clip."""
    live = measured_platforms(clips)
    med = {p: statistics.median([c["views"][p] for c in clips if p in c["views"]]) for p in live}
    out = []
    for c in clips:
        rel = [c["views"][p] / med[p] for p in live if p in c["views"] and med[p] > 0]
        if rel:
            out.append({**c, "score": round(sum(rel) / len(rel), 3)})
    return out


def creator_multipliers(scored: list[dict], *, min_clips: int = MULT_MIN_CLIPS) -> dict[str, float]:
    """channel_id → bounded boost for discovery ranking, from that creator's mean clip score."""
    by: dict[str, list[float]] = {}
    for c in scored:
        if c.get("channel_id"):
            by.setdefault(c["channel_id"], []).append(c["score"])
    lo, hi = MULT_BOUNDS
    return {ch: round(min(max(sum(s) / len(s), lo), hi), 3) for ch, s in by.items() if len(s) >= min_clips}


def auto_block_candidates(scored: list[dict]) -> list[tuple[str, str, float]]:
    """(channel_id, name, mean) for creators who have consistently flopped — pruned automatically."""
    by: dict[str, list[dict]] = {}
    for c in scored:
        if c.get("channel_id"):
            by.setdefault(c["channel_id"], []).append(c)
    out = []
    for ch, cs in by.items():
        mean = sum(c["score"] for c in cs) / len(cs)
        if len(cs) >= AUTO_BLOCK_MIN_CLIPS and mean < AUTO_BLOCK_BELOW:
            out.append((ch, cs[0].get("creator") or ch, round(mean, 3)))
    return out


def _engine_or(engine: Any):
    from meshpilot.db.session import _engine

    return engine or _engine()


async def scored_clips(brand_id: str, *, engine: Any = None) -> list[dict]:
    async with _engine_or(engine).connect() as conn:
        rows = [dict(r) for r in (await conn.execute(_ROWS, {"b": brand_id})).mappings().all()]
    return score(clips_from_rows(rows))


# ── guidance: what the picker and the discovery screen read ─────────────────────────────────────
_FACT_ROWS = text(
    "SELECT key, content, source FROM agent_memory WHERE brand_id = :b AND kind = 'fact' "
    "AND (key LIKE 'brand:%' OR key LIKE 'clip:lesson:%') ORDER BY key")


async def guidance(brand_id: str, *, engine: Any = None, scored: list[dict] | None = None) -> str:
    """Brand facts (operator-verified) + learned lessons + best/worst past hooks, as prompt text."""
    from meshpilot.agent.memory.store import is_verified_provenance

    eng = _engine_or(engine)
    try:
        async with eng.connect() as conn:
            facts = [dict(r) for r in (await conn.execute(_FACT_ROWS, {"b": brand_id})).mappings().all()]
        scored = scored if scored is not None else await scored_clips(brand_id, engine=eng)
    except Exception as exc:  # noqa: BLE001 — guidance is advisory; its absence must never stop a job
        log.warning("clipnet.guidance_failed", brand_id=brand_id, error=str(exc)[:200])
        return ""
    brand = [f["content"] for f in facts if f["key"].startswith("brand:") and is_verified_provenance(f["source"])]
    lessons = [f["content"] for f in facts if f["key"].startswith("clip:lesson:")]
    parts = []
    if brand:
        parts.append("About this brand (verified by the operator):\n" + "\n".join(f"- {b}" for b in brand))
    if lessons:
        parts.append("Learned from this brand's own results:\n" + "\n".join(f"- {x}" for x in lessons))
    if len(scored) >= 5:
        ranked = sorted(scored, key=lambda c: c["score"], reverse=True)
        parts.append("Best past hooks (score = x the brand's median views):\n"
                     + "\n".join(f'- "{c["hook"]}" ({c["score"]}x, {c["seconds"]}s)' for c in ranked[:5]))
        parts.append("Weakest past hooks:\n"
                     + "\n".join(f'- "{c["hook"]}" ({c["score"]}x)' for c in ranked[-3:]))
    return "\n\n".join(parts)[:3000]


# ── the learner ─────────────────────────────────────────────────────────────────────────────────
_LESSON_PROMPT = """You improve a short-form clip channel. Below are this brand's clips scored by
24-hour views relative to the brand's own median (1.0 = median, 2.0 = double). Platforms that report
no data are already excluded.

Brand: {brand_id}
{brand_facts}

Clips (score | seconds | creator | hook):
{table}

Write at most 5 durable, specific lessons about what makes a clip do well or badly FOR THIS BRAND:
hook wording, topic, creator, length, moment type. Each lesson must be supported by several clips
above, not one. No generic advice. Return ONLY JSON:
[{{"key": "<short-stable-slug>", "content": "<one lesson>", "importance": <0..1>}}]"""


def parse_lessons(raw: str) -> list[dict]:
    m = re.search(r"\[.*\]", raw or "", re.S)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for it in items[:5]:
        key = re.sub(r"[^a-z0-9-]+", "-", str(it.get("key", "")).lower()).strip("-")[:48]
        content = str(it.get("content", "")).strip()
        if key and content:
            out.append({"key": f"clip:lesson:{key}", "content": content[:400],
                        "importance": min(max(float(it.get("importance") or 0.6), 0.0), 1.0)})
    return out


async def _complete(prompt: str) -> str:
    from meshpilot.agent.loop import llm as agent_llm

    return await agent_llm.complete(prompt, tier="complex")


async def learn(brand_id: str, *, engine: Any = None, complete: Any = None, remember_fn: Any = None,
                notify_fn: Any = None) -> dict:
    """Score the brand's measured clips; write lessons (≥ MIN_MEASURED) and prune flops."""
    from meshpilot.agent.memory.store import remember

    eng = _engine_or(engine)
    remember_fn, complete = remember_fn or remember, complete or _complete
    scored = await scored_clips(brand_id, engine=eng)
    blocked = []
    for ch, name, mean in auto_block_candidates(scored):
        async with eng.begin() as conn:
            row = (await conn.execute(text(
                "INSERT INTO clipnet_creator_block (channel_id, channel_name, reason) VALUES (:c, :n, :r) "
                "ON CONFLICT (channel_id) DO NOTHING RETURNING channel_id"),
                {"c": ch, "n": name, "r": f"auto: {AUTO_BLOCK_MIN_CLIPS}+ clips averaging {mean}x the median"})).first()
        if row:
            blocked.append(name)
    if len(scored) < MIN_MEASURED:
        log.info("clipnet.learn_insufficient_sample", brand_id=brand_id, scored=len(scored), need=MIN_MEASURED)
        return {"scored": len(scored), "need": MIN_MEASURED, "lessons": [], "auto_blocked": blocked}
    ranked = sorted(scored, key=lambda c: c["score"], reverse=True)
    table = "\n".join(f'{c["score"]} | {c["seconds"]} | {c.get("creator") or "-"} | {c["hook"]}' for c in ranked[:40])
    facts = await guidance(brand_id, engine=eng, scored=[])
    lessons = parse_lessons(await complete(_LESSON_PROMPT.format(brand_id=brand_id, brand_facts=facts, table=table)))
    for les in lessons:
        await remember_fn(brand_id, "fact", les["content"], key=les["key"], importance=les["importance"],
                          source="curator", metadata={"capability": "clipnet_learn", "scored": len(scored)})
    summary = (f"Clip learner: {len(scored)} measured clips → {len(lessons)} lessons"
               + (f"; auto-blocked {', '.join(blocked)}" if blocked else ""))
    await remember_fn(brand_id, "episode", summary + "\n" + "\n".join(f"- {x['content']}" for x in lessons),
                      source="clipnet", metadata={"capability": "clipnet_learn"})
    if lessons or blocked:
        from meshpilot.agent.clipnet.notify import notify

        await (notify_fn or notify)(brand_id, "🧠 **" + summary + "**\n" + "\n".join(f"• {x['content']}" for x in lessons))
    return {"scored": len(scored), "lessons": [x["key"] for x in lessons], "auto_blocked": blocked}


# ── weekly: the agent tunes its own discovery searches ──────────────────────────────────────────
MAX_QUERIES, MIN_QUERIES = 8, 3
QUERY_MIN_SCORED = 10          # brand-wide scored clips before the agent touches its searches
RETIRE_MIN_CLIPS, RETIRE_BELOW = 3, 0.5

_QUERY_PROMPT = """You run discovery for a short-form clip channel. It searches YouTube for videos by
independent creators in its niche, clips them and posts the clips.

Niche: {subject}
{guidance}

Current searches and how their clips did (mean score = x the brand's median views; n = clips measured):
{stats}

Propose up to {room} NEW YouTube search phrases (2-6 words, English) likely to find videos that do
as well as the best ones above. Stay strictly inside the niche. No channel names, no phrases that
would surface music, movies, TV or news. Return ONLY JSON: {{"add": ["...", "..."]}}"""


def query_stats(scored: list[dict], queries: list[str]) -> dict[str, dict]:
    out = {q: {"n": 0, "mean": None} for q in queries}
    for q in queries:
        s = [c["score"] for c in scored if c.get("query") == q]
        if s:
            out[q] = {"n": len(s), "mean": round(sum(s) / len(s), 3)}
    return out


def plan_queries(queries: list[str], stats: dict[str, dict], proposed: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(new_list, retired, added) — bounded: retire proven flops, never below MIN_QUERIES, cap at MAX."""
    flops = [q for q in queries if stats.get(q, {}).get("n", 0) >= RETIRE_MIN_CLIPS
             and (stats[q]["mean"] or 0) < RETIRE_BELOW]
    keep = [q for q in queries if q not in flops]
    seen = {q.lower() for q in queries}
    added = []
    for p in proposed:                               # additions first, so a flop can be REPLACED
        p = re.sub(r"\s+", " ", str(p)).strip()
        if 2 <= len(p.split()) <= 6 and len(p) <= 60 and p.lower() not in seen and len(keep) + len(added) < MAX_QUERIES:
            seen.add(p.lower())
            added.append(p)
    retired = list(flops)
    while len(keep) + len(added) < MIN_QUERIES and retired:   # never strip a niche below the floor
        keep.append(retired.pop())
    return [q for q in queries if q in keep] + added, retired, added


async def refresh_queries(brand_id: str, *, engine: Any = None, complete: Any = None,
                          remember_fn: Any = None, notify_fn: Any = None) -> dict:
    from meshpilot.agent.clipnet.campaigns import load_campaign
    from meshpilot.agent.memory.store import remember

    eng = _engine_or(engine)
    complete, remember_fn = complete or _complete, remember_fn or remember
    scored = await scored_clips(brand_id, engine=eng)
    if len(scored) < QUERY_MIN_SCORED:
        return {"scored": len(scored), "need": QUERY_MIN_SCORED, "changed": {}}
    async with eng.connect() as conn:
        slugs = [r[0] for r in (await conn.execute(text(
            "SELECT slug FROM clipnet_campaign WHERE kind='organic' AND active AND :b = ANY(brand_ids)"),
            {"b": brand_id})).all()]
    g = await guidance(brand_id, engine=eng, scored=scored)
    changed = {}
    for slug in slugs:
        campaign = await load_campaign(slug, engine=eng)
        queries = list((campaign.discovery or {}).get("queries") or [])
        stats = query_stats(scored, queries)
        room = max(MAX_QUERIES - len(queries) + 2, 0)
        raw = await complete(_QUERY_PROMPT.format(
            subject=campaign.subject, guidance=g, room=room,
            stats="\n".join(f'- "{q}": n={s["n"]}, mean={s["mean"]}' for q, s in stats.items())))
        m = re.search(r"\{.*\}", raw or "", re.S)
        try:
            proposed = (json.loads(m.group(0)).get("add") or []) if m else []
        except ValueError:
            proposed = []
        new, retired, added = plan_queries(queries, stats, proposed)
        if new == queries:
            continue
        async with eng.begin() as conn:
            await conn.execute(text(
                "UPDATE clipnet_campaign SET discovery = jsonb_set(discovery, '{queries}', CAST(:q AS jsonb)) "
                "WHERE slug = :s"), {"q": json.dumps(new), "s": slug})
        changed[slug] = {"added": added, "retired": retired}
        msg = (f"Discovery searches updated for {slug}: added {added or 'none'}; retired {retired or 'none'}.")
        await remember_fn(brand_id, "episode", msg, source="clipnet",
                          metadata={"capability": "clipnet_refresh_queries", "slug": slug, "before": queries, "after": new})
        from meshpilot.agent.clipnet.notify import notify

        await (notify_fn or notify)(brand_id, "🔎 **" + msg + "**")
    return {"scored": len(scored), "changed": changed}
