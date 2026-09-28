"""CLIPNET-DISCOVER — find trending videos from INDEPENDENT creators in a brand's niche and queue them.

Spec: docs/plans/2026-09-26-clipnet-discover.md. Operator decision (2026-09-26): the channels are too
new for paid campaigns' strict rules, so each brand first grows on clips of independent creators who
WANT to be clipped (they get the reach; every post credits them). Production channels (studios,
networks, labels, big media) are excluded, and so is anything music-led, since Content ID claims a
song even inside an indie creator's video.

One run per brand per day (capability `clipnet_discover`): YouTube Data API search per niche query,
then videos.list + channels.list for the facts a search result does not carry (views, duration,
category, subscriber count). `eligible()` is pure and holds every rule; `rank()` orders what is left
by views per day. The top `per_day` from distinct creators are queued as ordinary clipnet_jobs, with
the creator recorded in stage_outputs so publishing can credit them and honour the blocklist.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text

log = structlog.get_logger(__name__)

API = "https://www.googleapis.com/youtube/v3"
MUSIC_CATEGORY = "10"
CREATOR_COOLDOWN_DAYS = 14
# A channel whose NAME says it is an outlet, not a person. Deliberately blunt: a false positive costs
# one candidate, a false negative can cost a copyright strike.
_PRODUCTION = re.compile(
    r"(\b(official|vevo|tv|network|studios?|records|recordings|entertainment|news|media|"
    r"broadcast|pictures|films?|productions?)\b|\s-\s*topic$)", re.I)
# Titles that announce music: Content ID matches the recording even inside a gaming or meme video
# (measured 2026-09-26: a VRChat "dancing meme" set to a chart song passed the model screen).
_MUSIC_TITLE = re.compile(r"\b(song|songs|lyrics?|remix|cover|dance|dancing|music|karaoke|feat\.|ft\.|"
                          r"official (music )?video|concert|album)\b", re.I)
_ISO_DUR = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def iso_minutes(d: str) -> float:
    m = _ISO_DUR.fullmatch(d or "")
    if not m:
        return 0.0
    days, h, mins, s = (int(x or 0) for x in m.groups())
    return days * 1440 + h * 60 + mins + s / 60


def is_production_channel(title: str) -> bool:
    return bool(_PRODUCTION.search(title or ""))


def mostly_non_latin(s: str) -> bool:
    """True when most letters are outside the Latin script — a language tag is often missing
    (measured 2026-09-26: Kannada and Korean titles arrived with no defaultAudioLanguage)."""
    letters = [c for c in s if c.isalpha()]
    return bool(letters) and sum(ord(c) > 0x24F for c in letters) / len(letters) > 0.3


def eligible(video: dict, channel: dict, cfg: dict, *, blocked: set[str], recent_creators: set[str],
             known_videos: set[str]) -> str | None:
    """None = a candidate; else why not. Every rule fails closed on missing data."""
    vid = video.get("id", "")
    sn, st, cd = video.get("snippet") or {}, video.get("statistics") or {}, video.get("contentDetails") or {}
    cid = sn.get("channelId", "")
    if vid in known_videos:
        return "already queued"
    if cid in blocked:
        return "creator blocked"
    if cid in recent_creators:
        return f"creator clipped in the last {CREATOR_COOLDOWN_DAYS} days"
    if sn.get("liveBroadcastContent", "none") != "none":
        return "live or upcoming"
    if sn.get("categoryId") == MUSIC_CATEGORY:
        return "music category (Content ID risk)"
    if _MUSIC_TITLE.search(sn.get("title", "")):
        return "music in the title (Content ID risk)"
    lang = (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "en").lower()
    if not lang.startswith("en"):
        return f"not English ({lang})"
    if mostly_non_latin(sn.get("title", "")) or mostly_non_latin((channel.get("snippet") or {}).get("title", "")):
        return "not English (script)"
    mins = iso_minutes(cd.get("duration", ""))
    if not (float(cfg.get("min_minutes", 15)) <= mins <= float(cfg.get("max_minutes", 90))):
        return f"length {mins:.0f} min outside range"
    ch_sn, ch_st = channel.get("snippet") or {}, channel.get("statistics") or {}
    if is_production_channel(ch_sn.get("title", "")):
        return f"production channel ({ch_sn.get('title')})"
    if ch_st.get("hiddenSubscriberCount") or "subscriberCount" not in ch_st:
        return "subscriber count hidden"
    subs = int(ch_st["subscriberCount"])
    if not (int(cfg.get("min_subs", 10_000)) <= subs <= int(cfg.get("max_subs", 1_000_000))):
        return f"{subs} subscribers outside the independent-creator range"
    if int(st.get("viewCount") or 0) <= 0:
        return "no views"
    return None


def views_per_day(video: dict, now: datetime) -> float:
    published = datetime.fromisoformat(video["snippet"]["publishedAt"].replace("Z", "+00:00"))
    days = max((now - published).total_seconds() / 86400, 0.25)
    return int(video["statistics"].get("viewCount") or 0) / days


def rank(cands: list[dict], now: datetime, n: int, boost: dict[str, float] | None = None) -> list[dict]:
    """Top `n` by views/day × the creator's learned boost (CLIPNET-LEARN L3, bounded 0.5–2×; 1.0
    for creators we have too little data on), at most one video per creator."""
    boost = boost or {}
    out, seen = [], set()
    for v in sorted(cands, key=lambda v: views_per_day(v, now) * boost.get(v["snippet"]["channelId"], 1.0),
                    reverse=True):
        cid = v["snippet"]["channelId"]
        if cid in seen:
            continue
        seen.add(cid)
        out.append(v)
        if len(out) >= n:
            break
    return out


# ── relevance + independence screen (one LLM call per brand per day) ─────────────────────────────
SCREEN_POOL = 15
# ROUTER-LUNA (2026-09-28): was a hardcoded anthropic/claude-sonnet-5 the router could not reach.
# Now a named default with an env override, matching the clip picker. CLIPNET_SCREEN_MODEL overrides.
SCREEN_MODEL = "openai/gpt-6-luna-pro"

_SCREEN_PROMPT = """You screen YouTube videos for a short-form clip channel. Keep a video ONLY if ALL hold:
1. The video's MAIN topic is this niche (judge strictly; a guest's side remark does not count): {subject}
2. It is spoken in English.
3. It is made by an INDEPENDENT creator or independent podcast. REJECT anything produced or owned by a
   TV network, broadcaster, film/TV studio, record label, news or media company, podcast network or
   company (e.g. Spotify Studios, The Ringer, iHeart, Wondery, SiriusXM), a sports league, federation,
   team or official event channel, or a celebrity show made by a production company.
4. It is not music-led (songs, concerts, music reactions) — music draws copyright claims.
5. The channel OWNS the footage: its own episode, interview or stream. REJECT compilations and
   re-uploads of other people's content ("funniest moments", "clips of the week", "best of",
   highlight reels of other streamers), and reactions or watch-alongs of someone else's video,
   a movie, a TV show or a music video (their footage is studio-owned and Content ID-matched).
6. It is brand-safe: REJECT trauma, abuse, self-harm, death, tragedy, trigger-warning content,
   hate, sexual content, and graphic violence.
When unsure about 3, 4, 5 or 6, reject.

Videos (id | channel | subscribers | title | description start):
{listing}

Return ONLY JSON: {{"keep": ["<id>", ...], "rejected": {{"<id>": "<short reason>"}}}}"""


def parse_screen(raw: str, valid: set[str]) -> list[str]:
    import json

    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return []
    try:
        keep = json.loads(m.group(0)).get("keep") or []
    except ValueError:
        return []
    return [k for k in dict.fromkeys(keep) if k in valid]


async def screen(http, cands: list[dict], channels: dict[str, dict], subject: str,
                 guidance: str = "") -> list[dict]:
    """Candidates the model judges on-niche, English, independent and not music-led — in the
    order given. Fails CLOSED: any error returns [] and nothing is queued that day."""
    import os

    if not cands:
        return []
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()   # same source as agent/loop/llm.py
    if not key:
        log.warning("clipnet.discover_screen_skipped", reason="no OPENROUTER_API_KEY")
        return []
    listing = "\n".join(
        f'{v["id"]} | {channels[v["snippet"]["channelId"]]["snippet"]["title"]} | '
        f'{channels[v["snippet"]["channelId"]]["statistics"].get("subscriberCount")} | {v["snippet"].get("title", "")} | '
        f'{(v["snippet"].get("description") or "")[:200].replace(chr(10), " ")}' for v in cands)
    try:
        r = await http.post("https://openrouter.ai/api/v1/chat/completions", timeout=90,
                            headers={"Authorization": f"Bearer {key}"},
                            json={"model": os.environ.get("CLIPNET_SCREEN_MODEL", SCREEN_MODEL), "temperature": 0,
                                  "messages": [{"role": "user", "content": _SCREEN_PROMPT.format(
                                      subject=subject, listing=listing) + (
                                      "\n\nWhat this brand knows and has learned (prefer videos that fit it; "
                                      "it never relaxes rules 1-6):\n" + guidance if guidance else "")}]})
        r.raise_for_status()
        keep = parse_screen(r.json()["choices"][0]["message"]["content"], {v["id"] for v in cands})
    except Exception as e:
        log.warning("clipnet.discover_screen_failed", error=str(e)[:200])
        return []
    by_id = {v["id"]: v for v in cands}
    return [by_id[k] for k in keep]


# ── YouTube Data API ──────────────────────────────────────────────────────────────────────────────
async def _get(http, path: str, params: dict) -> dict:
    r = await http.get(f"{API}/{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


async def search(http, key: str, cfg: dict, now: datetime) -> tuple[list[dict], dict[str, dict]]:
    """Candidate videos (full resources) + their channels, for every query in the config."""
    after = (now - timedelta(days=int(cfg.get("lookback_days", 30)))).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids: list[str] = []
    found_by: dict[str, str] = {}
    for q in cfg.get("queries") or []:
        res = await _get(http, "search", {
            "part": "snippet", "type": "video", "q": q, "order": "viewCount", "publishedAfter": after,
            "maxResults": 25, "relevanceLanguage": "en", "regionCode": "US", "safeSearch": "moderate",
            "videoDuration": cfg.get("video_duration", "any"), "key": key})
        for it in res.get("items") or []:
            vid = it.get("id", {}).get("videoId")
            if vid:
                ids.append(vid)
                found_by.setdefault(vid, q)
    ids = list(dict.fromkeys(ids))
    videos: list[dict] = []
    for i in range(0, len(ids), 50):
        res = await _get(http, "videos", {"part": "snippet,statistics,contentDetails",
                                          "id": ",".join(ids[i:i + 50]), "key": key})
        videos += res.get("items") or []
    for v in videos:
        v["_query"] = found_by.get(v["id"])
    cids = list(dict.fromkeys(v["snippet"]["channelId"] for v in videos))
    channels: dict[str, dict] = {}
    for i in range(0, len(cids), 50):
        res = await _get(http, "channels", {"part": "snippet,statistics", "id": ",".join(cids[i:i + 50]),
                                            "key": key})
        channels.update({c["id"]: c for c in res.get("items") or []})
    return videos, channels


# ── the capability ────────────────────────────────────────────────────────────────────────────────
_CAMPAIGNS = text("SELECT slug FROM clipnet_campaign WHERE kind='organic' AND active AND :b = ANY(brand_ids)")
# "Today" = since local midnight, not the last 24 h: a rolling window let an evening run use up the
# NEXT morning's run as well, so the brand lost a day of discovery (found 2026-09-26).
DAY_TZ = "America/Toronto"
_QUEUED_TODAY = text(
    "SELECT count(*) FROM clipnet_job WHERE campaign=:c "
    "AND created_at >= (date_trunc('day', now() AT TIME ZONE :tz) AT TIME ZONE :tz)")
_KNOWN = text("SELECT split_part(idem_key, ':', 1) FROM clipnet_job")
_RECENT = text("SELECT DISTINCT stage_outputs->'creator'->>'channel_id' FROM clipnet_job "
               "WHERE brand_id=:b AND created_at > now() - make_interval(days => :d) "
               "AND stage_outputs ? 'creator'")
_BLOCKED = text("SELECT channel_id FROM clipnet_creator_block")
_INSERT = text(
    "INSERT INTO clipnet_job (idem_key, brand_id, campaign, url, stage_outputs) "
    "VALUES (:k, :b, :c, :u, CAST(:so AS jsonb)) ON CONFLICT (idem_key) DO NOTHING RETURNING id")


def _engine_or(engine: Any):
    from meshpilot.db.session import _engine

    return engine or _engine()


def _api_key() -> str:
    from meshpilot.config import settings

    return getattr(settings(), "youtube_api_key", "") or ""


async def discover(brand_id: str, *, engine: Any = None, http: Any = None, now: datetime | None = None) -> dict:
    import json

    from meshpilot.agent.clipnet.campaigns import load_campaign
    from meshpilot.agent.clipnet.publish import enabled

    if not enabled():
        return {"skipped": "agent_clipnet_enabled / agent_publish_enabled is off"}
    key = _api_key()
    if not key:
        return {"skipped": "YOUTUBE_API_KEY is not set"}
    eng, now = _engine_or(engine), now or datetime.now(UTC)
    async with eng.connect() as conn:
        slugs = [r[0] for r in (await conn.execute(_CAMPAIGNS, {"b": brand_id})).all()]
        known = {r[0] for r in (await conn.execute(_KNOWN)).all()}
        recent = {r[0] for r in (await conn.execute(_RECENT, {"b": brand_id, "d": CREATOR_COOLDOWN_DAYS})).all() if r[0]}
        blocked = {r[0] for r in (await conn.execute(_BLOCKED)).all()}
    if not slugs:
        return {"queued": [], "reason": "brand has no active organic campaign"}

    close = http is None
    if http is None:
        import httpx

        http = httpx.AsyncClient()
    from meshpilot.agent.clipnet import learn

    scored = await learn.scored_clips(brand_id, engine=eng)
    boost = learn.creator_multipliers(scored)
    brand_guidance = await learn.guidance(brand_id, engine=eng, scored=scored)
    report: dict[str, Any] = {"queued": [], "rejected": {}, "boosted_creators": len(boost)}
    try:
        for slug in slugs:
            campaign = await load_campaign(slug, engine=eng)
            cfg = dict(campaign.discovery or {}) if campaign else {}
            async with eng.connect() as conn:
                room = int(cfg.get("per_day", 2)) - int((await conn.execute(_QUEUED_TODAY, {"c": slug, "tz": DAY_TZ})).scalar() or 0)
            if room <= 0:
                report.setdefault("full", []).append(slug)
                continue
            videos, channels = await search(http, key, cfg, now)
            cands = []
            for v in videos:
                why = eligible(v, channels.get(v["snippet"]["channelId"], {}), cfg,
                               blocked=blocked, recent_creators=recent, known_videos=known)
                if why:
                    report["rejected"][why.split(" (")[0]] = report["rejected"].get(why.split(" (")[0], 0) + 1
                else:
                    cands.append(v)
            pool = rank(cands, now, SCREEN_POOL, boost)
            kept = await screen(http, pool, channels, campaign.subject, brand_guidance)
            report.setdefault("screened", {})[slug] = {"pool": len(pool), "kept": len(kept)}
            for v in rank(kept, now, room, boost):
                sn, ch = v["snippet"], channels[v["snippet"]["channelId"]]
                so = {"creator": {"channel_id": sn["channelId"], "name": ch["snippet"]["title"],
                                  "url": f"https://www.youtube.com/channel/{sn['channelId']}",
                                  "subscribers": int(ch["statistics"]["subscriberCount"])},
                      "discovered": {"title": sn.get("title", ""), "views": int(v["statistics"].get("viewCount") or 0),
                                     "views_per_day": round(views_per_day(v, now)), "at": now.isoformat(timespec="seconds"),
                                     "query": v.get("_query"), "boost": boost.get(sn["channelId"], 1.0)}}
                async with eng.begin() as conn:
                    row = (await conn.execute(_INSERT, {"k": f"{v['id']}:{slug}", "b": brand_id, "c": slug,
                                                        "u": f"https://www.youtube.com/watch?v={v['id']}",
                                                        "so": json.dumps(so)})).first()
                if row:
                    known.add(v["id"])
                    recent.add(sn["channelId"])
                    report["queued"].append({"video": v["id"], "creator": ch["snippet"]["title"],
                                             "title": sn.get("title", "")[:80], "views_per_day": so["discovered"]["views_per_day"]})
            report.setdefault("candidates", {})[slug] = len(cands)
    finally:
        if close:
            await http.aclose()
    log.info("clipnet.discovered", brand_id=brand_id, queued=len(report["queued"]), rejected=report["rejected"])
    return report
