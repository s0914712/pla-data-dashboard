-- 日期區間的記事查詢。
--
-- notes_for_date 的區間版本，歸戶規則完全相同（target_date 優先，沒有就用
-- 建立當天的台北日期），只是把等值比對換成 between，並多接一個關鍵字。
--
-- 關鍵字放在 SQL 裡做而不是撈回來再過濾：limit 才會落在「符合條件的前 N 筆」
-- 上，而不是「前 N 筆裡剛好符合的幾筆」。
create or replace function public.notes_in_range(
  p_scope_key text,
  p_from      date,
  p_to        date,
  p_keyword   text default ''
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
     and coalesce(target_date, (timezone('Asia/Taipei', created_at))::date)
         between p_from and p_to
     and (
       coalesce(p_keyword, '') = ''
       -- ilike 的萬用字元先跳脫，避免使用者打的 % _ 變成 pattern
       or content ilike '%' ||
            replace(replace(replace(p_keyword, '\', '\\'), '%', '\%'), '_', '\_')
            || '%'
     )
   order by coalesce(target_date, (timezone('Asia/Taipei', created_at))::date) asc,
            created_at asc
   limit 50;
$$;

revoke all on function public.notes_in_range(text, date, date, text) from public, anon, authenticated;
