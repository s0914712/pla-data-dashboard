// 刻意不依賴外部測試套件，讓這個檔案在任何 Deno 環境都能直接跑。
function assert(cond: unknown, msg = "assertion failed"): asserts cond {
  if (!cond) throw new Error(msg);
}
function assertEquals<T>(actual: T, expected: T, msg = ""): void {
  const a = JSON.stringify(actual), b = JSON.stringify(expected);
  if (a !== b) throw new Error(`${msg}\n  actual:   ${a}\n  expected: ${b}`);
}
import { exclusiveEndDate, inferDate, normalize, parse, taipeiParts } from "./parser_zh_tw.ts";
import type { ParsedSchedule } from "./types.ts";

// 固定「現在」= 2026-08-23 12:00 台北時間（週日）
const NOW = new Date("2026-08-23T04:00:00Z");

function schedule(text: string, now = NOW): ParsedSchedule {
  const r = parse(text, now);
  assertEquals(r.kind, "schedule", `expected schedule, got ${r.kind}: ${JSON.stringify(r)}`);
  return (r as { kind: "schedule"; value: ParsedSchedule }).value;
}

function incomplete(text: string, now = NOW): string {
  const r = parse(text, now);
  assertEquals(r.kind, "incomplete", `expected incomplete, got ${r.kind}: ${JSON.stringify(r)}`);
  return (r as { kind: "incomplete"; reason: string }).reason;
}

Deno.test("台北時間換算", () => {
  const p = taipeiParts(NOW);
  assertEquals([p.y, p.m, p.d, p.hh], [2026, 8, 23, 12]);
});

Deno.test("normalize：全形數字與標點", () => {
  assertEquals(normalize("８/２８　１４：００－１６：００"), "8/28 14:00-16:00");
});

Deno.test("會議：完整輸入", () => {
  const s = schedule("8/28 14:00-16:00 海軍總部開會 地點：第一會議室");
  assertEquals(s.reqType, "meeting");
  assertEquals(s.title, "海軍總部開會");
  assertEquals(s.location, "第一會議室");
  assertEquals(s.isAllDay, false);
  assertEquals(s.startAt, "2026-08-28T14:00:00+08:00");
  assertEquals(s.endAt, "2026-08-28T16:00:00+08:00");
});

Deno.test("會議：省略「新增」動詞", () => {
  const s = schedule("新增 8/28 14:00-16:00 部務會議");
  assertEquals(s.title, "部務會議");
  assertEquals(s.startAt, "2026-08-28T14:00:00+08:00");
});

Deno.test("會議：逗號尾綴視為地點", () => {
  const s = schedule("8/28 14:00-16:00 海軍總部開會，第一會議室");
  assertEquals(s.title, "海軍總部開會");
  assertEquals(s.location, "第一會議室");
});

Deno.test("會議：中文時間與上下午", () => {
  const s = schedule("8/28 下午2點-4點半 專案檢討");
  assertEquals(s.startAt, "2026-08-28T14:00:00+08:00");
  assertEquals(s.endAt, "2026-08-28T16:30:00+08:00");
});

Deno.test("會議：相對日期「明天」", () => {
  const s = schedule("明天 09:00-10:30 週報會議");
  assertEquals(s.title, "週報會議");
  assertEquals(s.startAt, "2026-08-24T09:00:00+08:00");
});

Deno.test("會議：「下週一」以台北週次計算", () => {
  // 2026-08-23 是週日，本週一是 08-17，下週一 = 08-24
  const s = schedule("下週一 09:00-10:00 幕僚會報");
  assertEquals(s.startAt, "2026-08-24T09:00:00+08:00");
});

Deno.test("無年份日期：已過的月日推到明年", () => {
  const s = schedule("1/5 09:00-10:00 年度校閱");
  assertEquals(s.startAt, "2027-01-05T09:00:00+08:00");
});

Deno.test("無年份日期：今天的月日不推到明年", () => {
  const s = schedule("8/23 23:00-23:30 值班交接");
  assertEquals(s.startAt, "2026-08-23T23:00:00+08:00");
});

Deno.test("明確年份且已過期 → 拒絕並補問", () => {
  const reason = incomplete("2020/1/5 09:00-10:00 舊會議");
  assert(reason.includes("已經過去"), reason);
});

Deno.test("跨日時段：結束早於開始視為隔天", () => {
  const s = schedule("8/28 22:00-01:00 夜間戰備");
  assertEquals(s.startAt, "2026-08-28T22:00:00+08:00");
  assertEquals(s.endAt, "2026-08-29T01:00:00+08:00");
});

Deno.test("請假：只有日期 → 全天", () => {
  const s = schedule("請假 8/29 全天");
  assertEquals(s.reqType, "leave");
  assertEquals(s.isAllDay, true);
  assertEquals(s.startDate, "2026-08-29");
  assertEquals(s.endDate, "2026-08-29");
  assertEquals(s.title, "請假");
});

Deno.test("請假：沒寫「全天」也預設全天", () => {
  const s = schedule("請假 8/29");
  assertEquals(s.isAllDay, true);
  assertEquals(s.startDate, "2026-08-29");
});

Deno.test("請假：時段", () => {
  const s = schedule("請假 8/29 13:30-17:30");
  assertEquals(s.isAllDay, false);
  assertEquals(s.startAt, "2026-08-29T13:30:00+08:00");
  assertEquals(s.endAt, "2026-08-29T17:30:00+08:00");
});

Deno.test("請假：跨日區間，end_date 含首尾", () => {
  const s = schedule("請假 8/29-8/31");
  assertEquals(s.isAllDay, true);
  assertEquals(s.startDate, "2026-08-29");
  assertEquals(s.endDate, "2026-08-31");
  // 寫進 Google 時才轉排除式
  assertEquals(exclusiveEndDate(s.endDate!), "2026-09-01");
});

Deno.test("請假：「至」也是區間分隔", () => {
  const s = schedule("請假 8/29 至 8/31");
  assertEquals(s.startDate, "2026-08-29");
  assertEquals(s.endDate, "2026-08-31");
});

Deno.test("請假：跨年區間", () => {
  const s = schedule("請假 12/30-1/2");
  assertEquals(s.startDate, "2026-12-30");
  assertEquals(s.endDate, "2027-01-02");
});

Deno.test("會議缺結束時間 → 補問，不預設時長", () => {
  const reason = incomplete("8/28 14:00 部務會議");
  assert(reason.includes("結束時間"), reason);
});

Deno.test("會議缺時間 → 補問", () => {
  const reason = incomplete("8/28 部務會議");
  assert(reason.includes("缺少時間"), reason);
});

Deno.test("缺日期 → 補問", () => {
  const reason = incomplete("開個會");
  assert(reason.includes("日期"), reason);
});

Deno.test("缺標題 → 補問", () => {
  const reason = incomplete("8/28 14:00-16:00");
  assert(reason.includes("標題"), reason);
});

Deno.test("一則訊息多筆事件 → 拒絕", () => {
  const reason = incomplete("8/28 14:00-16:00 甲會議 8/29 10:00-11:00 乙會議");
  assert(reason.includes("多個日期"), reason);
});

Deno.test("備註只在明確輸入時才帶入", () => {
  const s = schedule("請假 8/29 全天 備註：家庭因素");
  assertEquals(s.note, "家庭因素");
  const bare = schedule("請假 8/30 全天");
  assertEquals(bare.note, undefined);
});

Deno.test("記事：前綴與標籤", () => {
  const r = parse("記事 下週要交防務報告 #報告", NOW);
  assertEquals(r.kind, "note");
  const v = (r as { kind: "note"; value: { content: string; tags: string[] } }).value;
  assertEquals(v.content, "下週要交防務報告 #報告");
  assertEquals(v.tags, ["報告"]);
});

Deno.test("記事：筆記也是前綴", () => {
  assertEquals(parse("筆記：明天記得帶識別證", NOW).kind, "note");
});

Deno.test("記事：查詢與刪除是指令", () => {
  assertEquals(parse("查記事", NOW), { kind: "command", value: { name: "note_list", arg: "" } });
  assertEquals(parse("查記事 報告", NOW), { kind: "command", value: { name: "note_list", arg: "報告" } });
  assertEquals(parse("刪記事 12", NOW), { kind: "command", value: { name: "note_delete", arg: "12" } });
});

Deno.test("斜線指令", () => {
  assertEquals(parse("/bind", NOW), { kind: "command", value: { name: "bind", arg: "" } });
  assertEquals(parse("/diag", NOW), { kind: "command", value: { name: "diag", arg: "" } });
});

Deno.test("空白訊息 → unknown", () => {
  assertEquals(parse("   ", NOW).kind, "unknown");
});

// --- 日期誤判防護 ------------------------------------------------------------

Deno.test("電話號碼不會被當成日期", () => {
  // 02/2345 若被當成 2 月 23 日，就會憑空生出一筆行程
  const r = parse("記事 值班電話 02/2345 6789", NOW);
  assertEquals(r.kind, "note");
  assertEquals((r as { value: { targetDate: string | null } }).value.targetDate, null);
});

Deno.test("緊鄰數字的日期樣式不採用", () => {
  assertEquals(inferDate("分機 1234/5678", NOW), null);
});

// --- 記事日期推論 ------------------------------------------------------------

Deno.test("記事：從內容推論日期", () => {
  assertEquals(inferDate("明天要帶識別證", NOW), "2026-08-24");
  assertEquals(inferDate("8/28 要交防務報告", NOW), "2026-08-28");
  assertEquals(inferDate("下週一 交報告", NOW), "2026-08-24");
});

Deno.test("記事：推論不出日期時為 null", () => {
  assertEquals(inferDate("下週要交防務報告", NOW), null);
  assertEquals(inferDate("記得補送簽呈", NOW), null);
});

Deno.test("記事的 targetDate 會帶進 parse 結果", () => {
  const r = parse("記事 明天要帶識別證", NOW);
  assertEquals(r.kind, "note");
  const v = (r as { value: { content: string; targetDate: string | null } }).value;
  assertEquals(v.content, "明天要帶識別證");
  assertEquals(v.targetDate, "2026-08-24");
});

// --- 日檢視查詢 --------------------------------------------------------------

Deno.test("日檢視：裸日期即查詢", () => {
  assertEquals(parse("明天", NOW), { kind: "day_query", value: { date: "2026-08-24" } });
  assertEquals(parse("今天", NOW), { kind: "day_query", value: { date: "2026-08-23" } });
  assertEquals(parse("8/28", NOW), { kind: "day_query", value: { date: "2026-08-28" } });
});

Deno.test("日檢視：帶前後綴的查詢", () => {
  assertEquals(parse("明天行程", NOW), { kind: "day_query", value: { date: "2026-08-24" } });
  assertEquals(parse("查 8/28", NOW), { kind: "day_query", value: { date: "2026-08-28" } });
  assertEquals(parse("明天有什麼", NOW), { kind: "day_query", value: { date: "2026-08-24" } });
  assertEquals(parse("查詢 下週一 的行程", NOW), { kind: "day_query", value: { date: "2026-08-24" } });
});

Deno.test("日檢視：有日期以外的內容就不是查詢，交給行程解析", () => {
  // 這是最重要的一條：建立行程不能被誤判成查詢
  assertEquals(schedule("明天 09:00-10:30 週報會議").title, "週報會議");
  assertEquals(schedule("請假 8/29 全天").reqType, "leave");
  assertEquals(parse("8/28 部務會議", NOW).kind, "incomplete");
});

Deno.test("選單關鍵字", () => {
  assertEquals(parse("選單", NOW).kind, "menu");
  assertEquals(parse("查詢", NOW).kind, "menu");
  assertEquals(parse("menu", NOW).kind, "menu");
  assertEquals(parse("/menu", NOW), { kind: "command", value: { name: "menu", arg: "" } });
});
