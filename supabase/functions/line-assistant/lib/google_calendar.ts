/**
 * Google Calendar 服務帳戶整合。
 *
 * Deno 沒有 google-auth-library，這裡用 Web Crypto 自行簽 RS256 JWT
 * 換 access token（Google Identity: OAuth 2.0 for Server to Server）。
 * 零依賴，也不需要任何使用者 OAuth 授權。
 */

import { requireEnv } from "./env.ts";
import { addDays, exclusiveEndDate, isoDate, isLeaveTitle } from "./parser_zh_tw.ts";
import type { CalendarRequestRow } from "./types.ts";

const TOKEN_URL = "https://oauth2.googleapis.com/token";
const CAL_API = "https://www.googleapis.com/calendar/v3";
const SCOPE = [
  "https://www.googleapis.com/auth/calendar.events",
  "https://www.googleapis.com/auth/calendar.readonly",
].join(" ");

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
    extendedProperties: {
      private: { request_id: req.id, source: "line-assistant", req_type: req.req_type },
    },
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

// --- 日檢視 ------------------------------------------------------------------

export interface DayEvent {
  /** Google event id —— 修訂流程靠這個指向同一筆事件。 */
  id: string;
  summary: string;
  location: string | null;
  isAllDay: boolean;
  /** 時段事件的 HH:MM（Asia/Taipei）；全天事件為 null。 */
  startTime: string | null;
  endTime: string | null;
  /** 會議或請假。優先看 extendedProperties，沒有才退回看標題。 */
  reqType: "meeting" | "leave";
  /** 事件涵蓋的第一天（Asia/Taipei）。區間檢視據此分日。 */
  date: string;
  /** 涵蓋的最後一天（**含**）。單日事件與 date 相同；跨日全天請假才會不同。 */
  endDate: string;
}

/**
 * 列出某一天共用日曆上的所有事件。
 *
 * 直接查 Google 而不是查 calendar_requests，因為共用日曆才是正式來源 ——
 * 這樣在手機日曆 App 裡直接新增的事件也會一起列出。
 * singleEvents=true 會把重複性事件展開成當天的實例。
 */
export function listEvents(dateIso: string, timezone: string): Promise<DayEvent[]> {
  return listEventsRange(dateIso, dateIso, timezone);
}

/**
 * 列出一段日期區間內的事件，可選擇再加上關鍵字。
 *
 * keyword 走 Google 的 q 參數（全文比對標題、說明、地點），所以行程與記事
 * 兩邊的關鍵字查詢是同一個語意。maxResults 隨天數放大但設上限，
 * 免得查一整個月時把整包塞爆 LINE 的訊息長度。
 */
export async function listEventsRange(
  fromIso: string,
  toIso: string,
  timezone: string,
  keyword = "",
): Promise<DayEvent[]> {
  const days = Math.max(1, Math.round(
    (Date.parse(`${toIso}T00:00:00Z`) - Date.parse(`${fromIso}T00:00:00Z`)) / 86_400_000,
  ) + 1);
  const params = new URLSearchParams({
    timeMin: `${fromIso}T00:00:00+08:00`,
    timeMax: `${toIso}T23:59:59+08:00`,
    singleEvents: "true",
    orderBy: "startTime",
    maxResults: String(Math.min(250, days * 50)),
    timeZone: timezone,
  });
  if (keyword) params.set("q", keyword);

  const res = await callCalendar(`/calendars/${encodeURIComponent(calendarId())}/events?${params}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(`Google Calendar ${res.status}: ${err?.error?.message ?? ""}`.trim());
  }
  const body = await res.json();

  // deno-lint-ignore no-explicit-any
  return (body.items ?? []).map((item: any): DayEvent => {
    const isAllDay = Boolean(item.start?.date);
    // 全天事件的 end.date 是排除式，往回一天才是實際的最後一天；
    // 時段事件一律歸在開始那一天（跨夜的 22:00-01:00 也算前一天的事）。
    const date = isAllDay ? String(item.start.date) : localDate(item.start?.dateTime);
    const endDate = isAllDay ? inclusiveEnd(String(item.end?.date ?? item.start.date)) : date;
    return {
      id: String(item.id ?? ""),
      summary: String(item.summary ?? "(無標題)"),
      location: item.location ? String(item.location) : null,
      isAllDay,
      startTime: isAllDay ? null : localHhmm(item.start?.dateTime),
      endTime: isAllDay ? null : localHhmm(item.end?.dateTime),
      reqType: classify(item),
      date,
      endDate,
    };
  });
}

/** Google 全天事件的 end.date 是排除式，往回一天才是人看的最後一天。 */
function inclusiveEnd(exclusive: string): string {
  const [y, m, d] = exclusive.split("-").map(Number);
  if (!y || !m || !d) return exclusive;
  return isoDate(addDays({ y, m, d }, -1));
}

/**
 * 判斷一筆事件是會議還是請假。
 *
 * 小助手建立的會在 extendedProperties.private.req_type 留下答案；
 * 手機日曆 App 直接新增的、以及這個欄位上線前建立的沒有，退回看標題。
 */
// deno-lint-ignore no-explicit-any
function classify(item: any): "meeting" | "leave" {
  const tagged = item?.extendedProperties?.private?.req_type;
  if (tagged === "leave" || tagged === "meeting") return tagged;
  return isLeaveTitle(String(item?.summary ?? "")) ? "leave" : "meeting";
}

/**
 * Google 回傳的 dateTime 帶原始時區位移（例如 +08:00），直接取字串的 HH:MM 即可。
 * 若來源是別的時區（例如手機在國外新增的事件），換算成 Asia/Taipei 再取。
 */
/** 帶位移的 ISO 字串 → 台北的 YYYY-MM-DD。 */
function localDate(dateTime: string | undefined): string {
  if (!dateTime) return "";
  const ms = Date.parse(dateTime);
  if (Number.isNaN(ms)) return dateTime.slice(0, 10);
  const tpe = new Date(ms + 8 * 60 * 60_000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${tpe.getUTCFullYear()}-${pad(tpe.getUTCMonth() + 1)}-${pad(tpe.getUTCDate())}`;
}

function localHhmm(dateTime: string | undefined): string | null {
  if (!dateTime) return null;
  if (dateTime.includes("+08:00")) return dateTime.slice(11, 16);
  const ms = Date.parse(dateTime);
  if (Number.isNaN(ms)) return dateTime.slice(11, 16);
  const tpe = new Date(ms + 8 * 60 * 60_000);
  return `${String(tpe.getUTCHours()).padStart(2, "0")}:${String(tpe.getUTCMinutes()).padStart(2, "0")}`;
}

// --- 修訂既有事件 ------------------------------------------------------------

/** 一筆事件的完整樣貌，供修訂前後對照。 */
export interface FullEvent {
  id: string;
  summary: string;
  location: string | null;
  isAllDay: boolean;
  /** 時段事件：原始帶位移的 ISO 字串；全天事件為 null。 */
  startAt: string | null;
  endAt: string | null;
  /** 全天事件：start.date 與 end.date（後者是 Google 的排除式）。 */
  startDate: string | null;
  endDate: string | null;
}

// deno-lint-ignore no-explicit-any
function toFullEvent(item: any): FullEvent {
  const isAllDay = Boolean(item.start?.date);
  return {
    id: String(item.id ?? ""),
    summary: String(item.summary ?? "(無標題)"),
    location: item.location ? String(item.location) : null,
    isAllDay,
    startAt: isAllDay ? null : (item.start?.dateTime ?? null),
    endAt: isAllDay ? null : (item.end?.dateTime ?? null),
    startDate: isAllDay ? (item.start?.date ?? null) : null,
    endDate: isAllDay ? (item.end?.date ?? null) : null,
  };
}

export async function getEvent(eventId: string): Promise<FullEvent | null> {
  const res = await callCalendar(
    `/calendars/${encodeURIComponent(calendarId())}/events/${encodeURIComponent(eventId)}`,
  );
  if (!res.ok) {
    await res.body?.cancel();
    return null;
  }
  return toFullEvent(await res.json());
}

/**
 * 局部更新一筆事件。用 PATCH 而不是 PUT —— 只送要改的欄位，
 * 不會把使用者在日曆 App 上另外填的參加者、提醒、描述洗掉。
 */
export async function patchEvent(
  eventId: string,
  patch: Record<string, unknown>,
): Promise<void> {
  const res = await callCalendar(
    `/calendars/${encodeURIComponent(calendarId())}/events/${encodeURIComponent(eventId)}`,
    { method: "PATCH", body: JSON.stringify(patch) },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(`Google Calendar ${res.status}: ${err?.error?.message ?? ""}`.trim());
  }
  await res.body?.cancel();
}

/**
 * 刪除一筆事件。
 *
 * 410 Gone 代表「已經被刪掉了」—— 對「把它刪掉」這個意圖來說結果相同，
 * 所以視為成功，重複按刪除不會噴錯。
 */
export async function deleteEvent(eventId: string): Promise<void> {
  const res = await callCalendar(
    `/calendars/${encodeURIComponent(calendarId())}/events/${encodeURIComponent(eventId)}`,
    { method: "DELETE" },
  );
  if (!res.ok && res.status !== 410 && res.status !== 404) {
    const err = await res.json().catch(() => ({}));
    throw new Error(`Google Calendar ${res.status}: ${err?.error?.message ?? ""}`.trim());
  }
  await res.body?.cancel();
}
