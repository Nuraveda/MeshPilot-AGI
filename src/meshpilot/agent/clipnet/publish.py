"""CLIPNET publisher (sub-lane A4): post the brand's next rendered clip to every platform.

Runs as the `clipnet_publish` capability on the brand's daily schedule (e.g. 3 slots a day). One
tick publishes AT MOST ONE clip, so the schedule is the posting cadence.

Routing is standard for every brand (operator decision 2026-09-24): X, TikTok and YouTube through
Buffer; Instagram and Facebook through the Meta Graph API directly.

Safety, all fail-closed:
  - kill switches: `agent_clipnet_enabled` AND `agent_publish_enabled` (both ship OFF);
  - a compliance gate (required hashtags, source on the campaign's allow-list, clip text about the
    campaign subject) — a miss marks the clip `blocked` with the reason and posts nothing;
  - the `clipnet_post` ledger: a row is RESERVED before each platform call. `posted` is never
    repeated; a `reserved` row whose outcome is unknown (a crash mid-call) is NEVER retried
    automatically — the JOBS lesson: after an unclear outcome, do not spend a second attempt.
"""
from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text

log = structlog.get_logger(__name__)

PLATFORMS = ("instagram", "facebook", "tiktok", "youtube", "x")
BUFFER_SERVICE = {"tiktok": "tiktok", "youtube": "youtube", "x": "x"}
SHEET_COLUMNS = ["posted_at", "brand", "campaign", "platform", "status", "url", "hook", "caption",
                 "source", "clip_url", "submit_by"]
_WORD = re.compile(r"[A-Za-z0-9]{4,}")
_STOP = {"with", "from", "that", "this", "their", "about", "into", "your", "what", "when"}

_NEXT_CLIP = text(
    "SELECT c.id, c.job_id, c.start_s, c.end_s, c.hook, c.caption, c.media_url, "
    "j.campaign, j.stage_outputs "
    "FROM clipnet_clip c JOIN clipnet_job j ON j.id = c.job_id "
    "WHERE j.brand_id = :b AND c.gate_status <> 'blocked' AND c.media_url IS NOT NULL "
    "AND (SELECT count(*) FROM clipnet_post p WHERE p.clip_id = c.id "
    "     AND p.status IN ('posted','reserved')) < :n "
    "ORDER BY c.created_at LIMIT 1 FOR UPDATE OF c SKIP LOCKED"
)
_BLOCK = text("UPDATE clipnet_clip SET gate_status='blocked', gate_reason=:r WHERE id=:id")
_CREATOR_BLOCKED = text("SELECT reason FROM clipnet_creator_block WHERE channel_id = :c")
_PASS = text("UPDATE clipnet_clip SET gate_status='passed', gate_reason=NULL WHERE id=:id")
_LEDGER = text("SELECT platform, status FROM clipnet_post WHERE clip_id=:id")
_RESERVE = text(
    "INSERT INTO clipnet_post (clip_id, platform, status) VALUES (:id, :p, 'reserved') "
    "ON CONFLICT (clip_id, platform) DO UPDATE SET status='reserved', error=NULL, updated_at=now() "
    "WHERE clipnet_post.status = 'failed' RETURNING platform"
)
_SETTLE = text(
    "UPDATE clipnet_post SET status=:s, external_id=:x, permalink=:u, error=:e, updated_at=now() "
    "WHERE clip_id=:id AND platform=:p"
)


def configured_platforms(brand_id: str) -> tuple[str, ...]:
    """The platforms this brand can actually post to. A brand without Meta keys (e.g. Buffer-only)
    simply has fewer platforms — they are skipped, never attempted and failed on every tick."""
    from meshpilot.config import brand_env

    meta = bool(brand_env("META_PAGE_ID", brand_id) and brand_env("SYSTEM_USER_TOKEN", brand_id))
    ig = meta and bool(brand_env("META_IG_USER_ID", brand_id))
    buf = bool(brand_env("BUFFER_API_KEY", brand_id))
    ok = {"instagram": ig, "facebook": meta, "tiktok": buf, "youtube": buf, "x": buf}
    return tuple(p for p in PLATFORMS if ok[p])


def enabled() -> bool:
    from meshpilot.config import settings

    s = settings()
    return bool(getattr(s, "agent_clipnet_enabled", False) and getattr(s, "agent_publish_enabled", False))


def subject_terms(subject: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(subject)} - _STOP


def clip_text(stage_outputs: dict, start_s: float) -> str:
    """The transcript text of the pick this clip was rendered from (matched on start time)."""
    for p in (stage_outputs or {}).get("picks") or []:
        if abs(float(p.get("start", -1)) - float(start_s)) < 0.5:
            return p.get("text") or ""
    return ""


def creator_of(clip: dict) -> dict:
    """The discovered creator this clip came from (organic campaigns), or {}."""
    return dict(((clip.get("stage_outputs") or {}).get("creator")) or {})


def credit_line(creator: dict, source_key: str) -> str:
    """The credit every organic post carries. Built by code, never left to the model: it is what the
    creator gets out of being clipped, and the reason independent creators welcome it."""
    vid = source_key.split(":", 1)[1] if ":" in source_key else ""
    link = f"https://youtu.be/{vid}" if vid else creator.get("url", "")
    return f"🎥 Credit: {creator.get('name') or 'the original creator'} · full video: {link}".strip()


def caption_for(clip: dict, campaign: Any) -> str:
    caption = clip.get("caption") or ""
    if not getattr(campaign, "is_organic", False):
        return caption
    source = ((clip.get("stage_outputs") or {}).get("source") or {}).get("key", "")
    return f"{caption}\n\n{credit_line(creator_of(clip), source)}"


def gate(clip: dict, campaign: Any) -> str | None:
    """None = pass; else the reason it is blocked. Every check fails closed."""
    if getattr(campaign, "is_organic", False):
        return _organic_gate(clip)
    caption = clip.get("caption") or ""
    present = {t.lower() for t in re.findall(r"#\w+", caption)}
    missing = [h for h in campaign.required_hashtags if h.lower() not in present]
    if missing:
        return f"caption is missing required hashtags {missing}"
    source = ((clip.get("stage_outputs") or {}).get("source") or {}).get("key", "")
    if not campaign.allows_source(source):
        return f"source {source or '(unknown)'} is not on the campaign's allowed list"
    terms = subject_terms(campaign.subject)
    so = clip.get("stage_outputs") or {}
    words = {w.lower() for w in _WORD.findall(clip_text(so, clip["start_s"]))}
    if terms and not (terms & words) and not guest_is_subject_speaking(so, clip, terms):
        return f"clip transcript never mentions the campaign subject ({campaign.subject})"
    return None


def _organic_gate(clip: dict) -> str | None:
    """Organic clips come from discovery, not a provided list, so the checks are about provenance:
    we must know WHO made it (to credit them and to honour the blocklist) and WHICH video it is."""
    creator = creator_of(clip)
    if not creator.get("channel_id") or not creator.get("name"):
        return "organic clip has no discovered creator to credit"
    source = ((clip.get("stage_outputs") or {}).get("source") or {}).get("key", "")
    if not source.startswith("youtube:"):
        return f"organic clip has no YouTube source ({source or 'unknown'})"
    return None


def guest_is_subject_speaking(stage_outputs: dict, clip: dict, terms: set[str], *,
                              max_host_share: float = 0.4) -> bool:
    """True when the episode's identified guest IS the campaign subject and the guest is the one
    talking in this window.

    Why (2026-09-25): Anton Osika, on screen for a full minute explaining Lovable's mission, said
    "we built…" and never "Lovable", so the transcript-only check blocked a clip the campaign rules
    ("must show Anton or Lovable") plainly allow. The worker's structure pass already names the guest
    and the host's speaking ranges; the guest counts only if the host holds at most
    `max_host_share` of the window, so a host's setup about the guest still fails."""
    st = (stage_outputs or {}).get("structure") or {}
    guest = {w.lower() for w in _WORD.findall(st.get("guest") or "")}
    if not terms or not (terms & guest):
        return False
    s, e = float(clip.get("start_s") or 0), float(clip.get("end_s") or 0)
    if e <= s:
        return False
    host = sum(max(0.0, min(e, float(b)) - max(s, float(a))) for a, b in (st.get("host_ranges") or []))
    return host / (e - s) <= max_host_share


def pick_for(stage_outputs: dict, start_s: float) -> dict:
    """The worker's pick record for this clip (text, Jev score, pick reason), matched on start time."""
    for p in (stage_outputs or {}).get("picks") or []:
        if abs(float(p.get("start", -1)) - float(start_s)) < 0.5:
            return p
    return {}


def episode_for(brand_id: str, clip: dict, campaign_slug: str, *, results: dict | None = None,
                blocked_reason: str | None = None) -> tuple[str, dict]:
    """(content, metadata) for the brand-memory episode of one clip (CLIPNET-LEARN L1).

    Content is what a human (and the curator) reads; metadata is what the metrics join and the
    learner key on. Written for published AND blocked clips — a block is a lesson too.
    """
    so = clip.get("stage_outputs") or {}
    pick = pick_for(so, clip["start_s"])
    source = (so.get("source") or {}).get("key", "")
    dur = float(clip.get("end_s") or 0) - float(clip.get("start_s") or 0)
    posted = {p: r.get("url") for p, r in (results or {}).items() if r.get("status") == "posted"}
    failed = {p: r.get("error") for p, r in (results or {}).items() if r.get("status") != "posted"}
    if blocked_reason:
        head = f"Clip BLOCKED before posting ({campaign_slug}): {clip.get('hook')!r}. Reason: {blocked_reason}."
    else:
        head = (f"Posted clip ({campaign_slug}) to {', '.join(posted) or 'no platform'}: hook "
                f"{clip.get('hook')!r}, {dur:.0f}s from {source} at {float(clip['start_s']):.0f}s.")
    why = f" Picked because: {pick['why']}" if pick.get("why") else ""
    excerpt = " ".join((pick.get("text") or "").split())[:400]
    content = head + why + (f" Transcript: {excerpt}" if excerpt else "")
    metadata = {
        "capability": "clipnet", "clip_id": str(clip["id"]), "job_id": str(clip.get("job_id") or ""),
        "campaign": campaign_slug, "hook": clip.get("hook"), "source": source,
        "start_s": float(clip["start_s"]), "duration_s": round(dur, 1), "jev": pick.get("jev"),
        "outcome": "blocked" if blocked_reason else "posted", "blocked_reason": blocked_reason,
        "links": posted, "failed": failed,
    }
    return content, metadata


async def _remember(brand_id: str, content: str, metadata: dict) -> bool:
    """Best effort, bounded: the post already happened; memory must never undo or delay it."""
    from meshpilot.agent.memory.store import remember

    try:
        await asyncio.wait_for(remember(brand_id, "episode", content, metadata=metadata,
                                        importance=0.5, source="clipnet"), timeout=30)
        return True
    except Exception as exc:
        log.warning("clipnet.remember_failed", brand_id=brand_id, error=str(exc)[:200])
        return False


def youtube_title(hook: str, hashtags: tuple[str, ...]) -> str:
    return " ".join([hook.strip(), *hashtags, "#shorts"])[:100]


_TAG = re.compile(r"#\w+")
_X_URL_LEN = 23   # X counts every link as 23 characters
_X_URL = re.compile(r"https?://\S+")


def _x_len(s: str) -> int:
    """X length, counted CONSERVATIVELY so Buffer can never reject it (2026-09-28: 285 raw / 281 weighted was
    refused as "cannot exceed 280"). Every character above U+10FF — emoji, CJK — weighs 2, as on X. A link
    counts at its REAL length but never under 23: X shortens links to 23, but we cannot see whether Buffer
    counts the short or the full form, so we pay for whichever is longer."""
    def w(chunk: str) -> int:
        return sum(1 if ord(c) <= 0x10FF else 2 for c in chunk)
    n, pos = 0, 0
    for m in _X_URL.finditer(s):
        n += w(s[pos:m.start()]) + max(len(m.group()), _X_URL_LEN)
        pos = m.end()
    return n + w(s[pos:])


def _body(caption: str) -> str:
    """The caption without hashtags (the worker appends campaign tags; we place tags per platform)."""
    return re.sub(r"[ \t]+\n", "\n", _TAG.sub("", caption or "")).strip()


def _tags_for(campaign: Any, platform: str, cap: int | None) -> list[str]:
    """Required campaign tags always; then the organic niche tags; capped per platform."""
    required = list(getattr(campaign, "required_hashtags", ()) or ())
    niche = list(((getattr(campaign, "discovery", None) or {}).get("hashtags")) or [])
    tags, seen = [], set()
    for t in ([*required, "#Shorts", *niche] if platform == "youtube" else [*required, *niche]):
        if t.lower() not in seen:
            seen.add(t.lower())
            tags.append(t)
    if cap is not None:
        keep = [t for t in tags if t in required or (platform == "youtube" and t == "#Shorts")]
        tags = keep + [t for t in tags if t not in keep][:max(cap - len(keep), 0)]
    return tags


def platform_caption(clip: dict, campaign: Any, platform: str, prof: dict | None = None) -> str:
    """The caption as THIS platform wants it (PLATFORM-TIMING standard, platform_profile row):
    keyword-led body first, creator credit for organic clips, then the platform's hashtag count."""
    prof = prof or {}
    body = _body(clip.get("caption") or "")
    source = ((clip.get("stage_outputs") or {}).get("source") or {}).get("key", "")
    credit = credit_line(creator_of(clip), source) if getattr(campaign, "is_organic", False) else ""
    tags = _tags_for(campaign, platform, prof.get("hashtag_max"))
    if platform == "x":
        tail = " ".join(tags)

        def compose(b: str) -> str:
            return "\n".join(x for x in (b, credit, tail) if x)
        if _x_len(compose(body)) > 280:
            words = body.split(" ")
            while words and _x_len(compose(" ".join(words) + "…")) > 280:
                words.pop()
            body = " ".join(words) + "…" if words else ""
        return compose(body)
    parts = [body] + ([credit] if credit else []) + ([" ".join(tags)] if tags else [])
    text = "\n\n".join(parts)
    limit = prof.get("max_chars") if platform != "youtube" else None   # youtube max_chars is the TITLE limit
    return text[:limit] if limit else text


async def _post_one(platform: str, brand_id: str, clip: dict, campaign: Any) -> tuple[str | None, str | None]:
    """(external_id, permalink). Buffer posts resolve their permalink by polling for up to ~2 min."""
    from meshpilot.platforms import buffer
    from meshpilot.platforms.facebook import publish_facebook
    from meshpilot.platforms.instagram import publish_instagram

    url, cap = clip["media_url"], clip["caption"]
    if platform == "instagram":
        return await publish_instagram(brand_id=brand_id, caption=cap, video_url=url)
    if platform == "facebook":
        return await publish_facebook(brand_id=brand_id, message=cap, video_url=url)
    meta = None
    if platform == "youtube":
        category = str(((getattr(campaign, "discovery", None) or {}).get("youtube_category")) or "28")
        meta = {"youtube": {"title": youtube_title(clip["hook"] or "", campaign.required_hashtags),
                            "categoryId": category, "privacy": "public", "madeForKids": False,
                            "isAiGenerated": False, "notifySubscribers": True}}
    post_id, _status = await buffer.create_post(brand_id, BUFFER_SERVICE[platform], text=cap, media_url=url,
                                                idem_key=f"clipnet:{clip['id']}:{platform}", metadata=meta)
    for _ in range(12):
        _pid, link = await buffer.poll_status_for_post(post_id, brand_id=brand_id)
        if link:
            return post_id, link
        await asyncio.sleep(10)
    return post_id, None


async def _record(brand_id: str, rows: list[dict]) -> str | None:
    """Append the post log to `<PREFIX>_CLIPNET_SHEET_ID` (Sheet1). Best effort: the DB is the record."""
    from meshpilot.config import brand_env
    from meshpilot.integrations.google_sheets import append_row

    sheet_id = brand_env("CLIPNET_SHEET_ID", brand_id)
    if not sheet_id:
        return "no <PREFIX>_CLIPNET_SHEET_ID"
    from meshpilot.integrations.google_sheets import ensure_header

    try:
        await ensure_header(sheet_id, "Sheet1", SHEET_COLUMNS)
        for row in rows:
            await append_row(sheet_id, "Sheet1", SHEET_COLUMNS, row)
    except Exception as exc:  # a sheet failure never undoes a post
        log.warning("clipnet.sheet_failed", brand_id=brand_id, error=str(exc)[:200])
        return str(exc)[:200]
    return None


async def _notify(brand_id: str, content: str) -> bool:
    from meshpilot.agent.clipnet.notify import notify

    return await notify(brand_id, content)


async def _announce(brand_id: str, content: str) -> bool:
    """The "📣 Posted" summary goes to the brand's POSTS channel (#posts-<brand>), the one channel for
    posting alerts. ALERTS-ONE-CHANNEL 2026-09-28: it used to go to #clip-queue while every platform
    publisher ALSO alerted #posts-<brand>, so one clip produced messages in two channels."""
    from meshpilot.comms.post_alerts import announce_text

    return await announce_text(brand_id, content)


async def _post_quietly(post: Any, p: str, brand_id: str, clip: dict, campaign: Any):
    """Publish with the per-platform post alert suppressed — our own summary replaces it."""
    from meshpilot.comms.post_alerts import suppressed

    with suppressed():
        return await post(p, brand_id, clip, campaign)


def _engine_or(engine: Any):
    from meshpilot.db.session import _engine

    return engine or _engine()


async def publish_next(brand_id: str, *, engine: Any = None, post: Any = None, record: Any = None,
                       platforms: tuple[str, ...] | None = None, notify_fn: Any = None,
                       remember_fn: Any = None, announce_fn: Any = None) -> dict:
    from meshpilot.agent.clipnet.campaigns import load_campaign

    if not enabled():
        return {"skipped": "agent_clipnet_enabled / agent_publish_enabled is off"}
    post, record, eng = post or _post_one, record or _record, _engine_or(engine)
    remember_fn = remember_fn or _remember
    platforms = platforms if platforms is not None else configured_platforms(brand_id)
    if not platforms:
        return {"published": None, "reason": "brand has no configured publishing platform"}

    async with eng.begin() as conn:
        row = (await conn.execute(_NEXT_CLIP, {"b": brand_id, "n": len(platforms)})).mappings().first()
        if row is None:
            return {"published": None, "reason": "no rendered clip waiting"}
        clip = dict(row)
        campaign = await load_campaign(clip["campaign"], engine=eng)
        reason = "campaign inactive or missing" if campaign is None else gate(clip, campaign)
        if reason is None and campaign.is_organic:
            blocked = (await conn.execute(_CREATOR_BLOCKED,
                                          {"c": creator_of(clip)["channel_id"]})).scalar()
            if blocked:
                reason = f"creator {creator_of(clip)['name']} is on the blocklist: {blocked}"
        if reason is None:
            clip["caption"] = caption_for(clip, campaign)
        todo: list[str] = []
        if reason:
            await conn.execute(_BLOCK, {"id": clip["id"], "r": reason})
        else:
            await conn.execute(_PASS, {"id": clip["id"]})
            ledger = {r.platform: r.status for r in (await conn.execute(_LEDGER, {"id": clip["id"]})).all()}
            for p in platforms:
                if ledger.get(p) in ("posted", "reserved"):
                    continue
                if (await conn.execute(_RESERVE, {"id": clip["id"], "p": p})).first():
                    todo.append(p)

    if reason:  # notified outside the transaction: a slow Discord call must not hold row locks
        log.warning("clipnet.gate_blocked", brand_id=brand_id, clip_id=str(clip["id"]), reason=reason)
        await (notify_fn or _notify)(brand_id, f"🛑 **Clip blocked, not posted:** {clip['hook']}\n{reason}")
        await remember_fn(brand_id, *episode_for(brand_id, clip, clip["campaign"], blocked_reason=reason))
        return {"published": None, "blocked": str(clip["id"]), "reason": reason}

    results: dict[str, dict] = {}
    for p in todo:
        try:
            ext, link = await _post_quietly(post, p, brand_id, clip, campaign)
            status, err = "posted", None
        except Exception as exc:
            ext, link, status, err = None, None, "failed", f"{type(exc).__name__}: {exc}"[:500]
        async with eng.begin() as conn:
            await conn.execute(_SETTLE, {"id": clip["id"], "p": p, "s": status, "x": ext, "u": link, "e": err})
        results[p] = {"status": status, "url": link, "error": err}
        log.info("clipnet.posted" if status == "posted" else "clipnet.post_failed", brand_id=brand_id,
                 clip_id=str(clip["id"]), platform=p, url=link, error=err)

    now = datetime.now(UTC)
    submit_by = (now.timestamp() + campaign.submit_window_min * 60)
    sheet_err = await record(brand_id, [{
        "posted_at": now.isoformat(timespec="seconds"), "brand": brand_id, "campaign": campaign.slug,
        "platform": p, "status": r["status"], "url": r["url"] or r["error"] or "", "hook": clip["hook"],
        "caption": clip["caption"], "source": (clip["stage_outputs"] or {}).get("source", {}).get("key", ""),
        "clip_url": clip["media_url"],
        "submit_by": datetime.fromtimestamp(submit_by, UTC).isoformat(timespec="seconds")
        if p in campaign.submit_platforms else "",
    } for p, r in results.items()])
    from meshpilot.agent.clipnet.notify import published_message

    submit_by_local = datetime.fromtimestamp(submit_by, UTC).strftime("%H:%M UTC")
    await (announce_fn or _announce)(brand_id, published_message(clip["hook"] or "", results,
                                                             campaign.submit_platforms, submit_by_local))
    remembered = await remember_fn(brand_id, *episode_for(brand_id, clip, campaign.slug, results=results))
    return {"published": str(clip["id"]), "hook": clip["hook"], "results": results, "remembered": remembered,
            "submit_to_whop": {p: results[p]["url"] for p in campaign.submit_platforms if p in results},
            "sheet_error": sheet_err}


# ── PLATFORM-TIMING: each platform posts on its own best-time slots, ≥ min_gap_hours apart ──────────
_NEXT_FOR_PLATFORM = text(
    "SELECT c.id, c.job_id, c.start_s, c.end_s, c.hook, c.caption, c.media_url, "
    "j.campaign, j.stage_outputs "
    "FROM clipnet_clip c JOIN clipnet_job j ON j.id = c.job_id "
    "WHERE j.brand_id = :b AND c.gate_status <> 'blocked' AND c.media_url IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM clipnet_post p WHERE p.clip_id = c.id AND p.platform = :p "
    "                AND p.status IN ('posted','reserved')) "
    "ORDER BY c.created_at LIMIT 1 FOR UPDATE OF c SKIP LOCKED"
)
_LAST_ON_PLATFORM = text(
    "SELECT max(p.updated_at) FROM clipnet_post p JOIN clipnet_clip c ON c.id = p.clip_id "
    "JOIN clipnet_job j ON j.id = c.job_id "
    "WHERE j.brand_id = :b AND p.platform = :p AND p.status IN ('posted','reserved')"
)
_FIRST_POST_OF_CLIP = text("SELECT count(*) FROM clipnet_post WHERE clip_id=:id AND status='posted'")


async def publish_due(brand_id: str, *, engine: Any = None, post: Any = None, record: Any = None,
                      platforms: tuple[str, ...] | None = None, profile_fn: Any = None,
                      notify_fn: Any = None, remember_fn: Any = None, now: datetime | None = None,
                      announce_fn: Any = None, reconcile_fn: Any = None) -> dict:
    """Post, on every platform whose best-time slot is open NOW, that platform's next clip.

    Platforms are independent: each has its own slots (platform_profile.post_times in post_tz), its
    own minimum gap since its last post (min_gap_hours), its own queue (the oldest clip not yet on
    it) and its own caption (platform_caption). Runs every ~10 minutes; a slot stays open 25
    minutes and the gap check makes a second tick in the same slot a no-op."""
    from meshpilot.agent.clipnet.campaigns import load_campaign
    from meshpilot.agent.social import platforms_kb, timing

    if not enabled():
        return {"skipped": "agent_clipnet_enabled / agent_publish_enabled is off"}
    post, record, eng = post or _post_one, record or _record, _engine_or(engine)
    remember_fn, profile_fn = remember_fn or _remember, profile_fn or platforms_kb.profile
    now = now or datetime.now(UTC)
    out: dict[str, Any] = {}
    # Settle reservations a crashed publish left behind, BEFORE picking new clips (2026-09-28 OOM).
    if reconcile_fn is not False:
        from meshpilot.agent.clipnet.reconcile import reconcile
        try:
            rec = await (reconcile_fn or reconcile)(brand_id, engine=eng)
            if any(rec.values()):
                out["_reconciled"] = rec
        except Exception as exc:  # noqa: BLE001 — never block publishing on the reconcile
            log.warning("clipnet.reconcile_error", brand_id=brand_id, error=str(exc)[:200])
    for p in (platforms if platforms is not None else configured_platforms(brand_id)):
        prof = timing.with_standard(p, await profile_fn(brand_id, p, engine=eng))
        slot = timing.due_slot(list(prof["post_times"]), prof["post_tz"], now)
        if not slot:
            continue
        async with eng.begin() as conn:
            last = (await conn.execute(_LAST_ON_PLATFORM, {"b": brand_id, "p": p})).scalar()
            if not timing.gap_ok(last, now, prof.get("min_gap_hours") or 3):
                out[p] = {"skipped": "inside the minimum gap since the last post", "slot": slot}
                continue
            row = (await conn.execute(_NEXT_FOR_PLATFORM, {"b": brand_id, "p": p})).mappings().first()
            if row is None:
                out[p] = {"skipped": "no clip waiting for this platform", "slot": slot}
                continue
            clip = dict(row)
            campaign = await load_campaign(clip["campaign"], engine=eng)
            reason = "campaign inactive or missing" if campaign is None else gate(clip, campaign)
            if reason is None and campaign.is_organic:
                blocked = (await conn.execute(_CREATOR_BLOCKED, {"c": creator_of(clip)["channel_id"]})).scalar()
                if blocked:
                    reason = f"creator {creator_of(clip)['name']} is on the blocklist: {blocked}"
            if reason:
                await conn.execute(_BLOCK, {"id": clip["id"], "r": reason})
            else:
                await conn.execute(_PASS, {"id": clip["id"]})
                reserved = (await conn.execute(_RESERVE, {"id": clip["id"], "p": p})).first()
        if reason:
            log.warning("clipnet.gate_blocked", brand_id=brand_id, clip_id=str(clip["id"]), reason=reason)
            await (notify_fn or _notify)(brand_id, f"🛑 **Clip blocked, not posted:** {clip['hook']}\n{reason}")
            await remember_fn(brand_id, *episode_for(brand_id, clip, clip["campaign"], blocked_reason=reason))
            out[p] = {"blocked": str(clip["id"]), "reason": reason, "slot": slot}
            continue
        if not reserved:
            out[p] = {"skipped": "another tick reserved this clip", "slot": slot}
            continue
        posted_clip = {**clip, "caption": platform_caption(clip, campaign, p, prof)}
        try:
            ext, link = await _post_quietly(post, p, brand_id, posted_clip, campaign)
            status, err = "posted", None
        except Exception as exc:
            ext, link, status, err = None, None, "failed", f"{type(exc).__name__}: {exc}"[:500]
        async with eng.begin() as conn:
            await conn.execute(_SETTLE, {"id": clip["id"], "p": p, "s": status, "x": ext, "u": link, "e": err})
            first = (await conn.execute(_FIRST_POST_OF_CLIP, {"id": clip["id"]})).scalar() == 1
        result = {"status": status, "url": link, "error": err}
        log.info("clipnet.posted" if status == "posted" else "clipnet.post_failed", brand_id=brand_id,
                 clip_id=str(clip["id"]), platform=p, slot=slot, url=link, error=err)
        sub = p in campaign.submit_platforms
        await record(brand_id, [{
            "posted_at": now.isoformat(timespec="seconds"), "brand": brand_id, "campaign": campaign.slug,
            "platform": p, "status": status, "url": link or err or "", "hook": clip["hook"],
            "caption": posted_clip["caption"], "source": (clip["stage_outputs"] or {}).get("source", {}).get("key", ""),
            "clip_url": clip["media_url"],
            "submit_by": datetime.fromtimestamp(now.timestamp() + campaign.submit_window_min * 60, UTC)
            .isoformat(timespec="seconds") if sub else "",
        }])
        from meshpilot.agent.clipnet.notify import published_message

        await (announce_fn or _announce)(brand_id, published_message(
            clip["hook"] or "", {p: result}, campaign.submit_platforms,
            datetime.fromtimestamp(now.timestamp() + campaign.submit_window_min * 60, UTC).strftime("%H:%M UTC")))
        if first and status == "posted":   # one brand-memory episode per clip, not per platform
            await remember_fn(brand_id, *episode_for(brand_id, clip, campaign.slug, results={p: result}))
        out[p] = {"clip": str(clip["id"]), "slot": slot, **result}
    return out
