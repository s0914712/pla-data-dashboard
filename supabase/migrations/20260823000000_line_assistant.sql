-- LINE 行程 + 記事小助手 (MVP)
--
-- 設計原則:
--   * 所有表 ENABLE RLS 且不建立任何 policy => anon / authenticated 一律拒絕。
--     Edge Function 使用 runtime 自動注入的 service role key，繞過 RLS。
--   * webhook_event_id UNIQUE 是 LINE webhook 重送的去重鍵。
--   * calendar_requests 的狀態機由 Edge Function 以條件更新原子推進。

-- ---------------------------------------------------------------------------
-- 列舉型別
-- ---------------------------------------------------------------------------
do $$ begin
  create type request_status as enum
    ('pending', 'processing', 'confirmed', 'canceled', 'failed', 'expired');
exception when duplicate_object then null; end $$;

do $$ begin
  create type request_type as enum ('meeting', 'leave');
exception when duplicate_object then null; end $$;

do $$ begin
  create type source_type as enum ('group', 'user');
exception when duplicate_object then null; end $$;

-- ---------------------------------------------------------------------------
-- 群組白名單 — 由管理員在群組內輸入 /bind 寫入
-- ---------------------------------------------------------------------------
create table if not exists public.allowed_groups (
  group_id     text primary key,
  display_name text,
  active       boolean     not null default true,
  bound_by     text        not null,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- 使用者白名單 — ADMIN_LINE_USER_ID 之外的額外管理員 / 成員
-- ---------------------------------------------------------------------------
create table if not exists public.allowed_users (
  user_id    text primary key,
  role       text        not null default 'member' check (role in ('admin', 'member')),
  active     boolean     not null default true,
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- 行程請求 — 待確認、確認結果與去重鍵
-- ---------------------------------------------------------------------------
create table if not exists public.calendar_requests (
  id               uuid primary key default gen_random_uuid(),
  webhook_event_id text        not null unique,
  source_type      source_type not null,
  group_id         text,
  user_id          text        not null,

  -- 原文只保留 30 日供除錯，之後由 cleanup_expired() 清為 null
  original_text    text,

  req_type         request_type not null,
  title            text        not null,
  location         text,
  note             text,

  -- 時段事件用 start_at / end_at；全天事件用 start_date / end_date
  is_all_day       boolean     not null default false,
  start_at         timestamptz,
  end_at           timestamptz,
  start_date       date,
  end_date         date,           -- 含首尾的人類可讀迄日；寫 Google 時才 +1 天

  status           request_status not null default 'pending',
  google_event_id  text,
  error_message    text,

  expires_at       timestamptz not null,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),

  -- 時段事件必須有起訖時間；全天事件必須有起訖日期
  constraint calendar_requests_time_shape check (
    (is_all_day = false and start_at is not null and end_at is not null
       and start_date is null and end_date is null)
    or
    (is_all_day = true  and start_date is not null and end_date is not null
       and start_at is null and end_at is null)
  ),
  constraint calendar_requests_time_order check (
    (is_all_day = false and end_at > start_at)
    or
    (is_all_day = true  and end_date >= start_date)
  )
);

create index if not exists calendar_requests_pending_idx
  on public.calendar_requests (status, expires_at)
  where status = 'pending';

create index if not exists calendar_requests_group_idx
  on public.calendar_requests (group_id, created_at desc);

create index if not exists calendar_requests_user_idx
  on public.calendar_requests (user_id, created_at desc);

-- ---------------------------------------------------------------------------
-- 記事 — 不進 Google Calendar，軟刪除
-- seq 是同一 scope (群組或個人) 內給使用者看的短編號
-- ---------------------------------------------------------------------------
create table if not exists public.notes (
  id               uuid primary key default gen_random_uuid(),
  webhook_event_id text        not null unique,
  source_type      source_type not null,
  group_id         text,
  user_id          text        not null,
  scope_key        text        not null,   -- group_id 或 'u:' || user_id
  seq              integer     not null,
  content          text        not null,
  tags             text[]      not null default '{}',
  created_at       timestamptz not null default now(),
  deleted_at       timestamptz,
  unique (scope_key, seq)
);

create index if not exists notes_scope_idx
  on public.notes (scope_key, created_at desc)
  where deleted_at is null;

create index if not exists notes_content_idx
  on public.notes using gin (to_tsvector('simple', content))
  where deleted_at is null;

-- 原子地取得某個 scope 的下一個短編號，並插入記事。
-- 併發時靠 unique(scope_key, seq) + advisory lock 確保不撞號。
create or replace function public.insert_note(
  p_webhook_event_id text,
  p_source_type      source_type,
  p_group_id         text,
  p_user_id          text,
  p_scope_key        text,
  p_content          text,
  p_tags             text[]
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
    (webhook_event_id, source_type, group_id, user_id, scope_key, seq, content, tags)
  values
    (p_webhook_event_id, p_source_type, p_group_id, p_user_id, p_scope_key, v_seq, p_content, p_tags)
  returning * into v_note;

  return v_note;
end $$;

-- ---------------------------------------------------------------------------
-- 稽核 — 誰在何時做了什麼、結果如何
-- actor_hash 是 LINE userId 的 SHA-256，長期保存不留原始 ID
-- ---------------------------------------------------------------------------
create table if not exists public.audit_logs (
  id         bigserial primary key,
  request_id uuid,
  action     text        not null,
  actor_hash text,
  result     text        not null,
  detail     jsonb       not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists audit_logs_request_idx  on public.audit_logs (request_id);
create index if not exists audit_logs_created_idx  on public.audit_logs (created_at desc);

-- ---------------------------------------------------------------------------
-- updated_at 自動維護
-- ---------------------------------------------------------------------------
create or replace function public.touch_updated_at()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  new.updated_at := now();
  return new;
end $$;

drop trigger if exists allowed_groups_touch on public.allowed_groups;
create trigger allowed_groups_touch before update on public.allowed_groups
  for each row execute function public.touch_updated_at();

drop trigger if exists calendar_requests_touch on public.calendar_requests;
create trigger calendar_requests_touch before update on public.calendar_requests
  for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------------------
-- 保留政策 — 逾時 pending 標 expired，30 日前的原文清空
-- ---------------------------------------------------------------------------
create or replace function public.cleanup_expired()
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_expired integer;
  v_scrubbed integer;
begin
  update public.calendar_requests
     set status = 'expired'
   where status = 'pending' and expires_at < now();
  get diagnostics v_expired = row_count;

  update public.calendar_requests
     set original_text = null
   where original_text is not null and created_at < now() - interval '30 days';
  get diagnostics v_scrubbed = row_count;

  return jsonb_build_object('expired', v_expired, 'scrubbed', v_scrubbed);
end $$;

-- ---------------------------------------------------------------------------
-- RLS: 全部啟用、不建 policy => 拒絕預設
-- ---------------------------------------------------------------------------
alter table public.allowed_groups    enable row level security;
alter table public.allowed_users     enable row level security;
alter table public.calendar_requests enable row level security;
alter table public.notes             enable row level security;
alter table public.audit_logs        enable row level security;

-- security definer 函式不開放給匿名端呼叫
revoke all on function public.insert_note(text, source_type, text, text, text, text, text[]) from public, anon, authenticated;
revoke all on function public.cleanup_expired() from public, anon, authenticated;
