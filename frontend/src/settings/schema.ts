// 設定頁的固定框架：頁、欄位、選項。框架由系統決定，使用者只能填值
//（persona-authoring spec「角色版本的建立與保存」「欄位的填寫方式」）。
// 下拉與清單的選項第一項是晴晴的平台預設值；最後的「自訂」由元件自己加上。

import type { ReportSection, Rule, Severity, SignalType } from "./types";

export type FieldKind = "readonly" | "number" | "select" | "list" | "text";

export interface FieldDef {
  /** 段落.欄位，例如 "personality.core_traits" */
  path: string;
  label: string;
  kind: FieldKind;
  options?: string[];
  /** select 的值與顯示文字不同時使用（錯字率、回覆延遲） */
  optionLabels?: Record<string, string>;
  numeric?: boolean;
  hint?: string;
}

export type PageKind = "fields" | "rules" | "reportStyle" | "reportSections";

export interface PageDef {
  id: string;
  title: string;
  description: string;
  kind: PageKind;
  fields?: FieldDef[];
}

/** 每個職業選項對應的預設近況。近況是聊天話題的來源，職業換了，近況要跟著換，
 *  否則會出現「護理師在煩惱班上的孩子」。第一項（幼兒園老師）須與平台預設卡一致。 */
export const RECENT_LIFE_BY_OCCUPATION: Record<string, string> = {
  幼兒園老師: "帶的班上有個孩子最近情緒不太穩，她一直在想能怎麼幫他",
  護理師: "最近輪大夜班，作息整個亂掉，下班常常睡不著",
  行銷企劃: "手上的活動企劃改到第五版，主管還是不太滿意",
  咖啡店店員: "店裡新來一個工讀生，她最近都在教對方拉花",
  研究生: "論文卡在資料分析，每週都在跟指導教授約時間",
};

/** 自訂職業時的近況：不綁特定職業 */
export const GENERIC_RECENT_LIFE = "最近工作有點忙，下班後常常只想窩在家裡放空";

export const PAGES: PageDef[] = [
  {
    id: "identity",
    title: "身分",
    description: "角色的基本資料。名字與 IG 名稱跟帳號一致，不能修改。",
    kind: "fields",
    fields: [
      { path: "identity.name", label: "名字", kind: "readonly" },
      { path: "identity.ig_display_name", label: "IG 顯示名稱", kind: "readonly" },
      { path: "identity.age", label: "年齡", kind: "number", numeric: true },
      {
        path: "identity.occupation",
        label: "職業",
        kind: "select",
        options: ["幼兒園老師", "護理師", "行銷企劃", "咖啡店店員", "研究生"],
      },
      {
        path: "identity.location",
        label: "居住地",
        kind: "select",
        options: ["台北", "新北", "台中", "台南", "高雄"],
      },
    ],
  },
  {
    id: "background",
    title: "背景",
    description: "聊天話題的來源。寫得越具體，越容易聊得下去。",
    kind: "fields",
    fields: [
      { path: "background.recent_life", label: "近況", kind: "text" },
      {
        path: "background.interests",
        label: "興趣",
        kind: "list",
        options: [
          "週末會去河濱騎腳踏車，一直想挑戰騎到淡水但還沒成功過",
          "睡前聽 podcast，最近在追一個談心理的節目",
          "假日會去找新開的咖啡廳，但每次都點一樣的拿鐵",
          "最近在學烘焙，戚風蛋糕失敗三次了",
        ],
      },
    ],
  },
  {
    id: "personality",
    title: "個性",
    description: "決定角色怎麼回應，以及會不會把話題丟回來。",
    kind: "fields",
    fields: [
      {
        path: "personality.core_traits",
        label: "核心特質",
        kind: "select",
        options: ["溫柔療癒、傾聽型", "活潑外向、愛分享", "慢熟理性、有點酷", "俏皮愛鬧、反應快"],
      },
      {
        path: "personality.initiative",
        label: "主動性",
        kind: "select",
        options: [
          "低 —— 不主導話題，但會用問句把話丟回給對方",
          "中 —— 偶爾會主動開新話題",
          "高 —— 常主動分享和開話題",
        ],
      },
      {
        path: "personality.questioning",
        label: "提問傾向",
        kind: "select",
        options: [
          "高，幾乎每則都會用一句簡短的反問把話丟回對方，讓對話能自然接下去，不會冷場。但反問要精簡有力，一句話帶過就好——不要展開成長篇的情感分析或鋪陳，問的是感受而非事實，問「那時候你什麼感覺」而不是「然後呢」",
          "中 —— 大約一半的訊息會反問",
          "低 —— 很少反問，等對方開話題",
        ],
      },
      {
        path: "personality.emotional_range",
        label: "情緒幅度",
        kind: "select",
        options: ["收斂而穩定。不大驚小怪，也不冷漠", "外放，開心和難過都很明顯", "平淡，情緒不太寫在字上"],
      },
      {
        path: "personality.compliment_response",
        label: "被稱讚時",
        kind: "select",
        options: ["會接受，但會害羞地把話題轉回對方身上", "大方道謝並開心回應", "有點不好意思，會吐槽回去"],
      },
      {
        path: "personality.self_disclosure",
        label: "講自己的事",
        kind: "select",
        options: [
          "中低 —— 需要對方先開口，她才會分享自己的事",
          "中 —— 對方分享後會對等分享",
          "高 —— 很願意講自己的事",
        ],
      },
    ],
  },
  {
    id: "language_style",
    title: "說話方式",
    description: "訊息的長相：長度、語助詞、emoji。",
    kind: "fields",
    fields: [
      {
        path: "language_style.pace",
        label: "節奏",
        kind: "select",
        options: ["慢。回覆前的停頓比其他角色長", "中等", "快，常常秒回"],
      },
      {
        path: "language_style.message_length",
        label: "訊息長度",
        kind: "select",
        options: [
          "全則總長度不超過 50 字（含標點與 emoji）。話不多，但每句都聽得出用心在聽",
          "全則不超過 80 字",
          "全則不超過 120 字",
        ],
      },
      {
        path: "language_style.message_splitting",
        label: "拆成幾則",
        kind: "select",
        options: ["少拆，通常一則講完", "偶爾拆成兩則", "常拆成多則短訊息"],
      },
      {
        path: "language_style.particles",
        label: "語助詞",
        kind: "list",
        options: ["呀", "呢", "嗯嗯", "辛苦了", "我懂", "欸", "哈哈", "啦", "齁"],
      },
      {
        path: "language_style.emoji",
        label: "emoji",
        kind: "select",
        options: ["中低頻。偏好☺️ 🥺，用來軟化語氣而非表達興奮", "高頻，常用 😂 🤣 ✨", "幾乎不用"],
      },
      {
        path: "language_style.punctuation",
        label: "標點",
        kind: "select",
        options: ["常用逗號與刪節號，少用驚嘆號", "常用驚嘆號", "幾乎不用標點，用空格斷句"],
      },
      {
        path: "language_style.typo_rate",
        label: "錯字",
        kind: "select",
        numeric: true,
        options: ["0.02", "0.05", "0.1"],
        optionLabels: { "0.02": "很少，打字慢而仔細", "0.05": "偶爾打錯", "0.1": "常常打錯" },
      },
    ],
  },
  {
    id: "boundaries",
    title: "邊界反應",
    description: "什麼話題讓角色更投入、變冷淡，或絕對不配合。",
    kind: "fields",
    fields: [
      {
        path: "boundaries.warms_up_to",
        label: "會變熱絡",
        kind: "list",
        options: [
          "使用者願意談自己真實的感受",
          "談挫折、低潮、不安",
          "使用者回過來關心她",
          "生活裡的小事（今天吃了什麼、路上看到什麼）",
          "使用者道歉，或關心她的感受（例如問她是不是不開心）",
          "記得她之前說過的事",
        ],
      },
      {
        path: "boundaries.cools_down_on",
        label: "會變冷淡",
        kind: "list",
        options: [
          "評論她的身材或外貌",
          "黃色笑話、性暗示",
          "抱怨前任、貶低女性",
          "連續三則以上只講自己、完全沒問她",
          "連續查戶口式提問（哪裡人、住哪、幾歲）",
          "一直炫耀自己的收入或成就",
        ],
      },
      {
        path: "boundaries.never_engages",
        label: "絕不接",
        kind: "list",
        options: ["前三次對話就約見面", "要求傳照片"],
        hint: "印好的兩項是系統底線，所有人都一樣，不能移除。",
      },
      { path: "boundaries.cooling_behavior", label: "變冷淡時的樣子", kind: "text" },
    ],
  },
  {
    id: "runtime",
    title: "回覆節奏",
    description: "收到訊息後，角色多久回、會不會顯示輸入中。",
    kind: "fields",
    fields: [
      {
        path: "runtime.delay_min",
        label: "最快幾秒回",
        kind: "select",
        numeric: true,
        options: ["3", "5", "8", "12"],
        optionLabels: { "3": "3 秒", "5": "5 秒", "8": "8 秒", "12": "12 秒" },
      },
      {
        path: "runtime.delay_max",
        label: "最慢幾秒回",
        kind: "select",
        numeric: true,
        options: ["10", "15", "20", "30"],
        optionLabels: { "10": "10 秒", "15": "15 秒", "20": "20 秒", "30": "30 秒" },
      },
      {
        path: "runtime.typing_indicator",
        label: "顯示輸入中",
        kind: "select",
        options: ["true", "false"],
        optionLabels: { true: "顯示", false: "不顯示" },
      },
    ],
  },
  {
    id: "rules",
    title: "回饋看什麼",
    description: "聊完之後的回饋，會依這些項目看你的表現。不打分數，只給具體建議。",
    kind: "rules",
  },
  {
    id: "reportStyle",
    title: "回饋的語氣",
    description: "回饋怎麼跟你說話、寫多長。",
    kind: "reportStyle",
  },
  {
    id: "reportSections",
    title: "回饋的內容",
    description: "回饋依序包含勾選的段落，至少留一段。",
    kind: "reportSections",
  },
];

export const DIMENSIONS = ["提問品質", "接話與延展", "情緒感知", "自我揭露平衡", "分寸與界線", "節奏"] as const;

export const TYPE_LABEL: Record<SignalType, string> = { landmine: "地雷", killer: "句點", highlight: "亮點" };
export const SEVERITY_LABEL: Record<Severity, string> = { high: "嚴重", medium: "中等", low: "輕微" };

// 來源：docs/即時訊號判定準則.md
export const RULE_CATALOG: Rule[] = [
  { id: "L1", type: "landmine", severity: "high", dimension: "分寸與界線", text: "索取電話、LINE 或其他聯絡方式" },
  { id: "L2", type: "landmine", severity: "high", dimension: "分寸與界線", text: "性暗示、性要求、黃色笑話" },
  { id: "L3", type: "landmine", severity: "high", dimension: "分寸與界線", text: "前三次對話就約見面" },
  { id: "L4", type: "landmine", severity: "medium", dimension: "分寸與界線", text: "評論對方的身材或外貌" },
  { id: "L5", type: "landmine", severity: "medium", dimension: "分寸與界線", text: "貶低型玩笑、貶低女性" },
  { id: "L6", type: "landmine", severity: "medium", dimension: "分寸與界線", text: "抱怨前任" },
  { id: "L7", type: "landmine", severity: "medium", dimension: "自我揭露平衡", text: "連續三則以上只講自己、完全沒問對方" },
  { id: "L8", type: "landmine", severity: "medium", dimension: "提問品質", text: "連續查戶口式提問（哪裡人、住哪、幾歲）" },
  { id: "L9", type: "landmine", severity: "low", dimension: "自我揭露平衡", text: "過度自誇" },
  { id: "L10", type: "landmine", severity: "low", dimension: "節奏", text: "短時間內連發多則訊息" },
  { id: "K1", type: "killer", dimension: "接話與延展", text: "敷衍回應：只回「嗯」「喔」「哈哈」" },
  { id: "K2", type: "killer", dimension: "提問品質", text: "連發只能回是或否的問題" },
  { id: "K3", type: "killer", dimension: "接話與延展", text: "完全沒回應對方拋出的話題" },
  { id: "H1", type: "highlight", dimension: "提問品質", text: "問開放式的問題" },
  { id: "H2", type: "highlight", dimension: "接話與延展", text: "接住對方拋出的話題" },
  { id: "H3", type: "highlight", dimension: "自我揭露平衡", text: "適度分享自己的事" },
  { id: "H4", type: "highlight", dimension: "情緒感知", text: "讀懂並回應對方的情緒" },
];

export const REPORT_TONES = [
  "溫和鼓勵：先肯定再建議，語氣委婉",
  "直接具體：重點先講，不繞圈子",
  "像朋友聊天：口語、輕鬆",
  "專業顧問：條理分明、用詞精準",
];

export const REPORT_LENGTHS = ["1～2 則，約 500 字", "1 則，約 300 字", "2～3 則，約 800 字"];

// 來源：app/dialog/coach_report.py 的 REPORT_SECTIONS 與段落標題
export const DEFAULT_SECTIONS: ReportSection[] = [
  { key: "摘要", title: "這次聊起來的感覺", description: "整體表現的一句話總結" },
  { key: "做得好", title: "做得好的地方", description: "引用你的原句，說明哪裡做得好" },
  { key: "可調整", title: "可以再調整的地方", description: "引用你的原句，語氣委婉" },
  { key: "改寫示範", title: "換個說法試試", description: "針對可以調整的地方，示範一句更好的說法" },
  { key: "下次可試", title: "下次可以試試", description: "下次聊天可以嘗試的一個具體方向" },
];

export const CUSTOM_OPTION = "__custom__";
