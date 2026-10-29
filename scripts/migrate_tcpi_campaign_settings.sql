-- Run once in the Supabase SQL editor before the first Google Ads sync.
-- Additive only: existing campaign mappings and performance data are preserved.

alter table campaigns add column if not exists target_cpi numeric(14, 4);
alter table campaigns add column if not exists daily_budget numeric(14, 4);
alter table campaigns add column if not exists bidding_strategy_goal_type text;
alter table campaigns add column if not exists target_cpi_changed_at timestamptz;
alter table campaigns add column if not exists settings_synced_at timestamptz;
