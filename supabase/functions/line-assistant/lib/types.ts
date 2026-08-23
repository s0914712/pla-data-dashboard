/** LINE webhook 與內部流程共用的型別。 */

export type SourceType = "group" | "user";
export type ReqType = "meeting" | "leave";
export type RequestStatus =
  | "pending" | "processing" | "confirmed" | "canceled" | "failed" | "expired";

// --- LINE webhook 結構（只宣告我們實際會用到的欄位）---------------------------

export interface LineMention {
  index: number;
  length: number;
  userId?: string;
  type?: string;
  /** 被提及者是不是這個 bot 自己。計畫書 §5.1 要求以此判定，不做名稱字串比對。 */
  isSelf?: boolean;
}

export interface LineTextMessage {
  id: string;
  type: "text";
  text: string;
  mention?: { mentionees: LineMention[] };
}

export interface LineSource {
  type: SourceType | "room";
  userId?: string;
  groupId?: string;
  roomId?: string;
}

export interface LineEvent {
  type: string;
  webhookEventId: string;
  timestamp: number;
  mode?: string;
  replyToken?: string;
  source: LineSource;
  message?: LineTextMessage;
  postback?: { data: string; params?: { date?: string; time?: string; datetime?: string } };
  deliveryContext?: { isRedelivery: boolean };
}

export interface LineWebhookBody {
  destination?: string;
  events: LineEvent[];
}

// --- 解析結果 ---------------------------------------------------------------

/** 解析器回傳的結果之一。 */
export type ParseResult =
  | { kind: "schedule"; value: ParsedSchedule }
  | { kind: "note"; value: ParsedNote }
  | { kind: "command"; value: { name: string; arg: string } }
  /**
   * 查詢某一天有什麼。date 為 YYYY-MM-DD。
   * filter 決定只看行程、只看請假，還是全部（含記事）。
   */
  | { kind: "day_query"; value: { date: string; filter: "all" | "meeting" | "leave" } }
  /** 叫出日期選單。 */
  | { kind: "menu" }
  | { kind: "incomplete"; reason: string }
  | { kind: "unknown" };

export interface ParsedNote {
  content: string;
  tags: string[];
  /** 從內容推論出的「這件事是關於哪一天」，推論不出來為 null。 */
  targetDate: string | null;
}

export interface ParsedSchedule {
  reqType: ReqType;
  title: string;
  location?: string;
  note?: string;
  isAllDay: boolean;
  /** 時段事件：Asia/Taipei 本地時間的 ISO 字串（含 +08:00 位移）。 */
  startAt?: string;
  endAt?: string;
  /** 全天事件：YYYY-MM-DD。endDate 為「含首尾」的迄日，寫 Google 時才 +1 天。 */
  startDate?: string;
  endDate?: string;
}

// --- DB 資料列 --------------------------------------------------------------

export interface CalendarRequestRow {
  id: string;
  webhook_event_id: string;
  source_type: SourceType;
  group_id: string | null;
  user_id: string;
  original_text: string | null;
  req_type: ReqType;
  title: string;
  location: string | null;
  note: string | null;
  is_all_day: boolean;
  start_at: string | null;
  end_at: string | null;
  start_date: string | null;
  end_date: string | null;
  status: RequestStatus;
  google_event_id: string | null;
  error_message: string | null;
  expires_at: string;
  created_at: string;
}

export interface NoteRow {
  id: string;
  seq: number;
  source_type: SourceType;
  group_id: string | null;
  user_id: string;
  scope_key: string;
  content: string;
  tags: string[];
  target_date: string | null;
  created_at: string;
  deleted_at: string | null;
}

/** 草稿目前在跑哪一種流程。 */
export type DraftMode =
  | "create" | "copy" | "edit_time" | "edit_title" | "edit_location" | "delete";

/** 引導式建立／修訂的暫存草稿（見 migration schedule_drafts）。 */
export interface DraftRow {
  scope_key: string;
  user_id: string;
  group_id: string | null;
  source_type: SourceType;
  target_date: string | null;
  /** Postgres time 欄位讀回來是 "HH:MM:SS"。 */
  start_time: string | null;
  end_time: string | null;
  mode: DraftMode;
  /** 修訂／複製／刪除時指向的 Google event id。 */
  event_id: string | null;
  /** copy 時是原標題；edit_title 時是使用者剛打的新標題。 */
  payload_title: string | null;
  payload_location: string | null;
  updated_at: string;
}
