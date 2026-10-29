-- App Monetization Dashboard 테이블.
-- SQL Editor에서 한 번 실행하세요. (USE_MOCK_DATA=false로 전환하기 전에 필요)

create table if not exists apps (
  id           bigserial primary key,
  app_id       text        not null unique,      -- AdMob sync가 자동 발견 시 AdMob App ID(ca-app-pub-...~...) 사용
  app_name     text        not null,
  platform     text        not null default 'android',
  -- AdMob 리포트 API는 정확한 패키지명을 안 준다. 자동 발견 시 NULL로 두고
  -- Settings > App Mapping에서 관리자가 채운다.
  package_name text,
  active       boolean     not null default true,
  created_at   timestamptz not null default now()
);

create table if not exists countries (
  id            bigserial primary key,
  country_code  text not null unique,             -- ISO 3166-1 alpha-2
  country_name  text not null
);

create table if not exists campaigns (
  id            bigserial primary key,
  campaign_id   text        not null unique,       -- Google Ads campaign id
  campaign_name text        not null,
  -- app_id는 Google Ads sync가 신규 캠페인을 처음 발견했을 때는 알 수 없어 NULL로 들어간다.
  -- Settings > App Mapping(plan.md #23, 아직 미구현)에서 관리자가 수동으로 채워준다.
  app_id        text        references apps (app_id),
  active        boolean     not null default true,
  target_cpi    numeric(14, 4),
  daily_budget  numeric(14, 4),
  bidding_strategy_goal_type text,
  target_cpi_changed_at timestamptz,
  settings_synced_at timestamptz
);

create index if not exists idx_campaigns_app on campaigns (app_id);

-- Existing installations: safe, additive migration for tCPI analysis.
alter table campaigns add column if not exists target_cpi numeric(14, 4);
alter table campaigns add column if not exists daily_budget numeric(14, 4);
alter table campaigns add column if not exists bidding_strategy_goal_type text;
alter table campaigns add column if not exists target_cpi_changed_at timestamptz;
alter table campaigns add column if not exists settings_synced_at timestamptz;


-- ──────────────────────────────────────────
-- 원본 데이터 (Google Ads / AdMob / GA4)
-- 날짜가 지나도 재동기화되므로(최근 7일 UPSERT) UNIQUE KEY 필수
-- ──────────────────────────────────────────

create table if not exists google_ads_daily (
  id            bigserial primary key,
  date          date        not null,
  account_id    text        not null,
  campaign_id   text        not null,
  campaign_name text        not null,
  app_id        text,       -- 앱 매핑 전에는 NULL (campaigns.app_id 참고)
  country       text        not null,
  currency      text        not null default 'USD',
  impressions   bigint      not null default 0,
  clicks        bigint      not null default 0,
  cost          numeric(14, 4) not null default 0,
  conversions   bigint      not null default 0,
  installs      bigint      not null default 0,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (date, campaign_id, country)
);

create index if not exists idx_google_ads_daily_app_date on google_ads_daily (app_id, date);
create index if not exists idx_google_ads_daily_country_date on google_ads_daily (country, date);

create table if not exists admob_daily (
  id                 bigserial primary key,
  date               date        not null,
  app_id             text        not null,
  app_name           text        not null,
  ad_unit_id         text        not null,
  country            text        not null,
  currency           text        not null default 'USD',
  ad_requests        bigint      not null default 0,
  matched_requests   bigint      not null default 0,
  impressions        bigint      not null default 0,
  clicks             bigint      not null default 0,
  estimated_earnings numeric(14, 4) not null default 0,
  ecpm               numeric(14, 4) not null default 0,
  match_rate         numeric(6, 4)  not null default 0,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  unique (date, app_id, ad_unit_id, country)
);

create index if not exists idx_admob_daily_app_date on admob_daily (app_id, date);
create index if not exists idx_admob_daily_country_date on admob_daily (country, date);

create table if not exists analytics_daily (
  id             bigserial primary key,
  date           date        not null,
  app_id         text        not null,
  country        text        not null,
  new_users      bigint      not null default 0,
  active_users   bigint      not null default 0,
  sessions       bigint      not null default 0,
  retention_d1   numeric(6, 4) not null default 0,
  retention_d7   numeric(6, 4) not null default 0,
  retention_d30  numeric(6, 4) not null default 0,
  ad_revenue     numeric(14, 4) not null default 0,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (date, app_id, country)
);

create index if not exists idx_analytics_daily_app_date on analytics_daily (app_id, date);


-- ──────────────────────────────────────────
-- Settings / Sync 상태
-- ──────────────────────────────────────────

create table if not exists dashboard_settings (
  id                bigint primary key default 1,
  base_currency     text    not null default 'USD',
  timezone          text    not null default 'Asia/Seoul',
  target_roas_pct   numeric(6, 2) not null default 100,
  roas_scale_min    numeric(6, 2) not null default 120,
  roas_watch_min    numeric(6, 2) not null default 80,
  roas_optimize_min numeric(6, 2) not null default 50,
  constraint single_row check (id = 1)
);

insert into dashboard_settings (id) values (1) on conflict (id) do nothing;

create table if not exists dashboard_sync_status (
  source         text primary key,   -- google_ads | admob | analytics
  status         text not null,      -- success | error | running
  message        text,
  last_synced_at timestamptz
);
