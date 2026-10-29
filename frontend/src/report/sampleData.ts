// 開發模式沒有 token 時用的範例資料，用來檢視版面與各種狀態（?state=writing|failed|pending|expired|used|invalid）。
// 內容是示意，不是真實使用者的對話。

import type { ReportData } from "./api";

export function sampleReport(): ReportData {
  const now = Date.now();
  const iso = (hoursAgo: number) => new Date(now - hoursAgo * 3_600_000).toISOString();
  return {
    personaName: "晴晴",
    expiresAt: new Date(now + 6.5 * 86_400_000).toISOString(),
    chatUrl: "https://www.instagram.com/direct/inbox/",
    sessions: [
      {
        id: 1,
        startedAt: iso(8),
        endedAt: iso(7.4),
        status: "ready",
        sections: [
          {
            title: "這次聊起來的感覺",
            body: "整體聊得很自然，你很願意分享自己的事，也有好幾次把話題丟回給晴晴。",
          },
          {
            title: "做得好的地方",
            body: "你問了「那你在研究所平時都在幹嘛？」這種開放式的問題，讓晴晴有很多可以說。\n她提到論文很累時，你回「聽起來很辛苦」，有接住她的情緒。",
          },
          {
            title: "可以再調整的地方",
            body: "有一段你連續問了「你幾歲？」「住哪裡？」「做什麼工作的？」，一次問太多基本資料，容易讓人覺得在被查戶口。",
          },
          {
            title: "換個說法試試",
            body: "想多認識她的時候，可以先分享自己再問：「我最近在學做菜，常常失敗哈哈，你平常會自己煮嗎？」",
          },
          {
            title: "下次可以試試",
            body: "記住她說過的小事，下次聊天時主動提起，例如問她論文的資料分析有沒有進展。",
          },
        ],
      },
      {
        id: 2,
        startedAt: iso(5),
        endedAt: iso(4.6),
        status: "not_generated",
        sections: [],
      },
      {
        id: 3,
        startedAt: iso(3),
        endedAt: iso(2.7),
        status: "not_generated",
        sections: [],
      },
    ],
  };
}
