---
version: 1
slug: "frontend-src-settings-settingspage-tsx"
primary_target: "frontend/src/settings/SettingsPage.tsx"
related_targets: []
---

# 使用者設定頁（晴晴的設定）

## Scope

- Mode: Operate。使用者完成任務：調整共用角色對自己呈現的人設與教練回饋
- 入口：私訊傳「設定」取得的專屬連結（`/settings?t=<token>`，30 分鐘有效），多半在手機的 Instagram 內建瀏覽器開啟
- 需求依據：`openspec/changes/add-persona-authoring-console/specs/persona-authoring/spec.md`；預覽第 2 版只約束欄位，不約束視覺，不做右側即時預覽
- 使用者回答（2026-10-15）：各區段同等重要；最怕成品像「工程後台」；第一次打開的人會從頭到尾設定一遍
- 頁面只出現角色名字，不放產品名或 logo；文案用中立的設定頁口吻

## Direction contract

THESIS: 這是一本替晴晴填的同學錄。九個區段是九頁，固定框架是印好的題目，使用者的選擇是寫在橫線上的原子筆字。拒絕的預設：灰底分組清單的 App 設定頁，以及一條長到看不到底的問卷表單。

OWN-WORLD: 白色活頁紙，淺藍橫線與淺藍頁邊線；印好的題目與系統底線是深藍印刷字，使用者的值是藍色原子筆；改過的值底下有一道淺藍螢光筆；錯誤是紅筆批改；頁首是白色封面與藍色儲存鈕。標題用粉圓體，內文用系統黑體。一律白底，不做夜間版（2026-10-15 使用者經三輪配色定案：要白底、藍字、清新不暗沉）。

STORY: 使用者知道自己在替晴晴寫資料，一頁一頁往下填；看得出哪些是印好不能改的、哪些是自己寫的、哪些改過；存檔後知道從下一則訊息生效，一鍵回到私訊。

FIRST VIEWPORT: 頂部白色封面帶：「晴晴的設定」、連結剩餘時間、儲存按鈕。其下是可橫向捲動的頁籤（九頁，改過的頁有螢光點）。主體是第一頁「身分」：大字頁名與「第 1 頁／共 9 頁」，欄位一行一條橫線，名字與 IG 名稱為印刷字（不可改）。底部固定列：上一頁、下一頁。

FORM: 同學錄個人資料頁（我排序清單的第 1 名，IMPECCABLE’S PICK）；seed key 2c0d4ca5。招牌動作：值一改，螢光筆從左到右畫過它（減少動態時直接出現）。

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Memorable moment

改了一個值，淺藍螢光筆劃過去；頁籤上那一頁亮起螢光點。

## Unresolved

- 客戶統計頁、管理員頁要不要沿用同一個世界（另案）
- 粉圓體在 IG 內建瀏覽器的載入速度需實測
