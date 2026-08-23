/**
 * Google Calendar 服務帳戶整合。
 *
 * Deno 沒有 google-auth-library，這裡用 Web Crypto 自行簽 RS256 JWT
 * 換 access token（Google Identity: OAuth 2.0 for Server to Server）。
 * 零依賴，也不需要任何使用者 OAuth 授權。
 */

import { requireEnv } from "./env.ts";
import { exclusiveEndDate } from "./parser_zh_tw.ts";
import type { CalendarRequestRow } from "./types.ts";

const TOKEN_URL = "https://oauth2.googleapis.com/token";
const CAL_API = "https://www.googleapis.com/calendar/v3";
const SCOPE = "https://www.googleapis.com/auth/calendar.events";

interface ServiceAccount {
  client_email: string;
  private_key: string;
}

function serviceAccount(): ServiceAccount {
  const raw = requireEnv("GOOGLE_SERVICE_ACCOUNT_JSON");
  let parsed: ServiceAccount;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON");
  }
  if (!parsed.client_email || !parsed.private_key) {
    throw new Error("GOOGLE_SERVICE_ACCOUNT_JSON missing client_email or private_key");
  }
  // 從 Dashboard 貼進來時 \n 常會變成字面上的兩個字元
  parsed.private_key = parsed.private_key.replace(/\\n/g, "\n");
  return parsed;
}

/** 服務帳戶的 client_email —— /diag 會顯示，方便核對日曆分享對象。 */
export function serviceAccountEmail(): string {
  return serviceAccount().client_email;
}

// --- JWT 簽章 ----------------------------------------------------------------

function base64url(bytes: Uint8Array): string {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64urlText(text: string): string {
  return base64url(new TextEncoder().encode(text));
}

async function importPrivateKey(pem: string): Promise<CryptoKey> {
  const body = pem
    .replace(/-----BEGIN PRIVATE KEY-----/, "")
    .replace(/-----END PRIVATE KEY-----/, "")
    .replace(/\s+/g, "");
  const der = Uint8Array.from(atob(body), (c) => c.charCodeAt(0));
  return await crypto.subtle.importKey(
    "pkcs8",
    der,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"],
  );
}

// access token 在同一個 isolate 內快取到過期前 60 秒
let cachedToken: { value: string; expiresAt: number } | null = null;

export async function accessToken(): Promise<string> {
  if (cachedToken && Date.now() < cachedToken.expiresAt) return cachedToken.value;

  const sa = serviceAccount();
  const now = Math.floor(Date.now() / 1000);
  const header = base64urlText(JSON.stringify({ alg: "RS256", typ: "JWT" }));
  const claims = base64urlText(JSON.stringify({
    iss: sa.client_email,
    scope: SCOPE,
    aud: TOKEN_URL,
    iat: now,
    exp: now + 3600,
  }));

  const key = await importPrivateKey(sa.private_key);
  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    key,
    new TextEncoder().encode(`${header}.${claims}`),
  );
  const assertion = `${header}.${claims}.${base64url(new Uint8Array(signature))}`;

  const res = await fetch(TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
      assertion,
    }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(`google token ${res.status}: ${body.error ?? ""} ${body.error_description ?? ""}`.trim());
  }
  const token = await res.json();
  cachedToken = {
    value: token.access_token,
    expiresAt: Date.now() + (token.expires_in - 60) * 1000,
  };
  return cachedToken.value;
}

// --- 事件操作 ----------------------------------------------------------------

function calendarId(): string {
  return requireEnv("GOOGLE_CALENDAR_ID");
}

async function callCalendar(path: string, init: RequestInit = {}): Promise<Response> {
  const token = await accessToken();
  return await fetch(`${CAL_API}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
  });
}

/**
 * 依 request_id 找出既有事件。
 *
 * 計畫書 §6.3：Google 已建立但本地回寫失敗時，重試前先查一次，
 * 避免同一個 request 產生兩筆事件。
 */
export async function findEventByRequestId(requestId: string): Promise<string | null> {
  const params = new URLSearchParams({
    privateExtendedProperty: `request_id=${requestId}`,
    maxResults: "1",
    showDeleted: "false",
  });
  const res = await callCalendar(
    `/calendars/${encodeURIComponent(calendarId())}/events?${params}`,
  );
  if (!res.ok) {
    await res.body?.cancel();
    return null;
  }
  const body = await res.json();
  return body.items?.[0]?.id ?? null;
}

/**
 * 建立事件。回傳 Google event ID。
 *
 * summary 刻意只放標題（請假時加上建立者姓名以便辨識）；原始訊息與請假理由
 * 預設不寫入，只有使用者明確輸入「備註：」才進 description（計畫書 §5.4）。
 */
export async function insertEvent(
  req: CalendarRequestRow,
  creatorName: string,
  timezone: string,
): Promise<string> {
  const existing = await findEventByRequestId(req.id);
  if (existing) return existing;

  const summary = req.req_type === "leave" ? `${req.title}（${creatorName}）` : req.title;

  const body: Record<string, unknown> = {
    summary,
    extendedProperties: { private: { request_id: req.id, source: "line-assistant" } },
  };
  if (req.location) body.location = req.location;
  if (req.note) body.description = `備註：${req.note}`;

  if (req.is_all_day) {
    body.start = { date: req.start_date };
    // Google 全天事件的 end.date 是排除式，DB 存的是含首尾的迄日
    body.end = { date: exclusiveEndDate(req.end_date!) };
  } else {
    body.start = { dateTime: req.start_at, timeZone: timezone };
    body.end = { dateTime: req.end_at, timeZone: timezone };
  }

  const res = await callCalendar(`/calendars/${encodeURIComponent(calendarId())}/events`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const message = err?.error?.message ?? "";
    if (res.status === 403 || res.status === 404) {
      throw new Error(
        `Google Calendar ${res.status}：${message}。` +
        `請確認日曆已分享給 ${serviceAccount().client_email} 且權限為「變更活動」，` +
        `且 GOOGLE_CALENDAR_ID 指向該共用日曆。`,
      );
    }
    throw new Error(`Google Calendar ${res.status}: ${message}`);
  }
  const created = await res.json();
  return created.id as string;
}

/** /diag 用：驗證憑證與日曆權限，不建立任何事件。 */
export async function probeAccess(): Promise<{ ok: boolean; detail: string }> {
  try {
    const res = await callCalendar(`/calendars/${encodeURIComponent(calendarId())}`);
    if (res.ok) {
      const cal = await res.json();
      return { ok: true, detail: `可讀取日曆「${cal.summary ?? calendarId()}」` };
    }
    const err = await res.json().catch(() => ({}));
    return { ok: false, detail: `HTTP ${res.status} ${err?.error?.message ?? ""}`.trim() };
  } catch (e) {
    return { ok: false, detail: (e as Error).message };
  }
}
