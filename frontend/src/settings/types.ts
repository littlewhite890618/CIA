// 使用者設定頁的資料形狀。欄位名稱對應後端 app/personas/schema.py 的 PersonaCard
//（使用者版本不含 identity.name、identity.ig_display_name，見 app/personas/user_versions.py）。

/** 畫面上的值一律是字串或字串清單；數字與布林值在跟後端交換時才轉換（見 api.ts）。 */
export type CardValue = string | string[];

/**
 * 人設卡內容：段落 → 欄位 → 值。
 * 與後端的差異只在 runtime：後端的 reply_delay_seconds.{min,max} 在畫面上是
 * runtime.delay_min、runtime.delay_max 兩個欄位。
 */
export type Card = Record<string, Record<string, CardValue>>;

export type SignalType = "landmine" | "killer" | "highlight";
export type Severity = "high" | "medium" | "low";

export interface Rule {
  id: string;
  type: SignalType;
  dimension: string;
  text: string;
  severity?: Severity;
  custom?: boolean;
}

export interface ReportSection {
  key: string;
  title: string;
  description: string;
  custom?: boolean;
}

export interface Coaching {
  disabledRuleIds: string[];
  customRules: Rule[];
  reportTone: string;
  reportLength: string;
  /** 啟用的段落，依序排列 */
  reportSections: ReportSection[];
}

export interface SettingsData {
  personaName: string;
  igDisplayName: string;
  /** 連結到期時間（ISO 字串） */
  expiresAt: string;
  /** 回到私訊的連結 */
  chatUrl: string;
  /** 平台預設版本，判斷「改過」與還原用 */
  defaults: { card: Card; coaching: Coaching };
  /** 使用者目前生效的版本；沒有版本時等於 defaults */
  current: { card: Card; coaching: Coaching };
  /** 「絕不接」中使用者不可移除的系統底線 */
  baselineNeverEngages: string[];
}
