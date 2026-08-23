/** LINE Messaging API：簽章驗證、回覆、確認卡。 */

import { env, requireEnv } from "./env.ts";
import type { CalendarRequestRow, LineEvent, NoteRow } from "./types.ts";
import type { DayEvent, FullEvent } from "./google_calendar.ts";

const REPLY_URL = "https://api.line.me/v2/bot/message/reply";
const PUSH_URL = "https://api.line.me/v2/bot/message/push";

/**
 * 驗證 X-Line-Signature。
 *
 * 計畫書 §6.1：必須使用未經 JSON.parse 或重組的「原始 request body 字串」。
 * 比對用 timing-safe 的逐位元 XOR，避免早退洩漏資訊。
 */
export async function verifySignature(rawBody: string, signature: string | null): Promise<boolean> {
  if (!signature) return false;

  const secret = env("LINE_CHANNEL_SECRET");
  if (!secret) {
    console.error("signature check skipped: LINE_CHANNEL_SECRET not configured");
    return false;
  }

  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(rawBody));
  const expected = base64Encode(new Uint8Array(mac));

  return timingSafeEqual(expected, signature);
}

function base64Encode(bytes: Uint8Array): string {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** LINE userId 的 SHA-256，供稽核長期保存而不留原始 ID。 */
export async function hashActor(userId: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(userId));
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

// --- 送訊息 ------------------------------------------------------------------

// deno-lint-ignore no-explicit-any
type LineMessage = Record<string, any>;

async function callLine(url: string, payload: unknown): Promise<boolean> {
  const token = env("LINE_CHANNEL_ACCESS_TOKEN");
  if (!token) {
    console.error("cannot send: LINE_CHANNEL_ACCESS_TOKEN not configured");
    return false;
  }
  const res = await fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    // 回應內容可能含使用者文字，只記錄狀態碼與 LINE 的錯誤訊息欄位
    let detail = "";
    try {
      const body = await res.json();
      detail = String(body?.message ?? "");
    } catch { /* 非 JSON 回應，忽略 */ }
    console.error(`LINE API ${res.status}: ${detail}`);
    return false;
  }
  return true;
}

/** replyToken 只能用一次，且應盡快回覆（計畫書 §6.1）。 */
export function reply(replyToken: string, messages: LineMessage[]): Promise<boolean> {
  return callLine(REPLY_URL, { replyToken, messages: messages.slice(0, 5) });
}

export function replyText(replyToken: string, text: string): Promise<boolean> {
  return reply(replyToken, [textMessage(text)]);
}

export function push(to: string, messages: LineMessage[]): Promise<boolean> {
  return callLine(PUSH_URL, { to, messages: messages.slice(0, 5) });
}

/** LINE 文字訊息上限 5000 字，留些餘裕。 */
export function textMessage(text: string): LineMessage {
  return { type: "text", text: text.length > 4900 ? text.slice(0, 4897) + "..." : text };
}

// --- 事件輔助 ----------------------------------------------------------------

/** 群組事件必須真的 @ 到 bot 自己；1:1 私訊不需要 @。 */
export function isAddressedToBot(event: LineEvent): boolean {
  if (event.source.type === "user") return true;
  return (event.message?.mention?.mentionees ?? []).some((m) => m.isSelf === true);
}

/**
 * 移除訊息中所有 @mention 的片段，留下純指令文字。
 *
 * 依 mentionee 的 index/length 由後往前切，避免前面的刪除位移後面的索引。
 * LINE 的 index 以 UTF-16 code unit 計算，與 JS 字串索引一致。
 */
export function stripMentions(message: { text: string; mention?: { mentionees: { index: number; length: number }[] } }): string {
  const mentionees = [...(message.mention?.mentionees ?? [])].sort((a, b) => b.index - a.index);
  let text = message.text;
  for (const m of mentionees) {
    text = text.slice(0, m.index) + text.slice(m.index + m.length);
  }
  return text.trim();
}

// --- 確認卡 ------------------------------------------------------------------

const WEEKDAYS = ["日", "一", "二", "三", "四", "五", "六"];

/** YYYY-MM-DD → 2026/08/28（週五） */
export function formatDate(isoDate: string): string {
  const [y, m, d] = isoDate.split("-").map(Number);
  const weekday = WEEKDAYS[new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
  return `${y}/${String(m).padStart(2, "0")}/${String(d).padStart(2, "0")}（週${weekday}）`;
}

/**
 * 把任意帶時區位移的 ISO 字串轉成台北的日期與時分。
 *
 * 不能直接切字串 —— PostgREST 讀 timestamptz 是以 UTC 輸出的
 * （寫進去的 2026-08-25T14:00:00+08:00 讀回來是 2026-08-25T06:00:00+00:00），
 * 直接 slice 會顯示成 06:00。一律轉成瞬間再換算 +08:00。
 */
export function splitLocalIso(iso: string): { date: string; time: string } {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) {
    // 解析不了就退回原字串的樣子，至少不會顯示成空白
    return { date: formatDate(iso.slice(0, 10)), time: iso.slice(11, 16) };
  }
  const t = new Date(ms + 8 * 60 * 60_000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return {
    date: formatDate(`${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())}`),
    time: `${pad(t.getUTCHours())}:${pad(t.getUTCMinutes())}`,
  };
}

/** 把待確認行程渲染成人類可讀的時間敘述。 */
export function describeWhen(req: {
  is_all_day: boolean;
  start_at: string | null;
  end_at: string | null;
  start_date: string | null;
  end_date: string | null;
}): string {
  if (req.is_all_day) {
    const start = formatDate(req.start_date!);
    // end_date 存的是「含首尾」的迄日，直接顯示即可
    return req.start_date === req.end_date ? `${start} 全天` : `${start} 至 ${formatDate(req.end_date!)} 全天`;
  }
  const s = splitLocalIso(req.start_at!);
  const e = splitLocalIso(req.end_at!);
  return s.date === e.date
    ? `${s.date} ${s.time}-${e.time}`
    : `${s.date} ${s.time} 至 ${e.date} ${e.time}`;
}

/**
 * 確認卡：標題、時間、地點、建立者，加上「確認建立 / 取消」兩個 postback 按鈕。
 * 用 Flex Message 以便按鈕在群組中清楚可見。
 */
export function confirmCard(req: CalendarRequestRow, creatorName: string): LineMessage {
  const rows: LineMessage[] = [
    kvRow("類型", req.req_type === "leave" ? "請假" : "會議／行程"),
    kvRow("標題", req.title),
    kvRow("時間", `${describeWhen(req)}\n（${requireTz()}）`),
  ];
  if (req.location) rows.push(kvRow("地點", req.location));
  if (req.note) rows.push(kvRow("備註", req.note));
  rows.push(kvRow("建立者", creatorName));

  return {
    type: "flex",
    altText: `請確認行程：${req.title} ${describeWhen(req)}`,
    contents: {
      type: "bubble",
      body: {
        type: "box",
        layout: "vertical",
        spacing: "md",
        contents: [
          { type: "text", text: "請確認行程", weight: "bold", size: "lg" },
          { type: "separator" },
          { type: "box", layout: "vertical", spacing: "sm", contents: rows },
        ],
      },
      footer: {
        type: "box",
        layout: "horizontal",
        spacing: "sm",
        contents: [
          {
            type: "button",
            style: "primary",
            action: { type: "postback", label: "確認建立", data: `act=confirm&rid=${req.id}` },
          },
          {
            type: "button",
            style: "secondary",
            action: { type: "postback", label: "取消", data: `act=cancel&rid=${req.id}` },
          },
        ],
      },
    },
  };
}

function requireTz(): string {
  return env("DEFAULT_TIMEZONE") ?? "Asia/Taipei";
}

function kvRow(label: string, value: string): LineMessage {
  return {
    type: "box",
    layout: "baseline",
    spacing: "sm",
    contents: [
      { type: "text", text: label, color: "#888888", size: "sm", flex: 2 },
      { type: "text", text: value, wrap: true, size: "sm", flex: 5 },
    ],
  };
}

/** 取得發話者的顯示名稱，失敗時退回「群組成員」。 */
export async function displayName(event: LineEvent): Promise<string> {
  const userId = event.source.userId;
  const token = env("LINE_CHANNEL_ACCESS_TOKEN");
  if (!userId || !token) return "群組成員";

  const url = event.source.groupId
    ? `https://api.line.me/v2/bot/group/${event.source.groupId}/member/${userId}`
    : `https://api.line.me/v2/bot/profile/${userId}`;
  try {
    const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
    if (!res.ok) return "群組成員";
    const profile = await res.json();
    return String(profile?.displayName ?? "群組成員");
  } catch {
    return "群組成員";
  }
}

/** requireEnv 只在真的要送 Google 時才會用到，這裡 re-export 給 index.ts 方便使用。 */
export { requireEnv };

// --- 日檢視 ------------------------------------------------------------------

/**
 * 功能選單。
 *
 * 群組內只 @ 小助手不打其他字時就回這張卡，比丟一大串文字說明好用。
 * 「選日期」用 LINE 原生的 datetimepicker action，使用者滑選即可，
 * 不必手打日期；選完會以 postback.params.date 回傳 YYYY-MM-DD。
 */
export function mainMenuCard(todayIso: string, tomorrowIso: string): LineMessage {
  const btn = (label: string, data: string, displayText: string, style = "secondary") => ({
    type: "button",
    style,
    height: "sm",
    action: { type: "postback", label, data, displayText },
  });

  return {
    type: "flex",
    altText: "課表小助手功能選單",
    contents: {
      type: "bubble",
      body: {
        type: "box",
        layout: "vertical",
        spacing: "md",
        contents: [
          { type: "text", text: "📅 課表小助手", weight: "bold", size: "lg" },
          { type: "text", text: "選一個功能，或直接打字建立行程", size: "xs", color: "#888888", wrap: true },
          { type: "separator" },

          { type: "text", text: "看某一天", size: "sm", weight: "bold", margin: "sm" },
          {
            type: "box",
            layout: "horizontal",
            spacing: "sm",
            contents: [
              btn("今天", `act=day&d=${todayIso}`, "查 今天", "primary"),
              btn("明天", `act=day&d=${tomorrowIso}`, "查 明天", "primary"),
            ],
          },
          {
            type: "button",
            style: "secondary",
            height: "sm",
            action: {
              type: "datetimepicker",
              label: "選其他日期",
              data: "act=day",
              mode: "date",
              initial: todayIso,
              min: "2020-01-01",
              max: "2035-12-31",
            },
          },

          { type: "separator", margin: "md" },
          { type: "text", text: "新增行程", size: "sm", weight: "bold", margin: "sm" },
          {
            type: "button",
            style: "primary",
            height: "sm",
            color: "#2e7d32",
            action: {
              type: "datetimepicker",
              label: "選日期建立行程",
              data: "act=new_date",
              mode: "date",
              initial: todayIso,
              min: "2020-01-01",
              max: "2035-12-31",
            },
          },

          {
            type: "button",
            style: "secondary",
            height: "sm",
            action: {
              type: "datetimepicker",
              label: "修訂／刪除既有行程",
              data: "act=ed_date",
              mode: "date",
              initial: todayIso,
              min: "2020-01-01",
              max: "2035-12-31",
            },
          },

          { type: "separator", margin: "md" },
          { type: "text", text: "記事", size: "sm", weight: "bold", margin: "sm" },
          {
            type: "box",
            layout: "horizontal",
            spacing: "sm",
            contents: [
              btn("查記事", "act=notes", "查記事"),
              btn("使用說明", "act=help", "/help"),
            ],
          },
        ],
      },
      footer: {
        type: "box",
        layout: "vertical",
        contents: [{
          type: "text",
          text: "例：8/28 14:00-16:00 部務會議　請假 8/29 全天",
          size: "xxs",
          color: "#aaaaaa",
          wrap: true,
        }],
      },
    },
  };
}

/** 把某一天的行程與記事組成一則文字訊息。 */
export function renderDayView(dateIso: string, events: DayEvent[], notes: NoteRow[]): string {
  const lines = [`📆 ${formatDate(dateIso)}`, ""];

  lines.push(`【行程】${events.length === 0 ? "沒有安排" : ""}`);
  for (const e of events) {
    const when = e.isAllDay ? "全天" : `${e.startTime}-${e.endTime}`;
    lines.push(`· ${when}　${e.summary}${e.location ? `（${e.location}）` : ""}`);
  }

  lines.push("", `【記事】${notes.length === 0 ? "沒有記事" : ""}`);
  for (const n of notes) {
    // target_date 是 null 代表這是當天寫下、但沒指明日期的記事
    const mark = n.target_date ? "" : "（當天記錄）";
    lines.push(`· #${n.seq}　${n.content}${mark}`);
  }

  if (events.length === 0 && notes.length === 0) {
    lines.push("", "這一天目前是空的。");
  }
  return lines.join("\n");
}

// --- 引導式建立行程 ----------------------------------------------------------

/**
 * 引導流程的時間選擇卡。
 *
 * LINE 的 datetimepicker(mode="time") 會以 postback.params.time 回傳 "HH:mm"，
 * 所以整個流程完全不用打字，直到最後一步輸入標題。
 */
export function timePickCard(
  title: string,
  subtitle: string,
  data: string,
  initial: string,
  label: string,
): LineMessage {
  return {
    type: "flex",
    altText: title,
    contents: {
      type: "bubble",
      body: {
        type: "box",
        layout: "vertical",
        spacing: "md",
        contents: [
          { type: "text", text: title, weight: "bold", size: "lg" },
          { type: "text", text: subtitle, size: "sm", color: "#888888", wrap: true },
          { type: "separator" },
          {
            type: "button",
            style: "primary",
            height: "sm",
            action: { type: "datetimepicker", label, data, mode: "time", initial },
          },
          {
            type: "button",
            style: "secondary",
            height: "sm",
            action: { type: "postback", label: "取消", data: "act=new_cancel", displayText: "取消" },
          },
        ],
      },
    },
  };
}

/**
 * 引導流程的日期選擇卡。與 timePickCard 同一組樣式，只是 mode=date。
 */
export function datePickCard(
  title: string,
  subtitle: string,
  data: string,
  initial: string,
  label: string,
): LineMessage {
  return {
    type: "flex",
    altText: title,
    contents: {
      type: "bubble",
      body: {
        type: "box",
        layout: "vertical",
        spacing: "md",
        contents: [
          { type: "text", text: title, weight: "bold", size: "lg" },
          { type: "text", text: subtitle, size: "sm", color: "#888888", wrap: true },
          { type: "separator" },
          {
            type: "button",
            style: "primary",
            height: "sm",
            action: {
              type: "datetimepicker", label, data, mode: "date",
              initial, min: "2020-01-01", max: "2035-12-31",
            },
          },
          {
            type: "button",
            style: "secondary",
            height: "sm",
            action: { type: "postback", label: "取消", data: "act=ed_cancel", displayText: "取消" },
          },
        ],
      },
    },
  };
}

/** "HH:MM:SS"（Postgres time）或 "HH:MM" 都收，一律回 "HH:MM"。 */
export function hhmm(time: string): string {
  return time.slice(0, 5);
}

/** 在起始時間上加 n 分鐘，用來當結束時間選擇器的預設值。 */
export function addMinutes(time: string, minutes: number): string {
  const [h, m] = hhmm(time).split(":").map(Number);
  const total = (h * 60 + m + minutes) % (24 * 60);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(Math.floor(total / 60))}:${pad(total % 60)}`;
}

// --- 修訂既有行程 ------------------------------------------------------------

/** 一天的事件清單，每筆一顆泡泡加「選這筆」按鈕。 */
export function eventPickerCarousel(dateIso: string, events: DayEvent[]): LineMessage {
  // LINE carousel 上限 12 顆泡泡
  const bubbles = events.slice(0, 12).map((e) => ({
    type: "bubble",
    size: "kilo",
    body: {
      type: "box",
      layout: "vertical",
      spacing: "sm",
      contents: [
        { type: "text", text: e.isAllDay ? "全天" : `${e.startTime}-${e.endTime}`, size: "xs", color: "#888888" },
        { type: "text", text: e.summary, weight: "bold", size: "sm", wrap: true, maxLines: 3 },
        ...(e.location
          ? [{ type: "text", text: `📍 ${e.location}`, size: "xxs", color: "#888888", wrap: true }]
          : []),
      ],
    },
    footer: {
      type: "box",
      layout: "vertical",
      contents: [{
        type: "button",
        style: "primary",
        height: "sm",
        action: {
          type: "postback",
          label: "選這筆",
          data: `act=ed_pick&e=${encodeURIComponent(e.id)}`,
          displayText: `修訂：${e.summary}`,
        },
      }],
    },
  }));

  return {
    type: "flex",
    altText: `${formatDate(dateIso)} 有 ${events.length} 筆行程，請選一筆修訂`,
    contents: { type: "carousel", contents: bubbles },
  };
}

/** 選定一筆之後，問要對它做什麼。 */
export function editActionCard(event: FullEvent): LineMessage {
  const when = event.isAllDay
    ? `${formatDate(event.startDate!)} 全天`
    : `${splitLocalIso(event.startAt!).date} ${splitLocalIso(event.startAt!).time}-${splitLocalIso(event.endAt!).time}`;

  const act = (label: string, data: string, style = "secondary", color?: string) => ({
    type: "button",
    style,
    height: "sm",
    ...(color ? { color } : {}),
    action: { type: "postback", label, data, displayText: label },
  });

  return {
    type: "flex",
    altText: `要對「${event.summary}」做什麼？`,
    contents: {
      type: "bubble",
      body: {
        type: "box",
        layout: "vertical",
        spacing: "md",
        contents: [
          { type: "text", text: "要改什麼？", weight: "bold", size: "lg" },
          { type: "text", text: event.summary, size: "sm", wrap: true },
          { type: "text", text: when, size: "xs", color: "#888888" },
          ...(event.location
            ? [{ type: "text", text: `📍 ${event.location}`, size: "xs", color: "#888888", wrap: true }]
            : []),
          { type: "separator" },
          act("改時間／日期", "act=ed_time", "primary"),
          act("改標題", "act=ed_title"),
          act("改地點", "act=ed_loc"),
          act("複製成新的一筆", "act=ed_copy"),
          { type: "separator", margin: "sm" },
          act("刪除這筆行程", "act=ed_del", "primary", "#c62828"),
          act("取消", "act=ed_cancel"),
        ],
      },
    },
  };
}

/**
 * 修訂確認卡：原本 → 變更後，兩相對照再決定。
 *
 * 跟新建一樣，任何寫入前都要有這一步（計畫書 §1.2）；
 * 刪除因為不可逆，額外把標題重述一次。
 */
export function editConfirmCard(
  heading: string,
  before: string,
  after: string,
  destructive = false,
): LineMessage {
  const row = (label: string, value: string, color: string) => ({
    type: "box",
    layout: "baseline",
    spacing: "sm",
    contents: [
      { type: "text", text: label, color: "#888888", size: "sm", flex: 2 },
      { type: "text", text: value, wrap: true, size: "sm", flex: 5, color },
    ],
  });

  return {
    type: "flex",
    altText: `${heading}：${after}`,
    contents: {
      type: "bubble",
      body: {
        type: "box",
        layout: "vertical",
        spacing: "md",
        contents: [
          { type: "text", text: heading, weight: "bold", size: "lg", color: destructive ? "#c62828" : undefined },
          { type: "separator" },
          {
            type: "box",
            layout: "vertical",
            spacing: "sm",
            contents: [
              row("原本", before, "#888888"),
              row(destructive ? "將刪除" : "改成", after, destructive ? "#c62828" : "#000000"),
            ],
          },
        ],
      },
      footer: {
        type: "box",
        layout: "horizontal",
        spacing: "sm",
        contents: [
          {
            type: "button",
            style: "primary",
            ...(destructive ? { color: "#c62828" } : {}),
            action: { type: "postback", label: destructive ? "確認刪除" : "確認修改", data: "act=ed_apply" },
          },
          {
            type: "button",
            style: "secondary",
            action: { type: "postback", label: "取消", data: "act=ed_cancel" },
          },
        ],
      },
    },
  };
}

/** 把一筆 Google 事件的時間渲染成人類可讀字串，供修訂前後對照。 */
export function describeEvent(event: FullEvent): string {
  if (event.isAllDay) return `${formatDate(event.startDate!)} 全天`;
  const s = splitLocalIso(event.startAt!);
  const e = splitLocalIso(event.endAt!);
  return s.date === e.date ? `${s.date} ${s.time}-${e.time}` : `${s.date} ${s.time} 至 ${e.date} ${e.time}`;
}
