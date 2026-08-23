-- 記事加上「這件事是關於哪一天」的日期，供日檢視查詢。
--
-- created_at 是「什麼時候寫下來的」，無法表達「明天要做的事」。
-- target_date 由解析器從記事內容自動推論（明天／下週一／8/28），
-- 推論不出來時為 NULL，此時日檢視改用 created_at 當天歸戶。

alter table public.notes
  add column if not exists target_date date;

-- 日檢視的查詢條件：target_date = D，或 (target_date is null and created_at 當天 = D)
create index if not exists notes_target_date_idx
  on public.notes (scope_key, target_date)
  where deleted_at is null and target_date is not null;

create index if not exists notes_created_date_idx
  on public.notes (scope_key, (timezone('Asia/Taipei', created_at)::date))
  where deleted_at is null and target_date is null;

-- insert_note 多收一個 p_target_date。舊簽章直接移除，避免留下多載造成呼叫歧義。
drop function if exists public.insert_note(text, source_type, text, text, text, text, text[]);

create or replace function public.insert_note(
  p_webhook_event_id text,
  p_source_type      source_type,
  p_group_id         text,
  p_user_id          text,
  p_scope_key        text,
  p_content          text,
  p_tags             text[],
  p_target_date      date default null
) returns public.notes
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_seq  integer;
  v_note public.notes;
begin
  -- 既有 webhook event 直接回傳原記錄 (webhook 重送冪等)
  select * into v_note from public.notes where webhook_event_id = p_webhook_event_id;
  if found then
    return v_note;
  end if;

  perform pg_advisory_xact_lock(hashtext(p_scope_key));

  select coalesce(max(seq), 0) + 1 into v_seq
    from public.notes where scope_key = p_scope_key;

  insert into public.notes
    (webhook_event_id, source_type, group_id, user_id, scope_key, seq, content, tags, target_date)
  values
    (p_webhook_event_id, p_source_type, p_group_id, p_user_id, p_scope_key, v_seq,
     p_content, p_tags, p_target_date)
  returning * into v_note;

  return v_note;
end $$;

revoke all on function public.insert_note(text, source_type, text, text, text, text, text[], date)
  from public, anon, authenticated;
