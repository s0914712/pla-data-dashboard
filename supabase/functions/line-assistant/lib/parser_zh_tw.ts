/**
 * 繁體中文行程／記事解析器（規則式，計畫書 §5.2）。
 *
 * 全部是純函式、不做任何 IO，"now" 一律由呼叫端傳入，所以可完整單元測試。
 * 時間一律以 Asia/Taipei 解讀；台灣沒有日光節約時間，位移固定 +08:00。
 */

import type { ParsedSchedule, ParseResult, ReqType } from "./types.ts";

const TPE_OFFSET_MIN = 8 * 60;

// --- Asia/Taipei 日曆運算 ----------------------------------------------------

export interface YMD {
  y: number;
  m: number;
  d: number;
}

/** 把 UTC 瞬間換成台北牆上時間的年月日時分。 */
export function taipeiParts(now: Date): YMD & { hh: number; mi: number } {
  const shifted = new Date(now.getTime() + TPE_OFFSET_MIN * 60_000);
  return {
    y: shifted.getUTCFullYear(),
    m: shifted.getUTCMonth() + 1,
    d: shifted.getUTCDate(),
    hh: shifted.getUTCHours(),
    mi: shifted.getUTCMinutes(),
  };
}

function toEpochDay(ymd: YMD): number {
  return Date.UTC(ymd.y, ymd.m - 1, ymd.d) / 86_400_000;
}

function fromEpochDay(day: number): YMD {
  const dt = new Date(day * 86_400_000);
  return { y: dt.getUTCFullYear(), m: dt.getUTCMonth() + 1, d: dt.getUTCDate() };
}

export function addDays(ymd: YMD, n: number): YMD {
  return fromEpochDay(toEpochDay(ymd) + n);
}

/** 0 = 週日 ... 6 = 週六 */
function weekdayOf(ymd: YMD): number {
  return new Date(Date.UTC(ymd.y, ymd.m - 1, ymd.d)).getUTCDay();
}

export function isoDate(ymd: YMD): string {
  return `${ymd.y}-${String(ymd.m).padStart(2, "0")}-${String(ymd.d).padStart(2, "0")}`;
}

export function isoDateTime(ymd: YMD, hh: number, mi: number): string {
  return `${isoDate(ymd)}T${String(hh).padStart(2, "0")}:${String(mi).padStart(2, "0")}:00+08:00`;
}

function isValidYmd(y: number, m: number, d: number): boolean {
  if (m < 1 || m > 12 || d < 1 || d > 31) return false;
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
}

// --- 正規化 ------------------------------------------------------------------

const FULLWIDTH_DIGITS = "０１２３４５６７８９";

/** 全形轉半形、標點統一，讓後續規則只需處理一種寫法。 */
export function normalize(input: string): string {
  let out = "";
  for (const ch of input) {
    const digit = FULLWIDTH_DIGITS.indexOf(ch);
    if (digit >= 0) { out += String(digit); continue; }
    const code = ch.codePointAt(0)!;
    // 全形英數與符號 U+FF01–U+FF5E 對應到 ASCII
    if (code >= 0xff01 && code <= 0xff5e) { out += String.fromCharCode(code - 0xfee0); continue; }
    out += ch;
  }
  return out
    .replace(/[　]/g, " ")
    .replace(/[～〜]/g, "~")
    .replace(/[—–—]/g, "-")
    .replace(/[「」『』]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

// --- 日期解析 ----------------------------------------------------------------

const RE_YMD = /(\d{4})\s*[/\-.年]\s*(\d{1,2})\s*[/\-.月]\s*(\d{1,2})\s*[日號]?/;
const RE_MD = /(\d{1,2})\s*[/月]\s*(\d{1,2})\s*[日號]?/;
const RE_REL = /(今天|今日|本日|明天|明日|後天|大後天)/;
const RE_WD = /(下下|下|這|本)?\s*(?:週|周|星期|禮拜)\s*([一二三四五六日天])/;

const WD_INDEX: Record<string, number> = { 日: 0, 天: 0, 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6 };
const REL_OFFSET: Record<string, number> = {
  今天: 0, 今日: 0, 本日: 0, 明天: 1, 明日: 1, 後天: 2, 大後天: 3,
};

interface DateHit {
  ymd: YMD;
  /** 訊息裡是否明確寫了年份 —— 影響「過期日期」的處理方式。 */
  explicitYear: boolean;
  start: number;
  end: number;
}

/** 從 text 的 from 位置起，找出第一個日期。找不到回 null。 */
function findDate(text: string, from: number, today: YMD): DateHit | null {
  const slice = text.slice(from);
  const candidates: DateHit[] = [];

  const ymd = RE_YMD.exec(slice);
  if (ymd) {
    const [y, m, d] = [Number(ymd[1]), Number(ymd[2]), Number(ymd[3])];
    if (isValidYmd(y, m, d)) {
      candidates.push({
        ymd: { y, m, d }, explicitYear: true,
        start: from + ymd.index, end: from + ymd.index + ymd[0].length,
      });
    }
  }

  const md = RE_MD.exec(slice);
  if (md) {
    const [m, d] = [Number(md[1]), Number(md[2])];
    if (isValidYmd(today.y, m, d)) {
      // 無年份：選「距現在最近且尚未過期」的未來日期（計畫書 §5.2）
      let year = today.y;
      if (toEpochDay({ y: year, m, d }) < toEpochDay(today)) year += 1;
      candidates.push({
        ymd: { y: year, m, d }, explicitYear: false,
        start: from + md.index, end: from + md.index + md[0].length,
      });
    }
  }

  const rel = RE_REL.exec(slice);
  if (rel) {
    candidates.push({
      ymd: addDays(today, REL_OFFSET[rel[1]]), explicitYear: false,
      start: from + rel.index, end: from + rel.index + rel[0].length,
    });
  }

  const wd = RE_WD.exec(slice);
  if (wd) {
    const target = WD_INDEX[wd[2]];
    // 本週以週一為首日
    const mondayOffset = (weekdayOf(today) + 6) % 7;
    const thisMonday = addDays(today, -mondayOffset);
    const targetOffset = (target + 6) % 7; // 週一=0 ... 週日=6
    let hit: YMD;
    if (wd[1] === "下") hit = addDays(thisMonday, targetOffset + 7);
    else if (wd[1] === "下下") hit = addDays(thisMonday, targetOffset + 14);
    else if (wd[1] === "這" || wd[1] === "本") hit = addDays(thisMonday, targetOffset);
    else {
      // 無前綴：最近一次（含今天）
      const delta = (target - weekdayOf(today) + 7) % 7;
      hit = addDays(today, delta);
    }
    candidates.push({
      ymd: hit, explicitYear: false,
      start: from + wd.index, end: from + wd.index + wd[0].length,
    });
  }

  if (candidates.length === 0) return null;
  // 取文字中最早出現的那一個；同位置時較長的優先（YMD 勝過 MD）
  candidates.sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));
  return candidates[0];
}

// --- 時間解析 ----------------------------------------------------------------

const RE_TIME = /(上午|早上|凌晨|清晨|中午|下午|晚上|傍晚|深夜)?\s*(\d{1,2})\s*(?::|點|时|時)\s*(半|\d{1,2})?\s*分?/;

interface TimeHit {
  hh: number;
  mi: number;
  /** 使用者是否明確寫了上午／下午。決定區間的第二個時間能否繼承。 */
  hadMeridiem: boolean;
  start: number;
  end: number;
}

function findTime(text: string, from: number): TimeHit | null {
  const m = RE_TIME.exec(text.slice(from));
  if (!m) return null;

  let hh = Number(m[2]);
  const mi = m[3] === "半" ? 30 : m[3] ? Number(m[3]) : 0;
  if (hh > 23 || mi > 59) return null;

  const meridiem = m[1];
  if (meridiem === "下午" || meridiem === "晚上" || meridiem === "傍晚" || meridiem === "深夜") {
    if (hh < 12) hh += 12;
  } else if (meridiem === "中午") {
    if (hh < 12 && hh !== 12) hh += 12;
  } else if (meridiem === "上午" || meridiem === "早上" || meridiem === "凌晨" || meridiem === "清晨") {
    if (hh === 12) hh = 0;
  }
  if (hh > 23) return null;

  return {
    hh, mi, hadMeridiem: meridiem !== undefined,
    start: from + m.index, end: from + m.index + m[0].length,
  };
}

const RANGE_SEP = /^\s*(?:-|~|to|至|到|~)\s*/i;

/** 找出時間區間（起-迄）或單一時間。 */
function findTimeRange(
  text: string,
): { start: TimeHit; end: TimeHit | null; from: number; to: number } | null {
  const first = findTime(text, 0);
  if (!first) return null;

  const rest = text.slice(first.end);
  const sep = RANGE_SEP.exec(rest);
  if (sep) {
    const second = findTime(text, first.end + sep[0].length);
    // 第二個時間必須緊接在分隔符後面，否則那是句子後段另一個時間
    if (second && second.start === first.end + sep[0].length) {
      // 「下午2點-4點半」：第二段沒寫上下午時，繼承第一段的。
      if (!second.hadMeridiem && first.hadMeridiem && second.hh < 12 && first.hh >= 12) {
        second.hh += 12;
      }
      return { start: first, end: second, from: first.start, to: second.end };
    }
  }
  return { start: first, end: null, from: first.start, to: first.end };
}

// --- 欄位抽取 ----------------------------------------------------------------

const RE_LOCATION = /(?:地點|地点|位置|場地|地方)\s*[:：]\s*([^,，;；]+)/;
const RE_NOTE = /(?:備註|备注|附註|說明)\s*[:：]\s*(.+)$/;
const RE_TRAILING_PLACE = /[,，]\s*([^,，]*(?:室|廳|館|樓|中心|基地|營區|會場|大樓))\s*$/;
const RE_ALLDAY = /(全天|整天|一整天|全日)/;

function cut(text: string, from: number, to: number): string {
  return (text.slice(0, from) + " " + text.slice(to)).replace(/\s+/g, " ").trim();
}

// --- 主解析 ------------------------------------------------------------------

const NOTE_PREFIX = /^(?:記事|記錄|記録|筆記|备忘|備忘|note)\s*[:：]?\s*/i;
const NOTE_LIST = /^(?:查記事|查筆記|記事列表|筆記列表|我的記事|列出記事)\s*[:：]?\s*(.*)$/;
/** 裸的「記事」「筆記」二字（後面沒有內容）視為查詢，而不是空記事。 */
const NOTE_LIST_BARE = /^(?:記事|筆記|備忘)$/;
const NOTE_DELETE = /^(?:刪記事|删記事|刪除記事|刪筆記|刪除筆記)\s*#?\s*(\d+)\s*$/;
const SCHEDULE_PREFIX = /^(?:新增|新增行程|建立|加入|安排|行程)\s*[:：]?\s*/;
const LEAVE_KEYWORD = /(請假|休假|補假|事假|病假|特休)/;

/**
 * 解析一則已去除 @mention 的訊息。
 *
 * @param raw  使用者輸入（已 strip mention）
 * @param now  現在時刻（UTC 瞬間）；測試時可注入固定值
 */
export function parse(raw: string, now: Date): ParseResult {
  const text = normalize(raw);
  if (!text) return { kind: "unknown" };

  // 1. 斜線指令
  if (text.startsWith("/")) {
    const [, name = "", arg = ""] = /^\/(\S+)\s*(.*)$/.exec(text) ?? [];
    return { kind: "command", value: { name: name.toLowerCase(), arg: arg.trim() } };
  }

  // 2. 記事相關（順序重要：刪除與查詢要比「新增記事」先比對）
  const del = NOTE_DELETE.exec(text);
  if (del) return { kind: "command", value: { name: "note_delete", arg: del[1] } };

  if (NOTE_LIST_BARE.test(text)) return { kind: "command", value: { name: "note_list", arg: "" } };

  const list = NOTE_LIST.exec(text);
  if (list) return { kind: "command", value: { name: "note_list", arg: list[1].trim() } };

  if (NOTE_PREFIX.test(text)) {
    const content = text.replace(NOTE_PREFIX, "").trim();
    if (!content) return { kind: "incomplete", reason: "記事內容是空的。請試：記事 下週要交防務報告" };
    const tags = [...content.matchAll(/#([^\s#]+)/g)].map((m) => m[1]);
    return { kind: "note", value: { content, tags } };
  }

  // 3. 行程
  return parseSchedule(text, now);
}

function parseSchedule(text: string, now: Date): ParseResult {
  const today = taipeiParts(now);
  const isLeave = LEAVE_KEYWORD.test(text);

  let rest = text.replace(SCHEDULE_PREFIX, "").trim();

  // 抽出備註與地點（在動日期之前，因為它們可能含數字）
  let note: string | undefined;
  const noteHit = RE_NOTE.exec(rest);
  if (noteHit) {
    note = noteHit[1].trim();
    rest = cut(rest, noteHit.index, noteHit.index + noteHit[0].length);
  }

  let location: string | undefined;
  const locHit = RE_LOCATION.exec(rest);
  if (locHit) {
    location = locHit[1].trim();
    rest = cut(rest, locHit.index, locHit.index + locHit[0].length);
  }

  // 日期（可能是區間）
  const first = findDate(rest, 0, today);
  if (!first) {
    return {
      kind: "incomplete",
      reason: "看不出日期。請加上日期，例如：8/28 14:00-16:00 部務會議",
    };
  }

  const startYmd = first.ymd;
  let endYmd = first.ymd;
  let dateTo = first.end;

  const afterFirst = rest.slice(first.end);
  const dateSep = RANGE_SEP.exec(afterFirst);
  if (dateSep) {
    const second = findDate(rest, first.end + dateSep[0].length, today);
    if (second && second.start === first.end + dateSep[0].length) {
      // 8/29-8/31 這種寫法：第二段若只有月日且早於起日，視為跨年
      endYmd = second.ymd;
      if (toEpochDay(endYmd) < toEpochDay(startYmd) && !second.explicitYear) {
        endYmd = { ...endYmd, y: endYmd.y + 1 };
      }
      dateTo = second.end;
    }
  }

  const explicitYear = first.explicitYear;
  rest = cut(rest, first.start, dateTo);

  // 多筆事件偵測：日期都拿掉後不該再出現另一個日期（計畫書 §5.2）
  if (findDate(rest, 0, today)) {
    return {
      kind: "incomplete",
      reason: "一次只能建立一筆行程，偵測到多個日期。請分成多則訊息輸入。",
    };
  }

  // 時間
  const allDayRequested = RE_ALLDAY.test(rest);
  const timeRange = allDayRequested ? null : findTimeRange(rest);
  if (timeRange) rest = cut(rest, timeRange.from, timeRange.to);
  rest = rest.replace(RE_ALLDAY, " ").trim();

  // 剩下的第一段就是標題；地點的逗號尾綴在此處補撈
  let title = rest.replace(LEAVE_KEYWORD, (m) => m).trim();
  if (!location) {
    const trailing = RE_TRAILING_PLACE.exec(title);
    if (trailing) {
      location = trailing[1].trim();
      title = title.slice(0, trailing.index).trim();
    }
  }
  title = title.replace(/^[,，、:：\-~\s]+|[,，、:：\-~\s]+$/g, "").trim();

  if (!title) {
    if (!isLeave) {
      return { kind: "incomplete", reason: "缺少標題。請補上這筆行程要叫什麼，例如：8/28 14:00-16:00 部務會議" };
    }
    title = "請假";
  }

  // 組出 schedule
  const reqType: ReqType = isLeave ? "leave" : "meeting";
  const base = { reqType, title, location, note };

  if (!timeRange) {
    // 請假只有日期 → 全天；會議只有日期 → 補問時間
    if (!isLeave) {
      return { kind: "incomplete", reason: `「${title}」缺少時間。請補上起訖時間，例如：${today.m}/${today.d} 14:00-16:00` };
    }
    return okSchedule({ ...base, isAllDay: true, startDate: isoDate(startYmd), endDate: isoDate(endYmd) }, now, explicitYear);
  }

  if (!timeRange.end) {
    return {
      kind: "incomplete",
      reason: `「${title}」只有開始時間 ${String(timeRange.start.hh).padStart(2, "0")}:${String(timeRange.start.mi).padStart(2, "0")}，缺少結束時間。請補上，例如：14:00-16:00`,
    };
  }

  const s = timeRange.start;
  const e = timeRange.end;
  let realEndYmd = endYmd;
  // 同一天而結束早於開始（例如 22:00-01:00）視為跨日到隔天
  if (toEpochDay(startYmd) === toEpochDay(endYmd) && (e.hh * 60 + e.mi) <= (s.hh * 60 + s.mi)) {
    realEndYmd = addDays(endYmd, 1);
  }

  return okSchedule({
    ...base,
    isAllDay: false,
    startAt: isoDateTime(startYmd, s.hh, s.mi),
    endAt: isoDateTime(realEndYmd, e.hh, e.mi),
  }, now, explicitYear);
}

/**
 * 最後一道檢查：明確寫了年份卻指向過去的日期，依計畫書 §5.2 不自動改到明年，
 * 而是請使用者確認或重新輸入。
 */
function okSchedule(schedule: ParsedSchedule, now: Date, explicitYear: boolean): ParseResult {
  if (explicitYear) {
    const startMs = schedule.isAllDay
      ? Date.parse(`${schedule.startDate}T23:59:59+08:00`)
      : Date.parse(schedule.startAt!);
    if (startMs < now.getTime()) {
      return {
        kind: "incomplete",
        reason: "這個日期已經過去了。若要建立過去的行程請重新輸入並註明年份，或改用未來日期。",
      };
    }
  }
  return { kind: "schedule", value: schedule };
}

/** 全天事件寫入 Google 時，end.date 為排除式（迄日 +1 天）。 */
export function exclusiveEndDate(inclusiveEndDate: string): string {
  const [y, m, d] = inclusiveEndDate.split("-").map(Number);
  return isoDate(addDays({ y, m, d }, 1));
}
