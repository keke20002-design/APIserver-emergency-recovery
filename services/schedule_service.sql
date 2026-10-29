-- locakcurapp 발행 일정 + 크롤링/동기화 로그 테이블. Supabase SQL Editor에서 1회 실행.

create table if not exists issuance_schedules (
    id bigint generated always as identity primary key,
    sigungu_code varchar(5) not null,
    region_name text not null,
    product_name text not null,
    issuance_datetime timestamptz not null,
    discount_rate numeric(5,2) not null default 0,
    per_person_limit integer not null default 0,
    status text not null default 'OPEN' check (status in ('OPEN', 'CLOSED')),
    publication_status text not null default 'DRAFT'
        check (publication_status in ('DRAFT', 'PUBLISHED', 'ARCHIVED')),
    approved_at timestamptz,
    -- 구매 앱 딥링크 (Flutter IssuanceSchedule과 동일한 컬럼)
    app_name text,
    app_scheme_ios text,
    package_name_android text,
    store_url_ios text,
    store_url_android text,
    web_url text,
    -- 출처 - 'ADMIN'(수기 등록) 또는 크롤러 플랫폼명(예: 'dongbaekjeon')
    source text not null default 'ADMIN',
    source_notice_id text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_issuance_schedules_sigungu on issuance_schedules (sigungu_code);
create index if not exists idx_issuance_schedules_status on issuance_schedules (status);
-- 크롤러가 같은 공지를 중복 upsert하지 않도록 (source, source_notice_id) 유니크.
-- source_notice_id가 없는 ADMIN 수기 등록 건은 이 제약에서 제외.
create unique index if not exists uq_issuance_schedules_source_notice
    on issuance_schedules (source, source_notice_id)
    where source_notice_id is not null;

alter table issuance_schedules enable row level security;

create policy "Public read access to issuance_schedules" on issuance_schedules
    for select
    to anon, authenticated
    using (true);


-- 크롤러가 수집한 원문 공지 - 발행 일정 구조화 전 단계(스테이징).
-- 자유 텍스트에서 할인율/인당한도/정확한 발행시각을 안정적으로 자동 추출하기
-- 어려워서(공지마다 형식이 다르고 여러 자치구를 한 글에 섞어 적기도 함),
-- 일단 원문만 모아두고 운영자가 POST/PUT /admin/schedules로 구조화해 옮긴다.
create table if not exists crawled_notices (
    id bigint generated always as identity primary key,
    platform text not null,
    notice_id text not null,
    title text not null,
    url text,
    published_date date,
    excerpt text,
    fetched_at timestamptz not null default now(),
    reviewed boolean not null default false,
    reviewed_at timestamptz,
    linked_schedule_id bigint references issuance_schedules(id)
);

create unique index if not exists uq_crawled_notices_platform_notice
    on crawled_notices (platform, notice_id);

alter table crawled_notices enable row level security;

create policy "Public read access to crawled_notices" on crawled_notices
    for select
    to anon, authenticated
    using (true);


-- 일일 배치(크롤링 + 마감 처리) 실행 기록 - GET /admin/sync-log가 조회.
create table if not exists sync_logs (
    id bigint generated always as identity primary key,
    run_at timestamptz not null default now(),
    status text not null check (status in ('SUCCESS', 'PARTIAL', 'FAILED')),
    notices_collected integer not null default 0,
    schedules_closed integer not null default 0,
    error_message text
);

alter table sync_logs enable row level security;

create policy "Public read access to sync_logs" on sync_logs
    for select
    to anon, authenticated
    using (true);

-- 기존 설치 환경용 idempotent migration. 기존 일정은 서비스 연속성을 위해 공개 상태로 둔다.
alter table issuance_schedules add column if not exists publication_status text;
alter table issuance_schedules add column if not exists approved_at timestamptz;
update issuance_schedules set publication_status = 'PUBLISHED' where publication_status is null;
alter table issuance_schedules alter column publication_status set default 'DRAFT';
alter table issuance_schedules alter column publication_status set not null;
do $$ begin
    alter table issuance_schedules add constraint issuance_schedules_publication_status_check
        check (publication_status in ('DRAFT', 'PUBLISHED', 'ARCHIVED'));
exception when duplicate_object then null;
end $$;
alter table crawled_notices add column if not exists reviewed_at timestamptz;
alter table crawled_notices add column if not exists linked_schedule_id bigint references issuance_schedules(id);
create index if not exists idx_issuance_schedules_publication_status
    on issuance_schedules (publication_status);
