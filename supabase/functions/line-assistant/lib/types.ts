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
  postback?: { data: string };
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
  | { kind: "note"; value: { content: string; tags: string[] } }
  | { kind: "command"; value: { name: string; arg: string } }
  | { kind: "incomplete"; reason: string }
  | { kind: "unknown" };

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
  created_at: string;
  deleted_at: string | null;
}
