## Why

平台過去以真人顧問代聊或授課協助男性會員提升聊天能力，成本高、會員抗拒上課，且真人代聊會讓客戶在互動中感到不適。本專案改以 Instagram 私訊上的 AI 角色提供低門檻的對話練習環境，並在對話後由 AI 顧問給出具體的溝通指導。

本 change 建立整個產品的地基：打通 Instagram Messaging 通道，並讓單一 AI 角色能以擬真的節奏與語氣進行對話，同時在每場對話結束時記錄教學所需的行為訊號。沒有這層地基，後續的教練回饋、使用者輪廓、配對與角色克隆都無從開始。

專案目前無任何程式碼，這是第一個 change。

## What Changes

**Instagram 通道**
- 新增 FastAPI webhook endpoint，接收 Meta Instagram Messaging 事件（`messages`、`messaging_postbacks`）
- 實作 `X-Hub-Signature-256` 簽章驗證，拒絕未通過驗證的請求
- 依 webhook payload 的 `entry[].id` 將事件路由至對應角色（MVP 僅 1 個角色，但保留多角色結構）
- 實作 24 小時訊息視窗的追蹤與強制檢查：逾期一律不得發訊
- 實作 1000 bytes UTF-8 的訊息切分與順序保證
- 實作每角色帳號的發訊速率配額

**對話 Session**
- Session 生命週期管理：建立、延續、靜默結束判定
- 滾動對話窗，控制 context 成長（摘要壓縮於 2026-10-12 移至 `add-persona-authoring-console`）
- 使用者身分處理：IGSID 以專業帳號為範圍，以 `username` 作為跨角色串接鍵

**角色對話**
- 人設卡以 YAML 定義，分為「給模型的」與「程式執行的」兩部分
- 人設卡渲染成 system prompt 的模板機制
- 對話開頭的 AI 揭露訊息（Meta 政策強制要求，非選配）
- typing indicator 與擬真回覆延遲

**即時訊號**
- 於 session 結束時偵測地雷、句點、亮點三類訊號並落庫
- 此為教練報告的唯一資料來源，不可延後至報告生成階段再行分析

**LLM 供應層**
- `LLMProvider` 抽象介面，對話生成／即時分析／教練回饋三條路徑各自獨立設定模型
- MVP 使用免費模型，供應商可抽換而不需修改業務邏輯
- 供應商失敗或觸及速率上限時的降級行為

**資料層**
- 建立 Postgres schema：`personas`、`users`、`user_persona_links`、`sessions`、`messages`、`realtime_signals`、`coach_reports`、`safety_events`、`rate_budget`
- `coach_reports` 與 `safety_events` 本 change 不使用，但先建表以避免日後遷移

## Capabilities

### New Capabilities

- `ig-messaging`: Instagram 私訊通道 —— webhook 接收與驗章、角色路由、訊息發送、24 小時視窗管理、訊息切分、發訊速率配額
- `conversation-session`: 對話 session 的生命週期、使用者身分歸戶、對話記憶
- `persona-chat`: 人設卡結構與渲染、角色回覆生成、AI 身分揭露、擬真對話節奏
- `realtime-signals`: session 結束時的行為訊號偵測與記錄（地雷／句點／亮點）
- `llm-provider`: LLM 供應層抽象 —— 分路徑模型設定、供應商抽換、失敗降級

### Modified Capabilities

（無。`openspec/specs/` 目前為空，本 change 是第一個引入規格的變更。）

## Impact

**新增程式碼**：整個 Python FastAPI 服務為新建，無既有程式碼受影響。

**新增依賴**：FastAPI、Postgres 16（psycopg）、Redis、YAML 解析、至少一家 LLM 供應商的 SDK。

**外部相依與前置條件**
- 需要 Meta 開發者帳號、商業驗證、Instagram 專業帳號，以及 `instagram_business_basic` + `instagram_business_manage_messages` 權限。此為關鍵路徑且不在本 change 範圍內，但開發可在 Development Mode 下先行進行（App 可與具備 app role 的帳號正常收發訊息）
- webhook 需要對外公開的 HTTPS endpoint，開發期以 Cloudflare Tunnel 或 ngrok 暴露

**已查證的平台限制（直接約束實作）**
- 24 小時訊息視窗：使用者發訊後 24h 內可主動發訊且不限則數；逾時完全不可發。點擊 postback 會重置視窗
- 不可對從未發訊的使用者主動開啟對話
- 訊息文字上限 1000 bytes UTF-8（中文約 330 字）
- `HUMAN_AGENT` tag 禁止用於自動化，不可作為逾時發訊的替代方案
- 官方文件未載明自動 DM 的每小時上限，第三方普遍回報約 200 則/小時/帳號（2026-09-23 查證：官方上限為 100 calls/秒/帳號，200 則/小時套錯對象；實測移至 `add-persona-authoring-console` 任務 11）

**不在本 change 範圍**：教練報告的生成與送達（Phase 3）、安全護欄與營運後台（Phase 4）、使用者輪廓萃取、配對比對、角色克隆。

**已知風險**：本 change 交付後，系統可對話但尚無安全護欄。在 Phase 4 完成前不得對外開放真實使用者，尤其首個角色主打「剛受挫、需要被安撫」的族群，情緒風險事件機率高於一般。
