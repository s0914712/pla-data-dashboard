/**
 * 繁體中文行程／記事解析器（規則式，計畫書 §5.2）。
 *
 * 全部是純函式、不做任何 IO，"now" 一律由呼叫端傳入，所以可完整單元測試。
 * 時間一律以 Asia/Taipei 解讀；台灣沒有日光節約時間，位移固定 +08:00。
 */

import type { ParsedSchedule, ParseResult, QueryTarget, ReqType } from "./types.ts";

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

const RE_YMD = /(\d{4})\s*[/\-.年]\s*(\d{1,2})\s*[/\-.月]\s*(\d{1,2})(?:\s*[日號])?/g;
const RE_MD = /(\d{1,2})\s*[/月]\s*(\d{1,2})(?:\s*[日號])?/g;
const RE_REL = /(今天|今日|本日|明天|明日|後天|大後天)/g;
const RE_WD = /(下下|下|這|本)?\s*(?:週|周|星期|禮拜)\s*([一二三四五六日天])/g;

/** 數字型日期前後不可緊鄰其他數字，否則電話 02/2345 之類會被誤判成 2 月 23 日。 */
function digitAdjacent(text: string, start: number, end: number): boolean {
  const before = text[start - 1];
  const after = text[end];
  const isDigit = (c: string | undefined) => c !== undefined && c >= "0" && c <= "9";
  return isDigit(before) || isDigit(after);
}

/** 在 text[from..] 內逐一取出某個 pattern 的所有比對結果。 */
function* scan(re: RegExp, text: string, from: number): Generator<RegExpExecArray> {
  re.lastIndex = from;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    yield m;
    if (m.index === re.lastIndex) re.lastIndex++;
  }
}

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

/**
 * 從 text 的 from 位置起，找出第一個日期。找不到回 null。
 *
 * 四種寫法各自掃描、取第一個「合法」的比對（而不是第一個比對就放棄），
 * 最後挑文字中出現位置最早的那個。
 */
function findDate(text: string, from: number, today: YMD, nearest = false): DateHit | null {
  const candidates: DateHit[] = [];

  for (const m of scan(RE_YMD, text, from)) {
    const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
    if (digitAdjacent(text, m.index, m.index + m[0].length)) continue;
    if (!isValidYmd(y, mo, d)) continue;
    candidates.push({
      ymd: { y, m: mo, d }, explicitYear: true,
      start: m.index, end: m.index + m[0].length,
    });
    break;
  }

  for (const m of scan(RE_MD, text, from)) {
    const [mo, d] = [Number(m[1]), Number(m[2])];
    if (digitAdjacent(text, m.index, m.index + m[0].length)) continue;
    if (!isValidYmd(today.y, mo, d)) continue;
    // 無年份：建立行程時選「距現在最近且尚未過期」的未來日期（計畫書 §5.2）；
    // 查詢時改選「絕對距離最近」的那一年 —— 9/1 查 8/28 要的是四天前，
    // 不是三百六十四天後。
    let year = today.y;
    if (nearest) {
      let bestDist = Infinity;
      for (const y of [today.y - 1, today.y, today.y + 1]) {
        if (!isValidYmd(y, mo, d)) continue;
        const dist = Math.abs(toEpochDay({ y, m: mo, d }) - toEpochDay(today));
        if (dist < bestDist) { bestDist = dist; year = y; }
      }
    } else if (toEpochDay({ y: year, m: mo, d }) < toEpochDay(today)) year += 1;
    candidates.push({
      ymd: { y: year, m: mo, d }, explicitYear: false,
      start: m.index, end: m.index + m[0].length,
    });
    break;
  }

  for (const m of scan(RE_REL, text, from)) {
    candidates.push({
      ymd: addDays(today, REL_OFFSET[m[1]]), explicitYear: false,
      start: m.index, end: m.index + m[0].length,
    });
    break;
  }

  for (const m of scan(RE_WD, text, from)) {
    const target = WD_INDEX[m[2]];
    // 本週以週一為首日
    const mondayOffset = (weekdayOf(today) + 6) % 7;
    const thisMonday = addDays(today, -mondayOffset);
    const targetOffset = (target + 6) % 7; // 週一=0 ... 週日=6
    let hit: YMD;
    if (m[1] === "下") hit = addDays(thisMonday, targetOffset + 7);
    else if (m[1] === "下下") hit = addDays(thisMonday, targetOffset + 14);
    else if (m[1] === "這" || m[1] === "本") hit = addDays(thisMonday, targetOffset);
    else {
      // 無前綴：最近一次（含今天）
      const delta = (target - weekdayOf(today) + 7) % 7;
      hit = addDays(today, delta);
    }
    candidates.push({
      ymd: hit, explicitYear: false,
      start: m.index, end: m.index + m[0].length,
    });
    break;
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

/** 套用上午／下午修正；回 null 表示這個組合不合法。 */
function applyMeridiem(hh: number, meridiem: string | undefined): number | null {
  if (meridiem === "下午" || meridiem === "晚上" || meridiem === "傍晚" || meridiem === "深夜") {
    if (hh < 12) hh += 12;
  } else if (meridiem === "中午") {
    if (hh < 12 && hh !== 12) hh += 12;
  } else if (meridiem === "上午" || meridiem === "早上" || meridiem === "凌晨" || meridiem === "清晨") {
    if (hh === 12) hh = 0;
  }
  return hh > 23 ? null : hh;
}

function findColonTime(text: string, from: number): TimeHit | null {
  const m = RE_TIME.exec(text.slice(from));
  if (!m) return null;

  const rawHh = Number(m[2]);
  const mi = m[3] === "半" ? 30 : m[3] ? Number(m[3]) : 0;
  if (rawHh > 23 || mi > 59) return null;

  const hh = applyMeridiem(rawHh, m[1]);
  if (hh === null) return null;

  return {
    hh, mi, hadMeridiem: m[1] !== undefined,
    start: from + m.index, end: from + m.index + m[0].length,
  };
}

/**
 * 四位數的軍用寫法：1600、0830、下午0400。
 *
 * 前後必須是空白、字串端或區間分隔符，否則「第1200梯次」「編號0830轉3」
 * 這種含四位數字的字串會被讀成時間。冒號寫法沒有這個問題（冒號本身就是
 * 夠強的訊號），所以只有這條需要邊界檢查。
 */
// 上下午與 \s* 綁在同一個非擷取群組裡：沒寫上下午時，比對必須從數字本身開始，
// 否則「請假 1330-1730」的比對會從空白起算，邊界檢查看到的是「假」而不是空白。
const RE_TIME_BARE = /(?:(上午|早上|凌晨|清晨|中午|下午|晚上|傍晚|深夜)\s*)?([01]\d|2[0-3])([0-5]\d)/g;
const TOKEN_BOUNDARY = /[\s\-~至到,，、。]/;

function findBareTime(text: string, from: number): TimeHit | null {
  for (const m of scan(RE_TIME_BARE, text, from)) {
    const start = m.index;
    const end = m.index + m[0].length;
    const before = text[start - 1];
    const after = text[end];
    if (before !== undefined && !TOKEN_BOUNDARY.test(before)) continue;
    if (after !== undefined && !TOKEN_BOUNDARY.test(after)) continue;

    const hh = applyMeridiem(Number(m[2]), m[1]);
    if (hh === null) continue;
    return { hh, mi: Number(m[3]), hadMeridiem: m[1] !== undefined, start, end };
  }
  return null;
}

/** 兩種寫法都掃，取文字中出現位置較早的那個。 */
function findTime(text: string, from: number): TimeHit | null {
  const colon = findColonTime(text, from);
  const bare = findBareTime(text, from);
  if (!colon) return bare;
  if (!bare) return colon;
  return bare.start < colon.start ? bare : colon;
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
/**
 * 查詢指令。記事與行程共用同一個入口 —— 「查記事」與「查行程」完全同義，
 * 查出來的都是同一份合併檢視（行程／請假／記事三區）。
 */
const QUERY_PREFIX =
  /^(?:查記事|查筆記|記事列表|筆記列表|我的記事|列出記事|查行程|查排程|行程列表|排程列表|我的行程|列出行程)\s*[:：]?\s*(.*)$/;
/** 裸的「記事」「行程」二字（後面沒有內容）視為查詢，而不是空記事。 */
const QUERY_BARE = /^(?:記事|筆記|備忘|行程|排程)$/;
const NOTE_DELETE = /^(?:刪記事|删記事|刪除記事|刪筆記|刪除筆記)\s*#?\s*(\d+)\s*$/;
const SCHEDULE_PREFIX = /^(?:新增|新增行程|建立|加入|安排|行程)\s*[:：]?\s*/;
const LEAVE_KEYWORD = /(請假|休假|補假|事假|病假|特休)/;

/** 叫出日期選單。 */
const MENU_RE = /^(?:選單|menu|功能|查詢|動態|查什麼)$/i;
/** 日檢視查詢的前綴與後綴，剝掉之後應只剩一個日期。 */
const DAY_QUERY_LEAD = /^(?:查詢|查|看|顯示|列出)\s*/;
/**
 * 只看請假的兩種寫法。
 *
 * 刻意分成兩條：「明天請假」在計畫書 §5.1 是「我明天要請假」的建立語句，
 * 不能被當成查詢。所以裸的「…請假」結尾只有在帶了查／看／列出前綴時才算
 * 查詢；沒有前綴時必須用「誰請假」「請假名單」這種明確的問法。
 */
const DAY_QUERY_LEAVE_EXPLICIT = /\s*(?:誰請假|誰不在|請假名單|請假狀況|休假名單)$/;
const DAY_QUERY_LEAVE_SUFFIX = /\s*的?(?:請假|休假)$/;
/** 帶前綴時類別詞可能在日期前面：「查請假 8/28」。 */
const DAY_QUERY_LEAVE_PREFIX = /^的?(?:請假|休假)\s*/;
const DAY_QUERY_MEETING_PREFIX = /^的?(?:會議|開會)\s*/;
/** 只看會議：「明天會議」。「行程」保留為全部，因為它是泛稱。 */
const DAY_QUERY_MEETING = /\s*的?(?:會議|開會)$/;
const DAY_QUERY_TAIL = /\s*(?:的?(?:行程|動態|排程|安排|活動|事情)|有什麼事?|有什麼|有啥|要做什麼)$/;

/**
 * 判斷一則訊息是不是「查詢」（沒有指令詞的那種寫法）。
 *
 * 規則：剝掉前後綴後，剩下的字必須「剛好只有一段日期」。
 * 這樣「明天 09:00-10:30 週報會議」不會被誤判成查詢（拿掉日期後還有東西），
 * 而「明天」「明天行程」「查 8/28」「8/28-8/31」「本週」會。
 */
function tryBareQuery(text: string, today: YMD): QueryTarget | null {
  const hadLead = DAY_QUERY_LEAD.test(text);
  let body = text.replace(DAY_QUERY_LEAD, "");

  // 先看有沒有只查某一類的後綴；剝掉之後才輪到通用後綴
  let filter: QueryTarget["filter"] = "all";
  if (DAY_QUERY_LEAVE_EXPLICIT.test(body)) {
    filter = "leave";
    body = body.replace(DAY_QUERY_LEAVE_EXPLICIT, "");
  } else if (hadLead && DAY_QUERY_LEAVE_SUFFIX.test(body)) {
    filter = "leave";
    body = body.replace(DAY_QUERY_LEAVE_SUFFIX, "");
  } else if (hadLead && DAY_QUERY_LEAVE_PREFIX.test(body)) {
    filter = "leave";
    body = body.replace(DAY_QUERY_LEAVE_PREFIX, "");
  } else if (hadLead && DAY_QUERY_MEETING_PREFIX.test(body)) {
    filter = "meeting";
    body = body.replace(DAY_QUERY_MEETING_PREFIX, "");
  } else if (DAY_QUERY_MEETING.test(body)) {
    filter = "meeting";
    body = body.replace(DAY_QUERY_MEETING, "");
  }

  const stripped = body.replace(DAY_QUERY_TAIL, "").trim();

  // 「請假」「誰請假」單獨出現時沒有日期，預設查今天
  if (!stripped) {
    return filter === "all" ? null : { from: isoDate(today), to: isoDate(today), filter, keyword: "" };
  }

  const span = parseSpan(stripped, today);
  if (!span) return null;

  // 日期以外還剩字 → 這是建立語句，交給行程解析
  if (cut(stripped, span.start, span.end) !== "") return null;
  return { from: isoDate(span.from), to: isoDate(span.to), filter, keyword: "" };
}

// --- 查詢參數 ----------------------------------------------------------------

/**
 * 只查請假／只查會議的類別詞，出現在日期前面時。
 *
 * 後面必須接空白、數字或字串結尾 —— 否則「查記事 請假流程」會把「請假」
 * 當成類別詞剝掉，只剩「流程」當關鍵字。
 */
const QUERY_FILTER_PREFIX: [RegExp, QueryTarget["filter"]][] = [
  [/^的?(?:請假|休假|不在)(?=$|[\s\d])\s*/, "leave"],
  [/^的?(?:會議|開會)(?=$|[\s\d])\s*/, "meeting"],
];
/** 同上，出現在日期後面時。 */
const QUERY_FILTER_SUFFIX: [RegExp, QueryTarget["filter"]][] = [
  [/\s*的?(?:誰請假|誰不在|請假名單|請假狀況|休假名單|請假|休假)$/, "leave"],
  [/\s*的?(?:會議|開會)$/, "meeting"],
];

const RE_PERIOD_WEEK = /^(本|這|这|上|下|下下)\s*(?:週|周|星期|禮拜)(?![一二三四五六日天])/;
const RE_PERIOD_MONTH = /^(本|這|这|上|下)\s*個?\s*月/;
const RE_RECENT_DAYS = /^(?:最近|近|過去)\s*(\d{1,3})\s*[天日]/;
const RE_RECENT_WEEK = /^(?:最近|近|過去)\s*(?:一|1)?\s*(?:週|周|星期|禮拜)/;
const RE_RECENT_MONTH = /^(?:最近|近|過去)\s*(?:一|1)?\s*個?\s*月/;

const PERIOD_OFFSET: Record<string, number> = { 本: 0, 這: 0, 这: 0, 上: -1, 下: 1, 下下: 2 };

/** 「本週」「上個月」「最近 7 天」這類期間詞。回 null 表示開頭不是期間詞。 */
function tryPeriod(text: string, today: YMD): { from: YMD; to: YMD; end: number } | null {
  const days = RE_RECENT_DAYS.exec(text);
  if (days) {
    const n = Math.max(1, Number(days[1]));
    return { from: addDays(today, -(n - 1)), to: today, end: days[0].length };
  }

  const recentWeek = RE_RECENT_WEEK.exec(text);
  if (recentWeek) return { from: addDays(today, -6), to: today, end: recentWeek[0].length };

  const recentMonth = RE_RECENT_MONTH.exec(text);
  if (recentMonth) return { from: addDays(today, -29), to: today, end: recentMonth[0].length };

  const week = RE_PERIOD_WEEK.exec(text);
  if (week) {
    // 週一為首日，跟 RE_WD 的「本週」定義一致
    const mondayOffset = (weekdayOf(today) + 6) % 7;
    const monday = addDays(addDays(today, -mondayOffset), (PERIOD_OFFSET[week[1]] ?? 0) * 7);
    return { from: monday, to: addDays(monday, 6), end: week[0].length };
  }

  const month = RE_PERIOD_MONTH.exec(text);
  if (month) {
    let y = today.y;
    let m = today.m + (PERIOD_OFFSET[month[1]] ?? 0);
    while (m < 1) { m += 12; y -= 1; }
    while (m > 12) { m -= 12; y += 1; }
    const first = { y, m, d: 1 };
    const nextFirst = m === 12 ? { y: y + 1, m: 1, d: 1 } : { y, m: m + 1, d: 1 };
    return { from: first, to: addDays(nextFirst, -1), end: month[0].length };
  }

  return null;
}

interface DateSpan {
  from: YMD;
  to: YMD;
  /** 這段日期在原字串中佔用的範圍，供呼叫端切掉後判斷還剩什麼。 */
  start: number;
  end: number;
}

/**
 * 從一段文字裡找出「一天」或「一段日期」。
 *
 * 期間詞（本週）只認開頭；單一日期與日期區間（8/28-8/31）可以出現在任何位置。
 * 查詢一律用 nearest 解無年份的月日 —— 9/1 問 8/28 指的是四天前。
 */
function parseSpan(text: string, today: YMD): DateSpan | null {
  const period = tryPeriod(text, today);
  if (period) return { from: period.from, to: period.to, start: 0, end: period.end };

  const first = findDate(text, 0, today, true);
  if (!first) return null;

  let last = first;
  let end = first.end;
  const sep = RANGE_SEP.exec(text.slice(first.end));
  if (sep) {
    const second = findDate(text, first.end + sep[0].length, today, true);
    if (second && second.start === first.end + sep[0].length) {
      last = second;
      end = second.end;
    }
  }

  let from = first.ymd;
  let to = last.ymd;
  // 迄日早於起日：沒寫年份就當跨年（12/28-1/3），寫了年份就當使用者寫反了
  if (toEpochDay(to) < toEpochDay(from)) {
    if (last.explicitYear) [from, to] = [to, from];
    else to = { ...to, y: to.y + 1 };
  }
  return { from, to, start: first.start, end };
}

/**
 * 解析「查記事」「查行程」後面那段參數 —— 兩個指令完全同義。
 *
 * 三種成分可以任意組合：類別詞（請假／會議）、日期或期間、關鍵字。
 *
 *   （空）        → 今天
 *   8/28          → 那一天
 *   8/28-8/31     → 區間
 *   本週          → 期間詞
 *   報告          → 只有關鍵字，日期交給呼叫端決定預設範圍
 *   本週 請假      → 區間 + 只看請假
 *   本週 報告      → 區間 + 關鍵字
 *
 * 這裡不像 tryBareQuery 需要 hadLead 判斷 —— 使用者已經明確打了查詢指令，
 * 「查記事 請假」不可能是「我要請假」。
 */
export function parseQueryArg(rawArg: string, now: Date): QueryTarget {
  const today = taipeiParts(now);
  let text = normalize(rawArg);
  let filter: QueryTarget["filter"] = "all";

  for (const [re, f] of QUERY_FILTER_SUFFIX) {
    if (re.test(text)) { filter = f; text = text.replace(re, "").trim(); break; }
  }
  if (filter === "all") {
    for (const [re, f] of QUERY_FILTER_PREFIX) {
      if (re.test(text)) { filter = f; text = text.replace(re, "").trim(); break; }
    }
  }
  text = text.replace(DAY_QUERY_TAIL, "").trim();

  if (!text) return { from: isoDate(today), to: isoDate(today), filter, keyword: "" };

  const span = parseSpan(text, today);
  if (!span) return { from: null, to: null, filter, keyword: text };

  return {
    from: isoDate(span.from),
    to: isoDate(span.to),
    filter,
    keyword: cut(text, span.start, span.end),
  };
}

/**
 * 從一段文字推論「這件事是關於哪一天」，供記事使用。
 *
 * 直接重用行程解析的日期邏輯（明天／下週一／8-28 都通），
 * 推論不出來時回 null —— 此時日檢視改以建立當天歸戶。
 */
export function inferDate(text: string, now: Date): string | null {
  const hit = findDate(normalize(text), 0, taipeiParts(now));
  return hit ? isoDate(hit.ymd) : null;
}

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

  if (QUERY_BARE.test(text)) return { kind: "day_query", value: parseQueryArg("", now) };

  const list = QUERY_PREFIX.exec(text);
  if (list) return { kind: "day_query", value: parseQueryArg(list[1].trim(), now) };

  if (NOTE_PREFIX.test(text)) {
    const content = text.replace(NOTE_PREFIX, "").trim();
    if (!content) return { kind: "incomplete", reason: "記事內容是空的。請試：記事 下週要交防務報告" };
    const tags = [...content.matchAll(/#([^\s#]+)/g)].map((m) => m[1]);
    return { kind: "note", value: { content, tags, targetDate: inferDate(content, now) } };
  }

  // 3. 日期選單
  if (MENU_RE.test(text)) return { kind: "menu" };

  // 4. 沒有指令詞的查詢（必須只有日期，否則交給行程解析）
  const day = tryBareQuery(text, taipeiParts(now));
  if (day) return { kind: "day_query", value: day };

  // 5. 行程
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

/**
 * 從標題判斷是不是請假。
 *
 * 供日檢視分區用。小助手建立的事件會在 extendedProperties 留下 req_type，
 * 但手機日曆 App 直接新增的、以及這個欄位上線前建立的事件都沒有，
 * 只能退回看標題 —— 跟解析使用者輸入時用的是同一組關鍵字。
 */
export function isLeaveTitle(summary: string): boolean {
  return LEAVE_KEYWORD.test(summary);
}

/** 全天事件寫入 Google 時，end.date 為排除式（迄日 +1 天）。 */
export function exclusiveEndDate(inclusiveEndDate: string): string {
  const [y, m, d] = inclusiveEndDate.split("-").map(Number);
  return isoDate(addDays({ y, m, d }, 1));
}
