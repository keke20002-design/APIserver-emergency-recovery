-- Supabase: stadium_updates 테이블 생성
-- SQL Editor에서 한 번 실행하세요.

create table if not exists stadium_updates (
  id           bigserial primary key,
  stadium      text        not null,          -- 잠실 / 고척 / 사직 등
  type         text        not null,          -- parking / restaurant / food / traffic / notice / tip
  message      text        not null,          -- 사람이 읽기 좋은 메시지
  score        integer     not null default 50, -- 0~100 혼잡도 점수
  status       text        not null default 'pending', -- pending / approved / rejected
  collected_at timestamptz not null default now(),
  approved_at  timestamptz
);

-- 최신 pending 조회용 인덱스
create index if not exists idx_stadium_updates_status_collected
  on stadium_updates (status, collected_at desc);

-- 중복 체크용 인덱스
create index if not exists idx_stadium_updates_dedup
  on stadium_updates (stadium, type, message, collected_at);

-- 기존 테이블에 컬럼 추가 (이미 생성된 경우 실행)
alter table stadium_updates add column if not exists raw_text   text;
alter table stadium_updates add column if not exists summary    text;
alter table stadium_updates add column if not exists keywords   text;
alter table stadium_updates add column if not exists confidence real    default 0.5;
alter table stadium_updates add column if not exists source     text    default 'naver_blog';


-- ──────────────────────────────────────────
-- 앱이 직접 읽는 테이블 (BaseStadium)
-- approve 시 자동으로 복사됨
-- ──────────────────────────────────────────

create table if not exists stadium_announcements (
  id         bigserial primary key,
  stadium    text        not null,
  message    text        not null,
  active     boolean     not null default true,
  created_at timestamptz not null default now()
);

create table if not exists stadium_food_overrides (
  id           bigserial primary key,
  stadium      text        not null,
  name         text        not null,
  active       boolean     not null default true,
  is_new       boolean     not null default false,
  is_popular   boolean     not null default false,
  reason       text,
  category     text,
  price        text,
  taste        text,
  feature      text,
  location     text,
  wait_minutes integer     not null default 0
);

create table if not exists stadium_parking_overrides (
  id       bigserial primary key,
  stadium  text    not null,
  lot_name text    not null,
  status   text    not null default 'normal', -- good / normal / bad
  note     text,
  active   boolean not null default true
);

create table if not exists stadium_tip_overrides (
  id         bigserial primary key,
  stadium    text    not null,
  tip_text   text    not null,
  active     boolean not null default true,
  sort_order integer not null default 0
);


-- ──────────────────────────────────────────
-- 승인 트리거: stadium_updates.status → 'approved' 시
-- 자동으로 앱 테이블에 복사
-- (Supabase 대시보드 직접 편집 or API 모두 동작)
-- ──────────────────────────────────────────

create or replace function publish_approved_update()
returns trigger as $$
begin
  if new.status = 'approved' and old.status <> 'approved' then

    if new.type = 'parking' then
      insert into stadium_parking_overrides (stadium, lot_name, status, note, active)
      values (
        new.stadium,
        new.message,
        case when new.score >= 68 then 'bad'
             when new.score >= 38 then 'normal'
             else 'good' end,
        coalesce(new.summary, new.message),
        true
      );

    elsif new.type in ('restaurant', 'food') then
      insert into stadium_food_overrides (stadium, name, active, is_new, is_popular, wait_minutes)
      values (new.stadium, new.message, true, false, new.score >= 68, 0);

    elsif new.type in ('traffic', 'tip') then
      insert into stadium_tip_overrides (stadium, tip_text, active, sort_order)
      values (new.stadium, coalesce(new.summary, new.message), true, 0);

    elsif new.type = 'notice' then
      insert into stadium_announcements (stadium, message, active)
      values (new.stadium, coalesce(new.summary, new.message), true);

    end if;
  end if;
  return new;
end;
$$ language plpgsql;

drop trigger if exists trigger_publish_approved on stadium_updates;
create trigger trigger_publish_approved
  after update on stadium_updates
  for each row execute function publish_approved_update();
