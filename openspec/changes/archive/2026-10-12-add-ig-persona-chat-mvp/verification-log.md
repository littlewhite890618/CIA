<!-- 保存 tasks.md 已完成任務的驗證細節與查證依據，依任務編號排列。
     tasks.md 只留純勾選清單；想知道某項任務是何時、如何驗證的，來這裡查。 -->

### 4.1 實作 Instagram 發訊 API 客戶端，涵蓋文字訊息與輸入狀態提示
（`send_sender_action` 用 `typing_on`／`typing_off`；Meta 規定 sender_action
必須單獨送出，不能與訊息內容同一個請求）

### 4.7 實作發訊速率配額：達上限暫緩並於配額釋出後重試，不丟棄訊息
⚠️ **改用記憶體滾動計數，非 Redis**——POC 已決定不起 Docker（見 1.2），
無 Redis 可用。單一進程下行為等價，重啟歸零只會讓發訊更保守。
介面已隔離於 `app/channels/rate_limit.py`，Phase 1 換 Redis 只需替換該類別。

### 5.1 定義 `LLMProvider` 介面，並建立三條具名路徑的設定結構
2026-09-15 完成。`app/llm/base.py`：`LLMProvider` 抽象類別只有一個
`generate()` 方法——介面愈大，新增供應商的成本愈高。重試、退避、降級
刻意**不在**這一層，那是 `LLMRouter` 的職責（對所有供應商都一樣，
沒理由讓每家各寫一份）。此分工與 channels 層一致：`instagram.py` 管
「怎麼呼叫一次」，`sender.py` 管「什麼時候可以呼叫、失敗了怎麼辦」。
例外依**處置方式**分類而非依錯誤來源：`LLMRateLimited`／`LLMTimeout`
可重試，`LLMBlocked`（被安全機制擋下）重試必然再被擋所以不重試，
`LLMConfigError` 應在啟動時就浮現。

### 5.2 實作啟動時的供應商設定驗證：指向未實作的供應商時報錯並指出問題
2026-09-15 完成。`app/llm/registry.py` 的 `PROVIDERS` 是「有哪些供應商」的
**唯一真相來源**；`app/config.py` 的 `LLMPathConfig` 改為延遲匯入
`supported_providers()` 而非自己另存一份名單——兩份名單遲早不一致，
然後出現「設定驗證說可以、實際建構時說沒有」這種最難查的矛盾。
錯誤訊息一律指出**該改哪個環境變數**（如 `DIALOG_PROVIDER`），
只說「不支援」等於沒說。缺金鑰同樣在建構時報錯並指名 `GEMINI_API_KEY`。
`build_router()` 採全有全無：任一路徑設定壞掉就整個建構失敗，
不讓「壞掉那條靜靜不能用」拖到使用者發訊時才暴露。

### 5.3 實作至少一家免費供應商，並以設定切換驗證抽換不需修改業務邏輯
2026-09-15 完成。`app/llm/gemini.py` 直接打 REST 的 generateContent 端點，
不裝 google-genai SDK——理由同 `instagram.py`：多一個 SDK 就多一組版本相依
與一層自己的重試邏輯，而我們只需要一個端點。

⚠️ **實測發現原訂模型已退役**：`gemini-2.0-flash` 與 `gemini-2.5-flash`
皆回 HTTP 404，錯誤訊息指名改用 `gemini-3.6-flash`。同日實測三個候選：

| 模型 | 延遲 | 結果 |
|---|---|---|
| gemini-3.6-flash | 3.6 秒 | 採用 |
| gemini-3.8-flash | 17.5 秒 | 太慢，會吃掉整個擬真延遲預算（8–20 秒） |
| gemini-2.5-flash | — | 404 已退役 |

`.env` 與 `config.py` 的三條路徑預設值皆已更新。analysis 與 coaching 暫時
同用 3.6-flash（POC 不跑這兩條路徑），實際啟用時應改用 lite／pro 類模型。

抽換驗證：`tests/test_llm.py` 的 `test_抽換供應商不需修改呼叫端` 把
`FakeProvider` 註冊進 `PROVIDERS` 後直接可用，router 與呼叫端一行未改。

### 5.3 補充：gemini-3.6-flash 額度用盡時的手動備援（2026-09-16）
2026-09-15 開發期間打了約 12 次 gemini-3.6-flash（比較模型、診斷思考模式
問題），當日 RPD 額度（20/日）隔天（9/16）查詢仍顯示 12/20 已用——**不是
按日曆日期重置**，重置時間點未知。剩 8 次不足以支撐任務 9.3 要求的
15～30 則連續對話測試。

已排除：自動輪替需新程式碼，7.6／7.7 未完成前不划算；gemini-3.8-flash
已知 17.5 秒過慢。

2026-09-16 實測 gemini-3.7-flash（同一份人設卡 system prompt、同一句測試
訊息「今天上班好累喔」、關閉思考模式）：

| 模型 | 延遲 | 結果 |
|---|---|---|
| gemini-3.6-flash | 3.6 秒 | 主用，額度剩 8/20 |
| **gemini-3.7-flash** | **5.2 秒** | **驗證通過，列為備援** |
| gemini-3.8-flash | 17.5 秒 | 太慢，僅作最後手段 |

回覆「今天辛苦了，抱抱你……🌿 聽起來是忙了一整天呢，是工作量太多，還是
遇到了什麼心累的事呀？」語助詞、emoji、問感受的方式與 3.6 一致，未走樣。
5.2 秒仍在晴晴 8–20 秒的擬真延遲下限之內，不會露餡。

**切換方式**：`.env` 的 `DIALOG_MODEL` 改為 `gemini-3.7-flash` 並重啟服務，
不需要改動任何程式碼——這正是 5.3 抽換設計要達成的效果。3.6 額度告罄時
以此作為當日的手動備援，尚未實作自動偵測額度並切換的邏輯。

### 5.4 實作逾時中止、退避重試與速率限制處理
2026-09-15 完成。`app/llm/router.py`：HTTP 429 與 5xx 皆歸為 `LLMRateLimited`
走同一條退避路徑（供應商暫時性問題與速率限制的處置相同），退避為
`base × 2^嘗試次數`，預設重試 2 次。逾時由 provider 層中止並轉為 `LLMTimeout`。
`LLMBlocked` 明確排除在重試之外。測試以注入的假 sleep 驗證退避秒數序列，
毫秒級跑完。

### 5.5 實作對話生成路徑的降級回應
2026-09-15 完成。`app/llm/fallback.py` + `LLMRouter.generate_or_fallback()`，
後者**不再往上拋任何例外**——模型壞掉是我們的問題不是使用者的。
回傳 `(文字, LLMResponse | None)`，第二個元素為 None 即代表這次是降級，
讓呼叫端分得出來而不必靠猜。
降級語句刻意不帶特定語氣或口頭禪（那會跟人設卡打架），且隨機挑選——
模型失敗常連續發生，每次回同一句馬上會被看出是罐頭訊息。
有一項測試守住「降級語句不得含錯誤／失敗／系統／逾時等字眼」。
**已知限制**：這批句子與人設卡無關，晴晴講起來略顯中性。Phase 1 應把
降級語句移進人設卡，讓每個角色有自己的搪塞方式。

### 5.6 實作呼叫記錄：路徑、供應商、模型、耗時、token 使用量與失敗原因
2026-09-15 完成。成功與失敗各一條結構化日誌。記錄的 model 取自回應的
`modelVersion`（實際服務的版本，如 gemini-3.6-flash-001）而非設定值——
要記的是「實際用了哪個」。失敗記錄額外帶 `blocked`／`rate_limited`／
`timed_out` 三個布林旗標，免得日後翻日誌還要靠訊息字串猜分類。

### 第 5 群的兩個實作中發現（皆已處理）
**一、Gemini 3.x 的「思考」額度會計入 `maxOutputTokens`。**
實測 gemini-3.6-flash 預設開啟思考時，256 的額度被 thoughts 吃掉 244，
回覆只剩 8 token 且被 `MAX_TOKENS` 截在句中（「今天一定處理了很多繁」）。
設 `thinkingConfig.thinkingBudget = 0` 後：2.1 秒、完整回覆 41 token。
對話路徑要的是快，閒聊不需要推理，故 `GeminiProvider` 預設關閉思考；
教練回饋路徑（M5）需要時可經 `PathConfig.extra` 覆寫。

**二、觸及 token 上限時退回最後一個完整句子。**
斷句比少講一句更破壞擬真感。標點清單沿用 `splitter.SENTENCE_ENDINGS`。
實作初版有 bug：`rfind` 回傳 -1 時，`"! "` 這類兩字元分隔符會算出位置 1，
把整段砍到只剩第一個字，由 `test_截斷內容沒有任何句末標點時原樣保留` 抓到。

**真實 API 四輪對話驗證**（2026-09-15）：延遲 1.5–2.4 秒，無截斷，
語助詞／emoji／提問傾向皆符合人設卡，且「自己的煩惱」有自然帶出。
附帶觀察到邊界反應（「你身材很好耶」→ 回覆變短、停止自我揭露、轉移話題）
已由人設卡本身產生，7.7 可在此基礎上驗證而非從零建立。
全套 pytest 170 passed（本次新增 35 項）。

### 6.1 實作使用者身分歸戶：以 username 為跨角色識別鍵，IGSID 寫入 `user_persona_links`
2026-09-17 完成。零件在 M1 就都寫好了（`fetch_username()`、
`resolve_user()`），2026-09-15 查證發現兩者從未接線，`_handle_message`
無條件傳入 `ig_username=None`。本次補上：`_handle_event`／`_handle_message`
改為 `async def`，只在全新使用者（`find_user_by_igsid` 找不到既有連結）
時同步呼叫 `fetch_username()`；已有連結的使用者不必每則訊息都打 API。
best-effort：查詢失敗回傳 None，直接走 6.2 既有的降級路徑，不會讓一次
查詢失敗擋住整個對話。

**設計取捨**：同步呼叫而非丟進背景任務——只有真正第一次見面才付這次
網路延遲（一次 Graph API 呼叫，通常 <1 秒），且能沿用 `resolve_user()`
既有的「查完再決定要不要歸戶」邏輯；若改成背景處理，會變成使用者列已
建立後才發現要合併到別的使用者，需要額外的重新歸戶邏輯，複雜度不成
比例。webhook 交易在等待期間持有的 DB session 已由 `db/session.py`
的 WAL 模式保證可與其他讀寫並行。

**副作用發現並修正**：`InstagramClient.__init__` 原本立即建立
`httpx.AsyncClient()`（不像 `GeminiProvider` 是延遲建立），6.1 讓
`_handle_message` 對每個新使用者多建一個實例，實測讓整套測試從 12 秒
拖到 63 秒。已比照 `GeminiProvider._http` 的手法改為延遲建立，測試
恢復至 4.9 秒（甚至比改動前更快，因為多數既有測試也順帶受益）。

驗證：`tests/test_webhook.py` 新增 3 項——查到 username 時標記
`identity_confirmed=True`、查不到時維持既有的未確認降級行為、
已有連結的使用者三則訊息只觸發一次查詢。跨角色合併邏輯本身（同一
username 對應到同一 User）不變，沿用既有的
`test_跨角色歸戶_不同IGSID仍認出同一人`（`test_repository.py`）。

### 6.2 實作使用者資料取得失敗的降級：以 IGSID 建立可運作紀錄並標記身分未確認
隨 M1 通道整合完成，2026-09-15 回頭查證後補勾。
`repository.resolve_user()` 在 `ig_username` 為 None 時仍建立使用者紀錄，
以 `users.identity_confirmed = False` 標記（`app/db/models.py:134`）。
驗證：`tests/test_repository.py` 的 `test_無法取得username_仍可運作但標記未確認`。
⚠️ **目前線上走的其實只有這條降級路徑**——見 6.1 的接線缺口。

### 6.3 實作 session 的建立與延續判定
隨 M1 通道整合完成，2026-09-15 回頭查證後補勾。
`repository.get_active_session()` / `get_or_create_session()`，接線於
`app/main.py` 的 `_handle_message`。以 `sessions.state == ACTIVE` 判定延續，
找不到才建新場次，同時寫入 `window_expires_at`（24 小時視窗）。
驗證：`test_場次_建立與延續`、`test_場次結束後再發訊_建立新場次`。

### 6.5 實作入站與出站訊息的持久化
隨 M1 通道整合完成，2026-09-15 回頭查證後補勾。
入站 `save_inbound_message()` 接線於 `app/main.py`；出站
`save_outbound_message()` 接線於 `app/channels/sender.py` 的 `_record_delivered`。
兩者皆記錄方向、內容、`platform_message_id` 與時間；未送達的訊息另由
`save_undelivered_message()` 落庫並於 `meta` 記錄原因。
`platform_message_id` 的唯一索引同時支撐 3.6 的事件去重。
驗證：`test_訊息去重_重送的相同事件被擋下`、`test_出站訊息不重置視窗`。

### 6.4 實作靜默結束判定的背景輪詢迴圈，掃描逾靜默門檻的 session 並標記結束
2026-09-17 完成。資料層 `find_silent_sessions()`／`end_session()` 早在
M1 就寫好且有測試，缺的只是真正的輪詢迴圈。`app/main.py` 新增
`_silence_poll_loop()`（`while True: sleep → 單次掃描`）於 lifespan
啟動時以 `asyncio.create_task()` 建立、關閉時 `cancel()` 並等待收尾，
與 `llm_router` 同屬行程層級單例。

單次掃描抽成獨立的 `_run_silence_pass()`，讓測試能直接呼叫一次驗證，
不必真的等 `silence_poll_interval_seconds`（新設定，預設 60 秒，對應
`find_silent_sessions()` 文件裡原本就假定的「每分鐘呼叫」）。

驗證：`tests/test_reply_generation.py` 2 項——逾門檻的 session 被標記
`ended`；未逾門檻的正常對話不受輪詢影響（反面案例，避免誤傷進行中的
對話）。

### 6.7 實作跨 session 的話題記錄與載入，使角色不重複詢問已問過的問題
2026-09-17 完成，做的是使用者指定的完整版：session 結束時用 LLM 萃取
話題存進 `user_persona_links.topic_memory`，下次見面時載入 system
prompt。依賴 6.4 提供「session 結束」的時機點，兩者本次一併實作。

**萃取**：新模組 `app/dialog/topics.py` 的 `extract_topics()`，走
**analysis 路徑**而非 dialog——這是背景分析工作，不在使用者等待的
關鍵路徑上，對延遲、語氣沒有要求，符合 design.md 決策 5 對這條路徑
的定位。提示要求「每行一個話題、不超過 10 字、最多 8 個」，
`_parse_topics()` 逐行解析並容忍常見的編號/項目符號前綴、去重。
任何失敗（含降級回應）都回傳空清單——這是背景工作，失敗只代表下次
見面少一點記憶，不是對話中斷，不值得重試或告警。

**儲存**：`repository.append_topic_memory()` 與既有記憶去重合併，
新話題排在後面，上限 30 筆（避免長期帳號無限累積，POC 展示規模用不到
這麼多）。

**載入**：`render_system_prompt()` 新增 `known_topics` 參數（獨立參數
而非塞進 `PersonaPromptFields`——那是每個使用者各自不同的動態資料，
跟角色本身的靜態設定分開才不會混淆「這是誰的記憶」）。渲染在最後一段
【你們之前聊過的話題】，明確指示「不要重複詢問，但也不用刻意提起」。
`_send_reply` 透過 `repository.get_link_by_session()` 取得對應連結後
一併載入。

**串接鏈路**（`_close_session_and_extract_topics`）：找到逾門檻 session
→ 取整段對話（`recent_messages` 上限 200，遠超 POC 展示規模）→
`end_session()` → 呼叫 `extract_topics()` → 有結果才寫回
`topic_memory`。分兩段各自開短生命週期 db_session，中間夾一次 LLM
呼叫，不讓資料庫連線在等待網路 I/O 時被佔用。

驗證：`tests/test_topics.py` 11 項（純函式解析、`extract_topics` 走
analysis 路徑、無話題／無對話內容/呼叫失敗時安全回傳空清單）；
`tests/test_repository.py` 3 項（依 session 找連結、去重合併、
超過 30 筆只留最新）；`tests/test_reply_generation.py` 3 項，其中
**完整鏈路測試**驗證了 session 結束 → 萃取 → 新 session 的 system
prompt 真的看得到先前話題，這是這次實作最重要的一項驗證——證明
6.4 與 6.7 串起來確實運作，不只是各自的單元測試通過。

### 6.1／6.4／6.7 實作過程中的一個效能發現
`InstagramClient.__init__` 原本立即建立 `httpx.AsyncClient()`（不像
`GeminiProvider` 已於第 5 群改為延遲建立）。6.1 讓 `_handle_message`
對每個新使用者多建一個實例（僅為了 `fetch_username()` 這一次呼叫），
實測讓整套測試從 12 秒拖到 63 秒。已比照 `GeminiProvider._http` 的
手法改為延遲建立，測試恢復至 4.9 秒（甚至比改動前更快，因為多數既有
測試也順帶受益，見 `app/channels/instagram.py`）。

全套 pytest 203 passed（本次新增 22 項），連續三次執行皆穩定於 5.7 秒。

### context 視窗調整（非任務清單項目，但直接影響 9.3 能否進行）
`dialog_context_message_count` 由 20 調為 60（2026-09-17）。9.3 要求
30 則以上連續對話，20 則窗口會讓後段對話忘記前面講過的事，讓測試敗在
窗口太小而非人設或模型本身。Gemini Flash 的上下文窗遠遠塞得下 60 則
短訊息，成本影響可忽略。`.env` 與 `config.py` 預設值皆已更新。
這也是 6.6（摘要壓縮）延後的替代方案——見 tasks.md 該項註記。

### 7.1 定義人設卡的 YAML 結構，明確區分供模型使用的欄位與 `runtime` 程式參數
2026-09-11 完成。`app/personas/schema.py` 用兩個互不包含的 Pydantic 模型落實區分：
`PersonaPromptFields`（identity／background／personality／language_style／boundaries，
會進 system prompt）與 `PersonaRuntimeParams`（回覆延遲秒數、多則拆分機率）。
`PersonaCard.prompt_fields` 回傳的物件根本沒有 runtime 屬性——「runtime 不進提示」
是型別結構的保證，不是靠寫程式的人記得。
驗證：`tests/test_personas.py` 結構類 6 項，含 min>max、機率超出 0–1、興趣清單為空、
邊界三清單缺一等拒絕案例。

### 7.2 實作人設卡載入與結構驗證，缺少必要欄位時啟動失敗並指出缺漏欄位
2026-09-11 完成。`app/personas/loader.py` 的 `load_persona_card()`／`load_all_cards()`。
缺欄位時錯誤訊息指出**完整路徑**（如 `background.自己的煩惱`），不只說「缺欄位」。
目錄批次載入採全有全無：任一張卡不合法就整批拋錯，不悄悄跳過壞卡；
`app/main.py` lifespan 於啟動時呼叫，載入失敗即服務起不來。
驗證：載入類 9 項，涵蓋 YAML 語法錯誤、內容非對應表、巢狀欄位缺漏、一壞全壞。

### 7.3 實作人設卡至 system prompt 的渲染模板，確保 `runtime` 參數不進入提示
2026-09-11 完成。`app/personas/prompt.py` 的
`render_system_prompt(fields: PersonaPromptFields)`——簽章只收 PromptFields，
想把整張 `PersonaCard` 或 runtime 傳進去會被靜態型別檢查擋下（執行期沒有額外
isinstance 檢查，擋不住硬要繞過型別的呼叫，但正常路徑寫不出來）。
驗證：渲染類 5 項，其中一項刻意讓卡片帶著 runtime 欄位後渲染，檢查輸出不含
「回覆延遲秒數」「多則拆分機率」「typing_indicator」等標籤。

### 7.4 依 `docs/人設卡草案-晴晴.yaml` 建立首個角色的人設卡檔案
2026-09-11 完成。正式檔案落在 `app/personas/cards/sunny.yaml`（晴晴），由草案遷移而來；
草案檔保留並加註記指向正式路徑，不刪除，以留住原始設計討論脈絡。
驗證：真實卡片可成功載入且 runtime 讀到 8–40 秒；另有一項檢查卡片 slug 與
`app/main.py` seed 的 persona slug 一致，避免日後改名造成靜默失聯。

### 7.5 實作 AI 身分揭露：新對話開頭與長時間中斷後皆執行，且無法由設定關閉
2026-09-11 完成。`app/personas/disclosure.py`：`should_disclose()`（新場次，或距上次
互動達靜默門檻）與 `compose_reply()`（把揭露文字接在回覆前）。
**無法被設定關閉是結構性的**——兩個函式都沒有任何停用參數，spec 的 MUST NOT
不靠「設定預設為開」來滿足；日後要加開關必須主動改函式簽章，不會是不小心接上的一行。
門檻沿用 conversation-session 的 `silence_threshold_seconds`，不另立設定值。
接線：`app/main.py` `_handle_message` 在 `touch_session_by_user_activity()` 覆寫
`last_message_at` **之前**先算出 disclose 旗標（順序反了會變成永遠不揭露），
`_send_reply`（7.8 前名為 `_send_fixed_reply`）以 `compose_reply()` 組出實際送出的文字。
驗證：揭露類 9 項單元測試，另加 `tests/test_webhook.py` 2 項整合測試——
走完整 webhook → 發訊管線後，從 DB 讀回 outbound 內容確認首則含揭露文字、
短時間內第二則不重複。單元測試測不出「忘了接線」，所以兩層都要。
全套 pytest 112 passed（本次改動前 110）。

### 7.6 實作角色回覆生成，串接人設卡、對話上下文與 LLM 供應層
2026-09-16 完成。串接鏈：`repository.recent_messages()`（既有，M1）→
`app/dialog/context.py` 新增的 `build_context()`（DB 的 direction 對應
LLM 的 role）→ `render_system_prompt()`（既有，7.3）→ `llm_router`（既有，
第 5 群）→ `send_paced_reply()`（既有，7.8）。`_send_reply` 從
`FIXED_REPLY` 改為真的呼叫 `llm_router.generate_or_fallback("dialog", ...)`；
`llm_router` 為 None（金鑰未設）時直接走 `pick_fallback()`，與呼叫失敗
走同一句降級語句——使用者分辨不出「沒接」與「接了但失敗」的差異，
這差異只對開發者有意義。
驗證：`tests/test_reply_generation.py` 5 項，走完整 webhook 管線、
`llm_router` 換成 `FakeDialogProvider`，確認回覆內容來自 LLM、
system prompt 含身分與語言風格、對話歷史正確累積且方向對應到
user/model 角色、缺金鑰時走降級不嘗試呼叫。

### 7.7 實作邊界反應：變冷淡、絕不接、以及使用者修正後的回暖行為
2026-09-16 完成。邊界反應主要由人設卡的 system prompt 驅動——
2026-09-15 對 gemini-3.6-flash 的真實對話測試已證實：完全沒有額外
程式邏輯的情況下，「你身材很好耶」就讓回覆長度從 91 tokens 掉到 28、
停止自我揭露、把話題拉回上一輪（見該日驗證記錄）。本次補的是
`app/personas/prompt.py` 缺的一段：**回暖**。原本的提示只交代
「變冷淡時怎麼退回去」，完全沒交代「使用者轉回熱絡話題後該怎麼恢復」，
模型讀不到「該回暖了」這件事，只會一路冷下去。新增段落明確引導模型
在使用者談及「變熱絡」清單內容後，於數則內自然恢復，且不要提起
「你剛才怎樣怎樣」。
刻意不做規則式的關鍵字偵測：邊界清單（如「連續查戶口式提問」「抱怨
前任、貶低女性」）是語意分類，不是字面關鍵字，規則比對不可靠又增加
複雜度，design.md 決策 4 本來就是把邊界判斷交給模型。
驗證：`tests/test_reply_generation.py` 3 項，結構層級確認變冷淡／絕不接／
回暖三段文字都有渲染進送給模型的 system prompt。**實際行為驗證**（冷淡
反應是否自然、回暖是否到位）刻意不消耗額度重複驗證——留給 9.4 用真實
對話測，那本來就要花使用者自己控制的額度。


### 7.8 實作擬真節奏：依 `runtime` 的延遲區間與拆分機率控制送出時機與則數
2026-09-15 完成。分成兩層：`app/dialog/pacing.py`（純函式，算延遲與拆分）
與 `app/dialog/delivery.py`（編排等待、typing 提示與逐則送出）。節奏放在
`app/dialog/` 而非 `app/channels/`，因為它是角色個性不是平台規則——
channels 層管的 1000 bytes、24 小時視窗、速率配額對所有角色都一樣。

**typing 提示刻意切成兩段**（安靜等待 → 末段才亮起，`TYPING_LEAD_SECONDS = 8`）。
Meta 的輸入提示約 10–20 秒自動消失，而晴晴延遲上限 40 秒，整段掛著會在
使用者眼前提早熄掉，看起來像「打到一半放棄」，比不顯示更怪。

`MessageSender.send_reply` 因此新增 `typing: bool = True` 參數：節奏層自行
接管提示時傳 False，避免兩邊互相開關。預設值維持原行為，既有呼叫端不受影響。
句末標點清單從 `splitter._SENTENCE` 改為公開的 `SENTENCE_ENDINGS` 供兩處共用，
不複製第二份。

**踩到的坑**：初版在送出後無條件把 typing 標記歸零，但 Meta 只在訊息**實際
送達**時才清除提示——發訊失敗時提示還亮在對方畫面上。由測試
`test_中途失敗仍送出_typing_off` 抓到，已改為僅於 `outcome.ok` 時歸零。

**第二個坑**：接上 main.py 後 `tests/test_webhook.py` 每筆假 payload 都真的睡
8–40 秒，全套測試從 4 秒變 165 秒。`delivery.DEFAULT_SLEEP` 因此抽成模組層級
變數供測試替換（與該檔案把 `send_text` 換成假物件同一理由），全套恢復為 7 秒。

驗證：`tests/test_pacing.py` 23 項，sleep 與 rng 皆注入，測試為毫秒級。涵蓋
延遲落在 8–40 區間（200 顆種子）、機率 0 必單則／機率 1 必兩則、拆分後非空白
字元不增不減不亂序、過短或單句不拆、typing 開關與時序、中途失敗不送後續則數，
以及 7.5 × 7.8 的交互——**揭露文字永遠落在第一則**（拆分若把揭露推到第二則，
等於使用者先看到角色說話才被告知對方是 AI，違反 persona-chat spec）。
另以真實人設卡抽樣 1000 次：拆分 150 次，與設定的 0.15 相符。
全套 pytest 135 passed（本次改動前 112）。
手機端的實機節奏觀察屬 9.5，尚未執行。

### 7.9 實作延遲期間使用者再次發訊的合併或取消處理
2026-09-16 完成，但**設計方向與 7.8 verification-log 原先寫的不同**。
7.8 完成時預想的做法是「用 asyncio.Task 包住整個發送流程，新訊息進來
就 task.cancel()」，實作時才發現行不通：FastAPI 的 BackgroundTasks
不會回傳可取消的 Task 物件，不同次 webhook 請求之間也拿不到彼此的
handle，要做到真取消得整個換掉背景任務排程機制，牽動既有測試依賴的
「背景任務在回應前跑完」特性，代價過高。

改用「發送前檢查是否已過時」取代真正的任務取消：
- `repository.has_newer_inbound(db, session_id, since_message_id)`——
  查詢該 session 是否存在 id 更大的入站訊息
- `_send_reply` 記住自己是被哪一則訊息（`message_id`）觸發的，在
  **生成前**與**擬真延遲跑完、送出前**各檢查一次；任一次發現已過時
  就整個放棄，不生成也不送出
- 新訊息會觸發自己的一次全新處理，其 `recent_messages()` 上下文本來
  就會包含被跳過的那則訊息——等同把兩則話「合併」進同一次理解，
  不需要真的合併文字內容

`app/dialog/delivery.py` 的 `send_paced_reply()` 新增 `is_stale` 參數
（可選，預設 None＝從不過時，向後相容），只在**第一則**送出前檢查一次——
延遲區間（8–20 秒）遠比則與則之間的間隔（2–6 秒）長，是使用者最可能
插話的窗口，中途每則都檢查的邊際效益低，不值得為此複雜化。

**代價**：若使用者剛好在「正在生成 LLM 回覆」的那幾秒插話，那次生成
不會被中斷，會在送出前的檢查被攔下，浪費一次 LLM 呼叫額度但不會浪費
使用者體驗（不會收到過時內容）。POC 階段可接受。

驗證：`tests/test_reply_generation.py` 3 項——核心情境（延遲期間插話則
放棄過時回覆，第一則的內容確認未送出、只送出第二則）、插話後的新回覆
帶有完整上下文（最後一次 LLM 呼叫的訊息列表同時含兩則使用者發言）、
反面案例（沒有插話時正常送出不受影響）。測試技巧：無法用 TestClient
的同步 HTTP 介面在背景任務內部模擬「使用者又發訊」（anyio 的 blocking
portal 不允許從事件迴圈執行緒內重入呼叫），改為直接呼叫 `_handle_event`
落庫、再手動執行其排入的背景任務，等同 Starlette 在正常請求週期對
BackgroundTasks 做的事。

### 實作 7.6 時發現並修正兩個問題（皆與新程式碼無關的既有風險）

**一、測試曾意外連上真實 Gemini API，燒掉額度。** `tests/test_webhook.py`
的 fixture 沒有明確清空 `GEMINI_API_KEY`，pydantic-settings 因此從開發者
本機 `.env` 讀到真實金鑰，讓 `llm_router` 在測試中對外發出真實請求——
這件事在 7.6 把 `llm_router` 真正接上發訊管線之前不會發生，因為先前
`llm_router` 建了但從未被呼叫。已在 fixture 明確設定
`GEMINI_API_KEY=""`，`tests/test_reply_generation.py` 的新 fixture
同樣明確清空。

**二、`_send_reply` 從同步函式包 `asyncio.run()` 改為 `async def`。**
原本的寫法對只用一次性資源的 `InstagramClient` 沒有問題（每次呼叫都在
函式內全新建立、同一個事件迴圈內關閉），但 `llm_router` 的供應商實例
是 lifespan 建立、跨請求存活的長壽物件，內部 httpx client 延遲建立。
若 `_send_reply` 仍用 `asyncio.run()` 各開一個新事件迴圈，供應商的
httpx client 會綁死在第一次呼叫時的那個迴圈上，下一次背景任務（新的
`asyncio.run()`、新的迴圈）再用同一個供應商就會撞上
`RuntimeError: Event loop is closed`。改成 `async def` 後，FastAPI 的
BackgroundTasks 會把這個協程直接排進主事件迴圈（與 `llm_router` 建立
時同一個），問題不再發生。

**三、既有的兩項揭露測試（7.5 建立）有潛伏的間歇性失敗風險。** 晴晴的
拆分機率（7.8）是 0.15，`send_paced_reply` 未指定 `rng` 時用真正隨機數，
約每 7 次就有 1 次把回覆隨機拆成兩則，讓「剛好一則 outbound」的斷言
間歇性失敗。這個風險自 7.8 接上 webhook 那天就存在，只是先前很少連續
跑幾十次沒撞到；今天因反覆重跑套件次數多而顯現。已在 `test_webhook.py`
的 fixture 把 `persona_cards["sunny"].runtime.多則拆分機率` 鎖為 0——
拆分邏輯本身已由 `tests/test_pacing.py` 用固定種子完整覆蓋，不影響
測試涵蓋範圍。

全套 pytest 181 passed（本次新增 11 項，另修正既有間歇性失敗）。

### 8.2 實作訊號偵測，於 session 結束時以背景任務對整段對話分析一次
2026-09-22 以真實 IG 對話驗證（session 26，5 則使用者訊息，其中一則刻意索取 LINE）。
靜默 25 分鐘後自動偵測，抓到 6 筆：4 筆 H2 接住話題、L1 索取聯絡方式、
同一則另有 K3（要 LINE 前沒回應角色的提問）——單則多訊號情境成立。
過程中主模型 `gemini-3.5-flash-lite` 回 HTTP 400，改由備援完成，對話不受影響。
同日手動重跑同一場，結果為 5 筆（K3 消失、267／270 由 H2 改判 H3）——
第一次用的備援是 `gemini-3.8-flash`，重跑時備援已改為 `3.1-flash-lite`。

### 8.5 驗證訊號涵蓋六項評分維度，且可回溯至使用者原句
2026-09-22 分三部分驗證：
1. **對照表**：`test_六項維度每一項都至少有一個訊號項目` 通過。
2. **原句回溯**（真實資料）：session 26 的 5 筆訊號 join `messages`，
   5 筆全部對應到該場次的使用者入站訊息，無孤兒、無指到角色訊息。
3. **模擬實測**：自寫 17 則對話（使用者 10 則，每個維度至少安排一句），
   不經 IG、不寫資料庫，直接送真實 Gemini 偵測 3 次：
   - **3 次皆涵蓋全部六項維度。**
   - 穩定抓到：H1、H4、H3、K1、L8、L9、L4、L3、L10（程式判斷）。
   - 第 1、2 次結果完全相同；第 3 次漏掉 #3 的 H2（H4 仍在）。
     → 同一模型下判定大致穩定，8.2 那次較大的差異應主要來自換了備援模型。
   - 沒抓到的預期項目：#5 同時有 H1（只給 H3）、#7 同時有 K3（只給 K1）、
     #11 的 K2（單則內問三個問題，模型判 L8；K2 定義是跨則連發，預期本身有爭議）。
     → 模型傾向每則只給最明顯的一個項目，單則多訊號的偵測率偏低。
   - ⚠️ **主模型 `gemini-3.5-flash-lite` 在 analysis 路徑 3 次全部回 HTTP 400
     （INVALID_ARGUMENT）**，每次都靠備援 `3.1-flash-lite` 完成。
     同日查明並修正：程式固定送 `thinkingBudget=0`（關閉思考），3.5 世代
     不能完全關閉思考，一律回 400；dialog 路徑其實也中了，所有回覆都由備援
     生成、只在日誌看得到。改送 `thinkingLevel="minimal"` 後，dialog 與
     analysis 皆由 3.5-flash-lite 直接完成（詳見 `app/llm/gemini.py` 註解）。
   - 修正後以 **3.5-flash-lite** 重跑同一段模擬 3 次：六項維度仍 3 次全涵蓋，但
     - 3 次結果兩兩不同（3.1 是 2 次相同），#1 的 H1 有 2 次漏抓。
     - 出現 3.1 沒有的**誤判**：#9 單獨一句「你是哪裡人？」3 次都判 L8
       （違反「跨則模式只標在達成條件那一則」）；#3「你是不是其實很難開口
       拒絕？」是封閉式問題卻判 H1（2 次）；第 1 次把「下次一起去吃飯？」
       同時判成 L1 索取聯絡方式。
     → 這段對話上 3.1-flash-lite 較適合 analysis 路徑（只有漏抓、沒有誤判）。
       樣本僅 1 段對話 × 3 次，是否換模型待使用者決定。

### 9.1 以真實 Instagram 手機 App 驗證「收訊 → 回覆 → 存 DB」全流程
⚠️ 原文寫「於 Development Mode」，實測**不成立**：開發模式下 Meta 完全不轉發
真實私訊。必須把 App 切換為「已發佈」才會收到 webhook（詳見 `docs/開發環境設定.md` A-7）。
2026-09-08 驗證通過：inbound 與 outbound 各一筆落庫，兩筆皆取得真實 Meta message_id。
驗章一度失敗，原因是用錯密鑰——webhook 簽章要用 **Instagram 專屬密鑰**
(`IG_APP_SECRET`)，不是 `META_APP_SECRET`。已改正，六筆真實封包全數通過驗證。

### 9.2 驗證簽章失敗、未知帳號、重複事件三種異常路徑的行為符合規格
2026-09-10 以 `scripts/m1_verify.py webhook <case>` 對真實公開網址實測：
- 簽章失敗 → HTTP 403，`messages` 無新增，log 記錄「簽章驗證失敗，拒絕請求」
- 未知帳號 → HTTP 200（不中斷批次），無新增資料，log 記錄
  「未知角色帳號，略過此事件 entry_id='99999999999999'」
- 重複事件 → 兩次皆 200，同一 `platform_message_id` 僅落庫一筆
重複事件那項刻意使用合成 IGSID，避免入站訊息重置 session 1 的 24 小時視窗
（該視窗是 9.6 的驗證前提，需自然過期，重置後要再等一天）。

### 9.6 刻意讓對話靜置逾 24 小時，確認系統拒發並標記未送達
2026-09-10 驗證通過。session 1 自 2026-09-09 02:57 起無入站訊息，
視窗於 2026-09-10 02:57 自然到期；逾期 15.5 小時後以
`scripts/m1_verify.py send` 觸發發訊，結果：
- `blocked_reason = messaging_window_expired`
- 耗時 0.05 秒 → 證明**未呼叫** Instagram API
- 落庫一筆 outbound，`platform_message_id` 為 NULL、
  `meta = {delivered: false, reason: "messaging_window_expired"}`
**Meta 端行為亦已確認**（額外驗證，非本任務要求）：繞過我方檢查
直接呼叫發訊 API，Meta 回傳
`HTTP 403 / IGApiException / code 10 / error_subcode 2534022`
「這則訊息並未在允許的期間內傳送。」
→ 已據此在 `app/channels/instagram.py` 新增 `InstagramWindowExpired`
錯誤分類，讓平台端拒絕也能記錄為視窗逾期而非籠統的發訊失敗。
