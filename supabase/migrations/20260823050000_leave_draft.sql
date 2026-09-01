-- 引導式請假：選日期 → 選方式（全天／時段／跨多天）→ 寫事由。
--
-- 需要兩個新欄位：跨多天請假要記迄日，事由要暫存到確認為止。
-- 事由刻意獨立成 payload_note，不跟 payload_title 共用 —— 兩者語意不同，
-- 混用會在之後看不出哪個欄位裝什麼。
alter table public.schedule_drafts
  add column if not exists end_date date,
  add column if not exists payload_note text;

-- mode 多一種 'leave'
alter table public.schedule_drafts drop constraint if exists schedule_drafts_mode_check;
alter table public.schedule_drafts add constraint schedule_drafts_mode_check
  check (mode in ('create','copy','edit_time','edit_title','edit_location','delete','leave'));

drop function if exists public.upsert_draft(text, text, text, source_type, date, time, time, text, text, text, text, boolean);

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
  p_end_date    date default null,
  p_note        text default null,
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
     mode, event_id, payload_title, payload_location, end_date, payload_note)
  values
    (p_scope_key, p_user_id, p_group_id, p_source_type, p_date, p_start, p_end,
     coalesce(p_mode, 'create'), p_event_id, p_title, p_location, p_end_date, p_note)
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
    payload_note = case when p_reset then p_note
                        else coalesce(p_note, schedule_drafts.payload_note) end,
    -- 重挑日期（或開新流程）時把時間與迄日清掉，避免沿用上一輪的殘值
    target_date = case when p_reset then p_date
                       else coalesce(p_date, schedule_drafts.target_date) end,
    start_time  = case when p_reset or p_date is not null then p_start
                       else coalesce(p_start, schedule_drafts.start_time) end,
    end_time    = case when p_reset or p_date is not null then p_end
                       else coalesce(p_end, schedule_drafts.end_time) end,
    end_date    = case when p_reset or p_date is not null then p_end_date
                       else coalesce(p_end_date, schedule_drafts.end_date) end,
    updated_at  = now()
  returning * into v;
  return v;
end $$;

revoke all on function public.upsert_draft(
  text, text, text, source_type, date, time, time, text, text, text, text, date, text, boolean
) from public, anon, authenticated;
