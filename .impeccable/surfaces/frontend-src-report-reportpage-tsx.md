---
version: 1
slug: "frontend-src-report-reportpage-tsx"
primary_target: "frontend/src/report/ReportPage.tsx"
related_targets: ["frontend/src/report/report.css","frontend/report.html"]
---

# 晴晴的聊天回饋（教練回饋網頁）

## Scope

- Mode: Read。使用者要讀懂：上一段時間每段對話的回饋
- 入口：凌晨 04:00 私訊送出的連結（`/report?t=<token>`，7 天有效、只限第一個打開的瀏覽器），多半在手機的 Instagram 內建瀏覽器開啟
- 需求依據：`openspec/changes/daily-coach-report-link/specs/coach-report/spec.md`
- 使用者回答（2026-10-21）：每段完整內容依序往下排；「可以再調整的地方」跟其他段落一樣用藍筆，不用紅筆；最怕像成績單或考卷、跟設定頁不像同一個產品、文字太密讀得累
- 打開才逐段產生報告（每段約 5～10 秒），所以「整理中」是常見狀態，不是例外
- 頁面只出現角色名字，不放產品名或 logo；中立口吻，不是晴晴在說話

## Direction contract

THESIS: 每段對話是一張疊起來的回饋信紙，抬頭寫日期與時段，回饋的段落是信裡的分段，一張讀完往下是下一張。沒有稱呼、沒有署名，不是晴晴寫的信。拒絕的預設：分數、等第、對錯記號的成績單，以及卡片牆式的報表。

OWN-WORLD: 跟設定頁同一本活頁：白紙、淺藍頁邊線與橫線、深藍印刷字的段落小標、藍色原子筆。信紙抬頭的日期用粉圓體藍筆字；內文是深藍、寬行距、約 34 字一行。使用者自己說過的原句（「」內）用藍色原子筆字，和設定頁「藍筆＝你寫的」同一個意思；「換個說法試試」裡的示範句底下畫淺藍螢光筆。不用紅筆。一律白底。

STORY: 使用者知道這是昨天那幾段聊天的回饋，一張一張往下讀，認得出哪句是自己說過的、哪句是可以換的說法，讀完一鍵回到私訊。

FIRST VIEWPORT: 頂部白色封面帶：「晴晴的聊天回饋」與「2 段對話・連結 6 天後到期」，右側「回到聊天」。其下是第一張信紙：藍筆粉圓體抬頭「10 月 21 日（二） 晚上 9:40–10:15」（時段略小、同一行；同一晚的幾張信紙靠時段分辨，2026-10-22 完工審查後調整）、一道雙橫線，接著第一個段落小標與內文。信紙底下露出兩張紙邊。

FORM: 疊起來的信紙（我排序清單的第 4 名）；seed key 87214797。招牌動作：整理中時，藍色原子筆的筆跡沿著空白橫線一行一行寫過去；整理好時，字由左到右寫上信紙（減少動態時只留橫線、字直接出現）。排在後面的信紙是空白橫線加一句等候說明。

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Memorable moment

整理中的那張信紙只有淺藍橫線，字從左到右一行一行寫上去。

## Unresolved

- 回饋訊息（私訊）的確切文案，實作時與使用者確認
