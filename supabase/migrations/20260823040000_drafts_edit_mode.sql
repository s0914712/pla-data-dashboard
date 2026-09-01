-- 草稿表擴充成通用的「待處理操作」：除了新建，也承載修訂／複製／刪除。
-- 這些流程一樣是多步 postback + 最後可能要打字，狀態需求完全相同。
alter table public.schedule_drafts
  add column if not exists mode text not null default 'create',
  add column if not exists event_id text,
  add column if not exists payload_title text,
  add column if not exists payload_location text;

do $$ begin
  alter table public.schedule_drafts add constraint schedule_drafts_mode_check
    check (mode in ('create','copy','edit_time','edit_title','edit_location','delete'));
exception when duplicate_object then null; end $$;

-- 舊的 upsert_draft 只處理新建；換成能帶 mode 與 event_id 的版本。
drop function if exists public.upsert_draft(text, text, text, source_type, date, time, time);

create or replace function public.upsert_draft(
  p_scope_key   text,
  p_user_id     text,
  p_group_id    text,
  p_source_type source_type,
  p_date        date default null,
  p_start       time default null,
  p_end         time default null,
  p_mode        text default null,
  p_event_id    text default null,
  p_title       text default null,
  p_location    text default null,
  -- 開新流程時要把上一輪殘留的欄位整個清掉
  p_reset       boolean default false
) returns public.schedule_drafts
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v public.schedule_drafts;
begin
  insert into public.schedule_drafts
    (scope_key, user_id, group_id, source_type, target_date, start_time, end_time,
     mode, event_id, payload_title, payload_location)
  values
    (p_scope_key, p_user_id, p_group_id, p_source_type, p_date, p_start, p_end,
     coalesce(p_mode, 'create'), p_event_id, p_title, p_location)
  on conflict (scope_key, user_id) do update set
    group_id    = excluded.group_id,
    source_type = excluded.source_type,
    mode        = coalesce(p_mode, schedule_drafts.mode),
    event_id    = case when p_reset then p_event_id
                       else coalesce(p_event_id, schedule_drafts.event_id) end,
    payload_title = case when p_reset then p_title
                         else coalesce(p_title, schedule_drafts.payload_title) end,
    payload_location = case when p_reset then p_location
                            else coalesce(p_location, schedule_drafts.payload_location) end,
    -- 重挑日期（或開新流程）時把時間清掉，避免沿用上一輪的殘值
    target_date = case when p_reset then p_date
                       else coalesce(p_date, schedule_drafts.target_date) end,
    start_time  = case when p_reset or p_date is not null then p_start
                       else coalesce(p_start, schedule_drafts.start_time) end,
    end_time    = case when p_reset or p_date is not null then p_end
                       else coalesce(p_end, schedule_drafts.end_time) end,
    updated_at  = now()
  returning * into v;
  return v;
end $$;

-- 回傳任何一張還沒過期的草稿，由呼叫端依 mode 決定完整與否；順手清掉過期的。
create or replace function public.get_draft(
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
   where scope_key = p_scope_key and user_id = p_user_id;
  return v;
end $$;

drop function if exists public.take_ready_draft(text, text, integer);

revoke all on function public.upsert_draft(text, text, text, source_type, date, time, time, text, text, text, text, boolean)
  from public, anon, authenticated;
revoke all on function public.get_draft(text, text, integer) from public, anon, authenticated;
