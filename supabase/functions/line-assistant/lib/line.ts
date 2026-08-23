/** LINE Messaging API：簽章驗證、回覆、確認卡。 */

import { env, requireEnv } from "./env.ts";
import type { CalendarRequestRow, LineEvent } from "./types.ts";

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

/** 2026-08-28T14:00:00+08:00 → { date: "2026/08/28（週五）", time: "14:00" } */
export function splitLocalIso(iso: string): { date: string; time: string } {
  return { date: formatDate(iso.slice(0, 10)), time: iso.slice(11, 16) };
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
