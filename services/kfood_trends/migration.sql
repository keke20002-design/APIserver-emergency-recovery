-- Run once in Supabase SQL Editor. Server writes use only the service-role key.
create extension if not exists pgcrypto;

create table if not exists public.kfood_trends (
  id uuid primary key default gen_random_uuid(),
  food_key text not null,
  title_ko text not null,
  title_ja text not null,
  category text not null,
  description_ja text not null,
  trend_reason_ja text not null,
  image_url text,
  ingredients_ja jsonb,
  steps_ja jsonb,
  cooking_time_minutes integer check (cooking_time_minutes is null or cooking_time_minutes > 0),
  difficulty text check (difficulty is null or difficulty in ('easy','medium','hard')),
  source_urls jsonb not null default '[]'::jsonb,
  evidence jsonb not null default '[]'::jsonb,
  mention_count integer not null default 0,
  source_count integer not null default 0,
  related_recipe_id text,
  status text not null default 'draft' check (status in ('draft','published','rejected')),
  published_at timestamptz,
  collected_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Upgrade the older manually-curated Korean Pantry table in place when it
-- already exists. These statements are idempotent and preserve existing rows.
alter table public.kfood_trends add column if not exists food_key text;
alter table public.kfood_trends add column if not exists title_ko text;
alter table public.kfood_trends add column if not exists category text default '기타';
alter table public.kfood_trends add column if not exists source_url text; -- legacy compatibility
alter table public.kfood_trends add column if not exists source_urls jsonb default '[]'::jsonb;
alter table public.kfood_trends add column if not exists evidence jsonb default '[]'::jsonb;
alter table public.kfood_trends add column if not exists mention_count integer default 0;
alter table public.kfood_trends add column if not exists source_count integer default 0;
alter table public.kfood_trends add column if not exists cooking_time_minutes integer;
alter table public.kfood_trends add column if not exists difficulty text;
alter table public.kfood_trends add column if not exists collected_at timestamptz default now();
alter table public.kfood_trends add column if not exists updated_at timestamptz default now();

update public.kfood_trends
set title_ko = coalesce(nullif(title_ko, ''), title_ja),
    category = coalesce(nullif(category, ''), '기타'),
    source_urls = case
      when source_urls is null or source_urls = '[]'::jsonb then
        case when source_url is null or source_url = '' then '[]'::jsonb else jsonb_build_array(source_url) end
      else source_urls
    end,
    collected_at = coalesce(collected_at, created_at, now()),
    updated_at = coalesce(updated_at, created_at, now())
where title_ko is null or category is null or source_urls is null or source_urls = '[]'::jsonb
   or collected_at is null or updated_at is null;

update public.kfood_trends
set food_key = encode(digest(lower(regexp_replace(title_ko, '\s+', '', 'g')), 'sha256'), 'hex')
where food_key is null;

alter table public.kfood_trends alter column food_key set not null;
alter table public.kfood_trends alter column title_ko set not null;
alter table public.kfood_trends alter column category set not null;
alter table public.kfood_trends alter column source_urls set not null;
alter table public.kfood_trends alter column evidence set not null;
alter table public.kfood_trends alter column mention_count set not null;
alter table public.kfood_trends alter column source_count set not null;
alter table public.kfood_trends alter column collected_at set not null;
alter table public.kfood_trends alter column updated_at set not null;
alter table public.kfood_trends alter column image_url drop not null;
alter table public.kfood_trends alter column ingredients_ja drop not null;
alter table public.kfood_trends alter column steps_ja drop not null;
alter table public.kfood_trends alter column published_at drop not null;
create index if not exists kfood_trends_status_published_idx on public.kfood_trends(status, published_at desc);
create index if not exists kfood_trends_food_key_created_idx on public.kfood_trends(food_key, created_at desc);

create table if not exists public.kfood_trend_runs (
  id uuid primary key default gen_random_uuid(),
  trigger text not null check (trigger in ('scheduler','manual')),
  status text not null check (status in ('running','success','failed')),
  metrics jsonb not null default '{}'::jsonb,
  error text,
  started_at timestamptz not null default now(),
  finished_at timestamptz
);
create index if not exists kfood_trend_runs_started_idx on public.kfood_trend_runs(started_at desc);
create unique index if not exists kfood_trend_one_running_idx
  on public.kfood_trend_runs ((status)) where status = 'running';

alter table public.kfood_trends enable row level security;
alter table public.kfood_trend_runs enable row level security;
revoke all on public.kfood_trends from anon, authenticated;
revoke all on public.kfood_trend_runs from anon, authenticated;
grant select on public.kfood_trends to anon, authenticated;
create policy "published trends are public" on public.kfood_trends for select
  to anon, authenticated using (status = 'published');

-- Admin review is intentionally performed through the authenticated API server.
-- The service_role bypasses RLS; never ship SUPABASE_SERVICE_ROLE_KEY to Flutter.
