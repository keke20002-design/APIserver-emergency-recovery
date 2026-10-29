-- locakcurapp 지역 마스터 테이블. Supabase SQL Editor에서 1회 실행.
-- (region_service.py는 이 테이블을 REST로만 읽고/쓰므로 DDL은 여기서 직접 실행해야 함)

create table if not exists regions (
    id bigint generated always as identity primary key,
    sido_code varchar(2) not null,
    sido_name text not null,
    sigungu_code varchar(5) not null unique,
    sigungu_name text not null,
    is_active boolean not null default true,
    created_at timestamptz not null default now()
);

create index if not exists idx_regions_sido_code on regions (sido_code);

-- 공개 참조 데이터(지자체 코드/이름)라 RLS를 켜고 읽기만 공개한다.
-- apiserver는 service_role 키로 접속해 RLS를 우회하므로 쓰기(seed_regions.py,
-- region_service.upsert_regions)에는 영향 없음. anon/authenticated 키로는
-- select만 가능하고 insert/update/delete는 정책이 없어 기본 거부된다.
alter table regions enable row level security;

create policy "Public read access to regions" on regions
    for select
    to anon, authenticated
    using (true);
