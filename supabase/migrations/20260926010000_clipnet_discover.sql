-- CLIPNET-DISCOVER — organic growth: the agent finds trending videos from INDEPENDENT creators in each
-- brand's niche and clips them, credited, until the channels have traction for paid campaigns.
-- Spec: docs/plans/2026-09-26-clipnet-discover.md. Additive only.

-- A campaign is now one of two kinds:
--   'campaign' — a paid Content Rewards campaign: provided sources only, subject + hashtag gates (unchanged)
--   'organic'  — the brand's own growth: sources come from discovery, every post credits the creator
alter table clipnet_campaign add column if not exists kind text not null default 'campaign';
alter table clipnet_campaign drop constraint if exists clipnet_campaign_kind_check;
alter table clipnet_campaign add constraint clipnet_campaign_kind_check check (kind in ('campaign', 'organic'));
-- Discovery config for organic campaigns: {queries[], per_day, min_subs, max_subs, min_minutes,
-- max_minutes, lookback_days, video_duration}. Ignored for kind='campaign'.
alter table clipnet_campaign add column if not exists discovery jsonb not null default '{}'::jsonb;

-- Creators we never clip again: they objected, or a post drew a claim. Keyed on the YouTube channel id.
create table if not exists clipnet_creator_block (
  channel_id    text primary key,
  channel_name  text,
  reason        text not null,
  created_at    timestamptz not null default now()
);
alter table clipnet_creator_block enable row level security;

insert into clipnet_campaign (slug, brand_ids, subject, kind, discovery, max_clips_per_day) values
  ('organic-ai-empire', array['ai_empire'],
   'AI, startups, entrepreneurship, business or personal finance: a concrete insight, story or opinion a viewer learns from',
   'organic',
   '{"queries": ["AI startup founder podcast", "AI tools business podcast", "entrepreneur interview podcast", "startup founder story podcast", "personal finance podcast"],
     "per_day": 2, "min_subs": 10000, "max_subs": 1000000, "min_minutes": 15, "max_minutes": 90,
     "lookback_days": 30, "video_duration": "long"}'::jsonb, 6),
  ('organic-entertainment-vault', array['entertainment_vault'],
   'Entertainment: a funny, surprising, emotional or dramatic moment from an independent talk show, comedy podcast or storytime that works on its own',
   'organic',
   '{"queries": ["comedy podcast full episode", "celebrity interview podcast", "storytime podcast", "funny stories podcast"],
     "per_day": 2, "min_subs": 10000, "max_subs": 1000000, "min_minutes": 15, "max_minutes": 90,
     "lookback_days": 30, "video_duration": "long"}'::jsonb, 6),
  ('organic-hypedrop-gaming', array['hypedrop_gaming'],
   'Gaming: a hype, clutch, funny or rage moment from a stream or gameplay video that works on its own',
   'organic',
   '{"queries": ["gaming podcast episode", "lets play funny", "minecraft survival series", "fortnite funny gameplay", "horror game playthrough funny", "gaming stream full vod"],
     "per_day": 2, "min_subs": 10000, "max_subs": 1000000, "min_minutes": 10, "max_minutes": 90,
     "lookback_days": 14, "video_duration": "any"}'::jsonb, 6)
on conflict (slug) do nothing;
