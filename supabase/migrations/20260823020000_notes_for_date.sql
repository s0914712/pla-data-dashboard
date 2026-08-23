-- 某一天的記事查詢。
--
-- 刻意做成 SQL function 而不是在 Edge Function 裡組 PostgREST 的 or=(...) 巢狀
-- 過濾字串：邏輯放在 SQL 這邊可以直接用 SQL 驗證，也不必在 URL 裡手工跳脫時間戳。
--
-- 歸戶規則取聯集：
--   1. target_date 等於當天（記事內容有提到日期，例如「明天要帶識別證」）
--   2. target_date 為 null，且建立當天（台北時區）等於當天
create or replace function public.notes_for_date(
  p_scope_key text,
  p_date      date
) returns setof public.notes
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select *
    from public.notes
   where scope_key = p_scope_key
     and deleted_at is null
     and (
       target_date = p_date
       or (target_date is null
           and (timezone('Asia/Taipei', created_at))::date = p_date)
     )
   order by target_date nulls last, created_at asc
   limit 30;
$$;

revoke all on function public.notes_for_date(text, date) from public, anon, authenticated;
