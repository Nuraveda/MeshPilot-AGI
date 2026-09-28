-- PLATFORM-TIMING — per-platform posting slots, gaps and hashtag caps become part of the platform
-- standard every brand inherits (`_default` rows), overridable per brand. Operator, 2026-09-26:
-- "scheduling should be on best timings per platform and each posting should have at least 3-4
-- hours gap … so it becomes standard for all other channels too."
-- Research + sources: docs/plans/2026-09-26-platform-timing.md. Additive only.

alter table platform_profile add column if not exists post_times    text[] not null default '{}'; -- local 'HH:MM'
alter table platform_profile add column if not exists post_tz       text   not null default 'America/New_York';
alter table platform_profile add column if not exists min_gap_hours real   not null default 3;
alter table platform_profile add column if not exists hashtag_max   int;

update platform_profile set post_times = '{09:00,12:30,19:30}', hashtag_max = 5,
  hashtags = 'Three to five niche tags at the END of the caption; Instagram enforces a hard cap of five (since Dec 2025). Topic tags, not reach-bait.'
  where brand_id = '_default' and platform = 'instagram';
update platform_profile set post_times = '{12:00,16:00,19:30}', hashtag_max = 5,
  hashtags = 'Three to five specific tags that match what people search; the first 100-150 characters of the caption must state the topic keyword.'
  where brand_id = '_default' and platform = 'tiktok';
update platform_profile set post_times = '{09:00,15:00,20:00}', hashtag_max = 3,
  hashtags = 'One to three topical tags at most; the topic and hook must land in the first 125 characters.'
  where brand_id = '_default' and platform = 'facebook';
update platform_profile set post_times = '{08:00,12:00,21:00}', hashtag_max = 1
  where brand_id = '_default' and platform = 'x';

insert into platform_profile (brand_id, platform, audience, register, max_chars, hashtags, avoid,
                              post_times, hashtag_max)
values ('_default', 'youtube',
  'Shorts viewers swiping a vertical feed; most arrive from the Shorts shelf and search, not subscriptions.',
  'Short, specific title (30-40 characters, keyword first); description expands on it in plain words.',
  100,
  '#Shorts plus two to four niche tags in the description; the first three show as links above the title; YouTube ignores tags past fifteen.',
  'Clickbait titles that the clip does not pay off; walls of hashtags; titles over 100 characters.',
  '{11:00,15:00,20:00}', 5)
on conflict (brand_id, platform) do nothing;

-- Organic clip campaigns: niche hashtags (the credit line is added by code) and the YouTube category.
update clipnet_campaign set discovery = discovery || '{"hashtags": ["#AI", "#AITools", "#ArtificialIntelligence", "#AIAgents", "#TechPodcast"], "youtube_category": "28"}'::jsonb
  where slug = 'organic-ai-empire';
update clipnet_campaign set discovery = discovery || '{"hashtags": ["#Podcast", "#PodcastClips", "#Funny", "#Comedy", "#Storytime"], "youtube_category": "24"}'::jsonb
  where slug = 'organic-entertainment-vault';
update clipnet_campaign set discovery = discovery || '{"hashtags": ["#Gaming", "#GamingClips", "#Gamer", "#FunnyMoments", "#Gameplay"], "youtube_category": "20"}'::jsonb
  where slug = 'organic-hypedrop-gaming';
