// 刻意不依賴外部測試套件，讓這個檔案在任何 Deno 環境都能直接跑。
function assert(cond: unknown, msg = "assertion failed"): asserts cond {
  if (!cond) throw new Error(msg);
}
function assertEquals<T>(actual: T, expected: T, msg = ""): void {
  const a = JSON.stringify(actual), b = JSON.stringify(expected);
  if (a !== b) throw new Error(`${msg}\n  actual:   ${a}\n  expected: ${b}`);
}
import { exclusiveEndDate, inferDate, normalize, parse, parseQueryArg, taipeiParts } from "./parser_zh_tw.ts";
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

Deno.test("記事：刪除是指令；查詢已併進行程查詢", () => {
  assertEquals(parse("刪記事 12", NOW), { kind: "command", value: { name: "note_delete", arg: "12" } });
  // 查記事＝查行程，兩個指令解出完全一樣的結果
  assertEquals(parse("查記事", NOW), parse("查行程", NOW));
  assertEquals(parse("查記事 本週", NOW), parse("查行程 本週", NOW));
  assertEquals(parse("查記事 8/28", NOW), parse("查行程 8/28", NOW));
  assertEquals(parse("查記事", NOW), {
    kind: "day_query",
    value: { from: "2026-08-23", to: "2026-08-23", filter: "all", keyword: "" },
  });
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

function dayQuery(text: string, date: string, filter: "all" | "meeting" | "leave" = "all") {
  assertEquals(parse(text, NOW), {
    kind: "day_query",
    value: { from: date, to: date, filter, keyword: "" },
  });
}

Deno.test("日檢視：裸日期即查詢", () => {
  dayQuery("明天", "2026-08-24");
  dayQuery("今天", "2026-08-23");
  dayQuery("8/28", "2026-08-28");
});

Deno.test("日檢視：帶前後綴的查詢", () => {
  dayQuery("明天行程", "2026-08-24");
  dayQuery("查 8/28", "2026-08-28");
  dayQuery("明天有什麼", "2026-08-24");
  dayQuery("查詢 下週一 的行程", "2026-08-24");
});

// --- 行程與請假查詢分開 ------------------------------------------------------

Deno.test("只查請假：明確問法不需要日期，預設今天", () => {
  dayQuery("誰請假", "2026-08-23", "leave");
  dayQuery("請假名單", "2026-08-23", "leave");
  dayQuery("查請假", "2026-08-23", "leave");
});

Deno.test("只查請假：類別詞在日期前後都認得", () => {
  dayQuery("明天誰請假", "2026-08-24", "leave");
  dayQuery("查請假 8/28", "2026-08-28", "leave");   // 類別詞在日期前
  dayQuery("查 8/28 請假", "2026-08-28", "leave");  // 類別詞在日期後
});

Deno.test("只查會議", () => {
  dayQuery("明天會議", "2026-08-24", "meeting");
});

Deno.test("「明天請假」仍然是建立，不是查詢", () => {
  // 計畫書 §5.1 把「請假 8/29」當成建立語句。若被當成查詢，
  // 使用者就再也沒辦法用最自然的說法請假了。
  assertEquals(schedule("明天請假").reqType, "leave");
  assertEquals(schedule("明天請假").isAllDay, true);
  assertEquals(schedule("請假 8/29 全天").startDate, "2026-08-29");
  assertEquals(schedule("請假 8/29").startDate, "2026-08-29");
  // 裸的「請假」沒有日期 → 補問，不要偷偷變成查詢
  assert(incomplete("請假").includes("日期"));
});

Deno.test("帶時間的會議不會被會議後綴吃掉", () => {
  assertEquals(schedule("明天 09:00-10:00 會議").title, "會議");
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

// --- 四位數時間寫法 ----------------------------------------------------------

Deno.test("四位數時間：1600-1700 不用冒號", () => {
  const s = schedule("8/28 1600-1700 部務會議");
  assertEquals(s.startAt, "2026-08-28T16:00:00+08:00");
  assertEquals(s.endAt, "2026-08-28T17:00:00+08:00");
  assertEquals(s.title, "部務會議");
});

Deno.test("四位數時間：前導零與非整點", () => {
  const s = schedule("8/28 0830-0945 晨會");
  assertEquals(s.startAt, "2026-08-28T08:30:00+08:00");
  assertEquals(s.endAt, "2026-08-28T09:45:00+08:00");
  assertEquals(s.title, "晨會");
});

Deno.test("四位數時間：到／至／~ 也算區間", () => {
  for (const sep of ["到", "至", "~", " - "]) {
    const s = schedule(`8/28 1600${sep}1700 部務會議`);
    assertEquals(s.startAt, "2026-08-28T16:00:00+08:00", sep);
    assertEquals(s.endAt, "2026-08-28T17:00:00+08:00", sep);
  }
});

Deno.test("四位數時間：可以跟冒號寫法混用", () => {
  const s = schedule("8/28 1600-17:30 部務會議");
  assertEquals(s.endAt, "2026-08-28T17:30:00+08:00");
});

Deno.test("四位數時間：只有起始時間仍然補問", () => {
  assert(incomplete("8/28 1600 部務會議").includes("結束時間"));
});

Deno.test("四位數時間：跨日", () => {
  const s = schedule("8/28 2200-0100 夜間戰備");
  assertEquals(s.startAt, "2026-08-28T22:00:00+08:00");
  assertEquals(s.endAt, "2026-08-29T01:00:00+08:00");
});

Deno.test("四位數時間：緊鄰其他字元的數字不算時間", () => {
  // 「第1200梯次」「A0930」都不該被讀成時間，所以這些訊息缺時間 → 補問
  assert(incomplete("8/28 第1200梯次報到").includes("時間"));
  assert(incomplete("8/28 電話分機0830轉3").includes("時間"));
});

Deno.test("四位數時間：不合法的時分不採用", () => {
  // 2500 與 1265 都不是合法時間，仍視為缺時間
  assert(incomplete("8/28 2500 部務會議").includes("時間"));
  assert(incomplete("8/28 1265 部務會議").includes("時間"));
});

Deno.test("四位數時間：請假也適用", () => {
  const s = schedule("請假 8/29 1330-1730");
  assertEquals(s.reqType, "leave");
  assertEquals(s.isAllDay, false);
  assertEquals(s.startAt, "2026-08-29T13:30:00+08:00");
});

Deno.test("四位數時間：年份不會被當成時間", () => {
  // 2026 年度預算的「2026」後面接的是「年」，不是邊界字元
  const s = schedule("8/28 14:00-16:00 2026年度預算會議");
  assertEquals(s.title, "2026年度預算會議");
});

// --- 查詢參數（查記事＝查行程）--------------------------------------------------------

Deno.test("查詢參數：沒有參數就是今天", () => {
  assertEquals(parseQueryArg("", NOW), {
    from: "2026-08-23", to: "2026-08-23", filter: "all", keyword: "",
  });
});

Deno.test("查詢參數：純關鍵字", () => {
  assertEquals(parseQueryArg("報告", NOW), { from: null, to: null, filter: "all", keyword: "報告" });
});

Deno.test("查詢參數：單一日期", () => {
  assertEquals(parseQueryArg("8/28", NOW), {
    from: "2026-08-28", to: "2026-08-28", filter: "all", keyword: "",
  });
});

Deno.test("查詢參數：日期區間", () => {
  assertEquals(parseQueryArg("8/28-8/31", NOW), {
    from: "2026-08-28", to: "2026-08-31", filter: "all", keyword: "",
  });
  assertEquals(parseQueryArg("8/28~8/31", NOW), {
    from: "2026-08-28", to: "2026-08-31", filter: "all", keyword: "",
  });
  assertEquals(parseQueryArg("8/28 到 8/31", NOW), {
    from: "2026-08-28", to: "2026-08-31", filter: "all", keyword: "",
  });
});

Deno.test("查詢參數：查過去的日期取最近的那一年", () => {
  // 8/23 查 8/20 要的是三天前，不是明年的 8/20（建立行程才會往未來找）
  assertEquals(parseQueryArg("8/20", NOW).from, "2026-08-20");
  // 反過來，接近年底的日期在年初查就要往前一年找
  const jan = new Date("2026-01-05T04:00:00Z");
  assertEquals(parseQueryArg("12/28", jan).from, "2025-12-28");
});

Deno.test("查詢參數：跨年區間", () => {
  const dec = new Date("2026-12-20T04:00:00Z");
  assertEquals(parseQueryArg("12/28-1/3", dec), {
    from: "2026-12-28", to: "2027-01-03", filter: "all", keyword: "",
  });
});

Deno.test("查詢參數：本週（週一到週日）", () => {
  // NOW 是 2026-08-23 週日 → 本週為 8/17（一）～8/23（日）
  assertEquals(parseQueryArg("本週", NOW), {
    from: "2026-08-17", to: "2026-08-23", filter: "all", keyword: "",
  });
  assertEquals(parseQueryArg("上週", NOW).from, "2026-08-10");
  assertEquals(parseQueryArg("下週", NOW).to, "2026-08-30");
});

Deno.test("查詢參數：本月與上個月", () => {
  assertEquals(parseQueryArg("本月", NOW), {
    from: "2026-08-01", to: "2026-08-31", filter: "all", keyword: "",
  });
  assertEquals(parseQueryArg("上個月", NOW), {
    from: "2026-07-01", to: "2026-07-31", filter: "all", keyword: "",
  });
  // 二月與跨年都要對
  const jan = new Date("2026-01-15T04:00:00Z");
  assertEquals(parseQueryArg("上個月", jan), {
    from: "2025-12-01", to: "2025-12-31", filter: "all", keyword: "",
  });
  const mar = new Date("2028-03-10T04:00:00Z");
  assertEquals(parseQueryArg("上個月", mar).to, "2028-02-29");
});

Deno.test("查詢參數：最近 N 天", () => {
  assertEquals(parseQueryArg("最近7天", NOW), {
    from: "2026-08-17", to: "2026-08-23", filter: "all", keyword: "",
  });
  assertEquals(parseQueryArg("近3天", NOW).from, "2026-08-21");
  assertEquals(parseQueryArg("最近一週", NOW).from, "2026-08-17");
  assertEquals(parseQueryArg("最近一個月", NOW).from, "2026-07-25");
});

Deno.test("查詢參數：區間加關鍵字", () => {
  assertEquals(parseQueryArg("本週 報告", NOW), {
    from: "2026-08-17", to: "2026-08-23", filter: "all", keyword: "報告",
  });
  assertEquals(parseQueryArg("8/28-8/31 報告", NOW), {
    from: "2026-08-28", to: "2026-08-31", filter: "all", keyword: "報告",
  });
});

Deno.test("查詢參數：下週一是單日，不是整個下週", () => {
  // RE_PERIOD_WEEK 的 lookahead 要擋住「下週一」
  assertEquals(parseQueryArg("下週一", NOW), {
    from: "2026-08-24", to: "2026-08-24", filter: "all", keyword: "",
  });
});

Deno.test("查詢參數：相對日期", () => {
  assertEquals(parseQueryArg("明天", NOW).from, "2026-08-24");
  assertEquals(parseQueryArg("今天", NOW).to, "2026-08-23");
});

Deno.test("查詢參數：走得通整條 parse 路徑", () => {
  assertEquals(parse("查行程 本週", NOW), {
    kind: "day_query",
    value: { from: "2026-08-17", to: "2026-08-23", filter: "all", keyword: "" },
  });
  assertEquals(parse("查記事 8/28-8/31", NOW), {
    kind: "day_query",
    value: { from: "2026-08-28", to: "2026-08-31", filter: "all", keyword: "" },
  });
  // 裸的「行程」「記事」二字都是查詢，不是空記事
  assertEquals(parse("行程", NOW).kind, "day_query");
  assertEquals(parse("記事", NOW).kind, "day_query");
});

// --- 合併查詢：類別詞與關鍵字 ------------------------------------------------

Deno.test("查詢參數：類別詞在日期前後都認得", () => {
  assertEquals(parseQueryArg("本週請假", NOW), {
    from: "2026-08-17", to: "2026-08-23", filter: "leave", keyword: "",
  });
  assertEquals(parseQueryArg("請假 本週", NOW).filter, "leave");
  assertEquals(parseQueryArg("8/28 請假", NOW), {
    from: "2026-08-28", to: "2026-08-28", filter: "leave", keyword: "",
  });
  assertEquals(parseQueryArg("明天會議", NOW), {
    from: "2026-08-24", to: "2026-08-24", filter: "meeting", keyword: "",
  });
});

Deno.test("查詢參數：只有類別詞就是今天", () => {
  assertEquals(parseQueryArg("請假", NOW), {
    from: "2026-08-23", to: "2026-08-23", filter: "leave", keyword: "",
  });
  assertEquals(parseQueryArg("誰請假", NOW).filter, "leave");
});

Deno.test("查詢參數：關鍵字裡的「請假」不算類別詞", () => {
  // 「請假流程」是要找的字，不是「只看請假」
  assertEquals(parseQueryArg("請假流程", NOW), {
    from: null, to: null, filter: "all", keyword: "請假流程",
  });
});

Deno.test("查詢參數：只有關鍵字時不給日期，範圍交給呼叫端", () => {
  assertEquals(parseQueryArg("報告", NOW), {
    from: null, to: null, filter: "all", keyword: "報告",
  });
});

Deno.test("查詢：查記事與查行程完全同義", () => {
  for (const arg of ["", "本週", "8/28", "8/28-8/31", "報告", "本週請假", "最近7天"]) {
    assertEquals(
      parse(`查記事 ${arg}`.trim(), NOW),
      parse(`查行程 ${arg}`.trim(), NOW),
      `「${arg}」兩個指令應該一樣`,
    );
  }
});

Deno.test("合併後仍然分得出建立與查詢", () => {
  // 這幾條是界線：被查詢吃掉的話，使用者就沒辦法用最自然的說法建立了
  assertEquals(parse("明天請假", NOW).kind, "schedule");
  assertEquals(parse("請假 8/29-8/31", NOW).kind, "schedule");
  assertEquals(schedule("明天 09:00-10:30 週報會議").title, "週報會議");
  assertEquals(schedule("本週三 1400-1600 部務會議").title, "部務會議");
  assertEquals(parse("記事 明天要帶識別證", NOW).kind, "note");
});

Deno.test("裸的日期區間也是查詢", () => {
  assertEquals(parse("8/28-8/31", NOW), {
    kind: "day_query",
    value: { from: "2026-08-28", to: "2026-08-31", filter: "all", keyword: "" },
  });
  assertEquals(parse("本週", NOW), {
    kind: "day_query",
    value: { from: "2026-08-17", to: "2026-08-23", filter: "all", keyword: "" },
  });
});
