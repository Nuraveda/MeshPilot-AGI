"""CLIPNET campaign record — the rules a clip must satisfy (spec § 3.3, § 6).

A campaign decides what a clip must be ABOUT, which sources it may come from, which hashtags every
post carries, and which platforms are submitted for payment. WHERE a clip is posted is the brand's
choice, not the campaign's (spec § 3.3), so nothing here restricts publishing platforms.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text

log = structlog.get_logger(__name__)

_SELECT = text(
    "SELECT slug, brand_ids, subject, required_hashtags, allowed_sources, submit_platforms, "
    "disclosure, submit_window_min, max_clips_per_day, active, kind, discovery "
    "FROM clipnet_campaign WHERE slug = :slug"
)


def _tup(v: Any) -> tuple[str, ...]:
    return tuple(v or ())


def _json(v: Any) -> dict:
    if isinstance(v, str):
        import json

        return json.loads(v or "{}")
    return dict(v or {})


@dataclass(frozen=True)
class Campaign:
    slug: str
    brand_ids: tuple[str, ...]
    subject: str
    required_hashtags: tuple[str, ...]
    allowed_sources: tuple[str, ...]
    submit_platforms: tuple[str, ...]
    disclosure: str | None
    submit_window_min: int
    max_clips_per_day: int
    active: bool
    # 'campaign' = a paid Content Rewards campaign (provided sources, subject gate);
    # 'organic'  = the brand's own growth from discovered independent creators (CLIPNET-DISCOVER).
    kind: str = "campaign"
    discovery: Mapping[str, Any] | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Campaign:
        return cls(
            slug=row["slug"],
            brand_ids=_tup(row.get("brand_ids")),
            subject=row["subject"],
            required_hashtags=_tup(row.get("required_hashtags")),
            allowed_sources=_tup(row.get("allowed_sources")),
            submit_platforms=_tup(row.get("submit_platforms")),
            disclosure=row.get("disclosure"),
            submit_window_min=int(row.get("submit_window_min") or 10),
            max_clips_per_day=int(row.get("max_clips_per_day") or 5),
            active=bool(row.get("active")),
            kind=row.get("kind") or "campaign",
            discovery=_json(row.get("discovery")),
        )

    @property
    def is_organic(self) -> bool:
        return self.kind == "organic"

    def allows_source(self, source_key: str) -> bool:
        """Exact match on '<platform>:<id>'. An empty allow-list allows nothing (fail closed)."""
        return source_key in self.allowed_sources

    def default_brand(self) -> str | None:
        return self.brand_ids[0] if len(self.brand_ids) == 1 else None


def _engine_or(engine: Any):
    from meshpilot.db.session import _engine

    return engine or _engine()


async def load_campaign(slug: str, *, engine: Any = None) -> Campaign | None:
    """The ACTIVE campaign for `slug`, or None. Inactive is treated as absent on purpose."""
    async with _engine_or(engine).connect() as conn:
        row = (await conn.execute(_SELECT, {"slug": slug})).mappings().first()
    if row is None:
        return None
    campaign = Campaign.from_row(row)
    if not campaign.active:
        log.info("clipnet.campaign_inactive", slug=slug)
        return None
    return campaign
