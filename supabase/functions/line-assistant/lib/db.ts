/**
 * PostgREST 存取層。
 *
 * 刻意不使用 supabase-js —— 這個函式的用量只是幾個單表操作與一支 RPC，
 * 直接打 REST API 讓整個 Edge Function 零依賴，也讓本機 deno check 能完整跑。
 *
 * SUPABASE_URL 與 SUPABASE_SERVICE_ROLE_KEY 由 Edge Runtime 自動注入，
 * 不需要（也不可以）手動設成 secret。service role key 會繞過 RLS。
 */

import type { CalendarRequestRow, NoteRow, RequestStatus, SourceType } from "./types.ts";

const REST = `${Deno.env.get("SUPABASE_URL")}/rest/v1`;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

function headers(extra: Record<string, string> = {}): HeadersInit {
  return {
    apikey: SERVICE_KEY,
    Authorization: `Bearer ${SERVICE_KEY}`,
    "Content-Type": "application/json",
    ...extra,
  };
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${REST}${path}`, { ...init, headers: { ...headers(), ...(init.headers ?? {}) } });
  const text = await res.text();
  if (!res.ok) {
    // PostgREST 錯誤訊息可能含欄位值，只往上帶 code 與 message
    let message = `postgrest ${res.status}`;
    try {
      const body = JSON.parse(text);
      message = `postgrest ${res.status} ${body.code ?? ""} ${body.message ?? ""}`.trim();
    } catch { /* 非 JSON */ }
    throw new PostgrestError(message, res.status, text);
  }
  return text ? JSON.parse(text) as T : (undefined as T);
}

export class PostgrestError extends Error {
  constructor(message: string, readonly status: number, readonly raw: string) {
    super(message);
    this.name = "PostgrestError";
  }
  /** 23505 = unique_violation，用來判斷 webhook 重送。 */
  get isUniqueViolation(): boolean {
    return this.raw.includes("23505");
  }
}

// --- 白名單 ------------------------------------------------------------------

export async function isGroupAllowed(groupId: string): Promise<boolean> {
  const rows = await request<{ group_id: string }[]>(
    `/allowed_groups?group_id=eq.${encodeURIComponent(groupId)}&active=is.true&select=group_id&limit=1`,
  );
  return rows.length > 0;
}

export async function bindGroup(groupId: string, boundBy: string, displayName: string | null): Promise<void> {
  await request("/allowed_groups", {
    method: "POST",
    headers: { Prefer: "resolution=merge-duplicates,return=minimal" },
    body: JSON.stringify({ group_id: groupId, bound_by: boundBy, display_name: displayName, active: true }),
  });
}

export async function unbindGroup(groupId: string): Promise<void> {
  await request(`/allowed_groups?group_id=eq.${encodeURIComponent(groupId)}`, {
    method: "PATCH",
    headers: { Prefer: "return=minimal" },
    body: JSON.stringify({ active: false }),
  });
}

export async function isAdminUser(userId: string): Promise<boolean> {
  const rows = await request<{ user_id: string }[]>(
    `/allowed_users?user_id=eq.${encodeURIComponent(userId)}&role=eq.admin&active=is.true&select=user_id&limit=1`,
  );
  return rows.length > 0;
}

/** 尚未綁定任何群組時，任何白名單檢查都應回 false 而不是報錯。 */
export async function boundGroupCount(): Promise<number> {
  const res = await fetch(`${REST}/allowed_groups?select=group_id&active=is.true`, {
    headers: headers({ Prefer: "count=exact", Range: "0-0" }),
  });
  const range = res.headers.get("content-range") ?? "*/0";
  await res.body?.cancel();
  return Number(range.split("/")[1] ?? 0);
}

// --- 行程請求 ----------------------------------------------------------------

export interface NewCalendarRequest {
  webhook_event_id: string;
  source_type: SourceType;
  group_id: string | null;
  user_id: string;
  original_text: string;
  req_type: "meeting" | "leave";
  title: string;
  location: string | null;
  note: string | null;
  is_all_day: boolean;
  start_at: string | null;
  end_at: string | null;
  start_date: string | null;
  end_date: string | null;
  expires_at: string;
}

/**
 * 建立待確認請求。webhook_event_id 是 UNIQUE，LINE 重送時會撞鍵，
 * 此時回傳既有那一筆而不是新增第二筆（計畫書 §6.3）。
 */
export async function createRequest(row: NewCalendarRequest): Promise<CalendarRequestRow> {
  try {
    const [created] = await request<CalendarRequestRow[]>("/calendar_requests", {
      method: "POST",
      headers: { Prefer: "return=representation" },
      body: JSON.stringify(row),
    });
    return created;
  } catch (err) {
    if (err instanceof PostgrestError && err.isUniqueViolation) {
      const existing = await getRequestByEventId(row.webhook_event_id);
      if (existing) return existing;
    }
    throw err;
  }
}

export async function getRequest(id: string): Promise<CalendarRequestRow | null> {
  const rows = await request<CalendarRequestRow[]>(
    `/calendar_requests?id=eq.${encodeURIComponent(id)}&select=*&limit=1`,
  );
  return rows[0] ?? null;
}

async function getRequestByEventId(eventId: string): Promise<CalendarRequestRow | null> {
  const rows = await request<CalendarRequestRow[]>(
    `/calendar_requests?webhook_event_id=eq.${encodeURIComponent(eventId)}&select=*&limit=1`,
  );
  return rows[0] ?? null;
}

/**
 * 原子地把狀態由 from 推進到 to。
 *
 * 這是防重複建立的關鍵：兩次「確認建立」同時進來時，條件更新
 * (status = 'pending') 只有第一個會拿到 row，第二個拿到空陣列。
 */
export async function advanceStatus(
  id: string,
  from: RequestStatus,
  to: RequestStatus,
  extra: Record<string, unknown> = {},
): Promise<CalendarRequestRow | null> {
  const rows = await request<CalendarRequestRow[]>(
    `/calendar_requests?id=eq.${encodeURIComponent(id)}&status=eq.${from}`,
    {
      method: "PATCH",
      headers: { Prefer: "return=representation" },
      body: JSON.stringify({ status: to, ...extra }),
    },
  );
  return rows[0] ?? null;
}

export async function updateRequest(id: string, patch: Record<string, unknown>): Promise<void> {
  await request(`/calendar_requests?id=eq.${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { Prefer: "return=minimal" },
    body: JSON.stringify(patch),
  });
}

// --- 記事 --------------------------------------------------------------------

export function scopeKeyFor(groupId: string | null, userId: string): string {
  return groupId ? `g:${groupId}` : `u:${userId}`;
}

export async function insertNote(args: {
  webhook_event_id: string;
  source_type: SourceType;
  group_id: string | null;
  user_id: string;
  scope_key: string;
  content: string;
  tags: string[];
}): Promise<NoteRow> {
  return await request<NoteRow>("/rpc/insert_note", {
    method: "POST",
    body: JSON.stringify({
      p_webhook_event_id: args.webhook_event_id,
      p_source_type: args.source_type,
      p_group_id: args.group_id,
      p_user_id: args.user_id,
      p_scope_key: args.scope_key,
      p_content: args.content,
      p_tags: args.tags,
    }),
  });
}

export async function listNotes(scopeKey: string, keyword: string, limit = 10): Promise<NoteRow[]> {
  let path = `/notes?scope_key=eq.${encodeURIComponent(scopeKey)}&deleted_at=is.null` +
    `&select=id,seq,content,tags,user_id,created_at&order=created_at.desc&limit=${limit}`;
  if (keyword) {
    // ilike 的萬用字元與跳脫字元先處理掉，避免使用者輸入變成 pattern
    const safe = keyword.replace(/[\\%_]/g, (c) => `\\${c}`);
    path += `&content=ilike.*${encodeURIComponent(safe)}*`;
  }
  return await request<NoteRow[]>(path);
}

export async function findNoteBySeq(scopeKey: string, seq: number): Promise<NoteRow | null> {
  const rows = await request<NoteRow[]>(
    `/notes?scope_key=eq.${encodeURIComponent(scopeKey)}&seq=eq.${seq}&deleted_at=is.null&select=*&limit=1`,
  );
  return rows[0] ?? null;
}

export async function softDeleteNote(id: string): Promise<void> {
  await request(`/notes?id=eq.${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { Prefer: "return=minimal" },
    body: JSON.stringify({ deleted_at: new Date().toISOString() }),
  });
}

// --- 稽核 --------------------------------------------------------------------

export async function audit(entry: {
  request_id?: string | null;
  action: string;
  actor_hash?: string | null;
  result: string;
  detail?: Record<string, unknown>;
}): Promise<void> {
  try {
    await request("/audit_logs", {
      method: "POST",
      headers: { Prefer: "return=minimal" },
      body: JSON.stringify({ detail: {}, ...entry }),
    });
  } catch (err) {
    // 稽核失敗不能讓主流程掛掉
    console.error("audit write failed:", (err as Error).message);
  }
}

/** /diag 用：確認 DB 可讀寫。 */
export async function pingDb(): Promise<boolean> {
  try {
    await request<unknown[]>("/allowed_groups?select=group_id&limit=1");
    return true;
  } catch {
    return false;
  }
}

export async function cleanupExpired(): Promise<{ expired: number; scrubbed: number }> {
  return await request<{ expired: number; scrubbed: number }>("/rpc/cleanup_expired", {
    method: "POST",
    body: "{}",
  });
}
