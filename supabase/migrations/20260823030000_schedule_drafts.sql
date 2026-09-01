-- 引導式建立行程的暫存草稿。
--
-- 使用者透過選單一步步挑日期、開始時間、結束時間，最後才打標題。
-- 前三步都是 postback（無法夾帶自由文字），所以中間狀態必須落地。
-- 一個人在一個 scope 同時只會有一張草稿，撞了就覆蓋。
create table if not exists public.schedule_drafts (
  scope_key   text not null,
  user_id     text not null,
  group_id    text,
  source_type source_type not null,
  target_date date,
  start_time  time,
  end_time    time,
  updated_at  timestamptz not null default now(),
  primary key (scope_key, user_id)
);

alter table public.schedule_drafts enable row level security;

-- 逐步寫入：只覆蓋這次有給的欄位，其餘保留。
-- 挑新日期時把時間清掉，避免沿用上一輪的殘值。
create or replace function public.upsert_draft(
  p_scope_key   text,
  p_user_id     text,
  p_group_id    text,
  p_source_type source_type,
  p_date        date default null,
  p_start       time default null,
  p_end         time default null
) returns public.schedule_drafts
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v public.schedule_drafts;
begin
  insert into public.schedule_drafts
    (scope_key, user_id, group_id, source_type, target_date, start_time, end_time)
  values
    (p_scope_key, p_user_id, p_group_id, p_source_type, p_date, p_start, p_end)
  on conflict (scope_key, user_id) do update set
    group_id    = excluded.group_id,
    source_type = excluded.source_type,
    target_date = coalesce(p_date, schedule_drafts.target_date),
    -- 重新挑日期 => 時間重來
    start_time  = case when p_date is not null then null
                       else coalesce(p_start, schedule_drafts.start_time) end,
    end_time    = case when p_date is not null then null
                       else coalesce(p_end, schedule_drafts.end_time) end,
    updated_at  = now()
  returning * into v;
  return v;
end $$;

-- 只回傳「三個欄位都齊、且還沒過期」的草稿；順手把過期的清掉。
create or replace function public.take_ready_draft(
  p_scope_key text,
  p_user_id   text,
  p_ttl_min   integer default 15
) returns public.schedule_drafts
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v public.schedule_drafts;
begin
  delete from public.schedule_drafts
   where updated_at < now() - (p_ttl_min || ' minutes')::interval;

  select * into v from public.schedule_drafts
   where scope_key = p_scope_key and user_id = p_user_id
     and target_date is not null and start_time is not null and end_time is not null;
  return v;
end $$;

revoke all on function public.upsert_draft(text, text, text, source_type, date, time, time)
  from public, anon, authenticated;
revoke all on function public.take_ready_draft(text, text, integer)
  from public, anon, authenticated;
