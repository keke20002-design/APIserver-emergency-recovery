-- 기존 locakcurapp DB에 관리자 승인 워크플로를 추가하는 1회 마이그레이션.
-- 5/6번 요청에 따라 기존 RLS 정책과 인증 방식은 변경하지 않는다.

alter table issuance_schedules add column if not exists publication_status text;
alter table issuance_schedules add column if not exists approved_at timestamptz;

-- 이 기능 도입 전에 운영 중이던 일정은 기존 노출 상태를 유지한다.
update issuance_schedules
set publication_status = 'PUBLISHED', approved_at = coalesce(approved_at, created_at)
where publication_status is null;

alter table issuance_schedules alter column publication_status set default 'DRAFT';
alter table issuance_schedules alter column publication_status set not null;

do $$ begin
    alter table issuance_schedules add constraint issuance_schedules_publication_status_check
        check (publication_status in ('DRAFT', 'PUBLISHED', 'ARCHIVED'));
exception when duplicate_object then null;
end $$;

alter table crawled_notices add column if not exists reviewed_at timestamptz;
alter table crawled_notices add column if not exists linked_schedule_id bigint
    references issuance_schedules(id);

create index if not exists idx_issuance_schedules_publication_status
    on issuance_schedules (publication_status);
