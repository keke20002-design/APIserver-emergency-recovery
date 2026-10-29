-- EPL Odds / Prediction Engine v1 / Model vs Market (plan.md 2차 STEP 3).
-- SQL Editor에서 한 번 실행하세요.
--
-- 모든 확률/차이 값은 기존 /epl/games/{id}/intelligence 응답과 동일하게
-- 0~100 정수 퍼센트로 저장한다 (0~1 소수 아님) — Flutter가 이미 이 단위로
-- 파싱하고 있어 단위를 통일해 혼동을 없앤다.

create table if not exists match_odds (
  id          bigserial primary key,
  match_id    text        not null,          -- Naver game_id (Flutter Match.id)
  home_team   text        not null,          -- English team name
  away_team   text        not null,
  match_date  date        not null,          -- KST 경기일 (Flutter Match.apiDate)
  provider    text        not null default 'api-football',
  bookmaker   text,                          -- API-Football이 반환한 bookmaker 이름
  home_odds   numeric(6, 2) not null,
  draw_odds   numeric(6, 2) not null,
  away_odds   numeric(6, 2) not null,
  fetched_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (match_id, provider)
);

create index if not exists idx_match_odds_match on match_odds (match_id);


create table if not exists predictions (
  id                   bigserial primary key,
  match_id             text        not null,
  home_team            text        not null,
  away_team            text        not null,
  match_date           date        not null,
  model_version        text        not null default 'poisson-v1',
  home_probability     integer     not null,   -- 0~100
  draw_probability     integer     not null,
  away_probability     integer     not null,
  expected_goals_home  numeric(5, 2),
  expected_goals_away  numeric(5, 2),
  home_matches_sample  integer,
  away_matches_sample  integer,
  created_at           timestamptz not null default now(),
  unique (match_id, model_version)
);

create index if not exists idx_predictions_match on predictions (match_id);


create table if not exists model_vs_market (
  id               bigserial primary key,
  match_id         text        not null,
  model_version    text        not null,
  odds_provider    text        not null,
  home_difference  integer     not null,   -- model% - market%, signed (%p)
  draw_difference  integer     not null,
  away_difference  integer     not null,
  calculated_at    timestamptz not null default now(),
  unique (match_id, model_version, odds_provider)
);

create index if not exists idx_model_vs_market_match on model_vs_market (match_id);


-- RLS: .env의 SUPABASE_KEY가 sb_publishable_... (구 anon 키와 동급 — RLS를
-- 그대로 적용받는다) 이므로, RLS를 꺼두면 이 키를 아는 누구나 PostgREST로
-- 이 테이블들을 직접 읽고 쓸 수 있다. 서버가 지금 이 키로 정상 동작해야
-- 하니 우선 "전체 허용" 정책으로 RLS를 켜서 최소한 Supabase의 기본 보안
-- 경고는 없애 둔다 (지금 동작과 기능적으로는 동일함, 진짜 방어는 아님).
-- 근본적인 해결책은 서버가 sb_secret_... 키(RLS를 완전히 우회)를 쓰도록
-- 바꾸는 것 — 이건 다른 기존 테이블(stadium_updates 등)에도 동일하게
-- 해당되는 별개의 이슈라 이번 작업 범위 밖으로 남겨둔다.
alter table match_odds enable row level security;
alter table predictions enable row level security;
alter table model_vs_market enable row level security;

drop policy if exists "match_odds_service_access" on match_odds;
create policy "match_odds_service_access" on match_odds
  for all using (true) with check (true);

drop policy if exists "predictions_service_access" on predictions;
create policy "predictions_service_access" on predictions
  for all using (true) with check (true);

drop policy if exists "model_vs_market_service_access" on model_vs_market;
create policy "model_vs_market_service_access" on model_vs_market
  for all using (true) with check (true);
