<!-- 前置依賴：本 change 排在 M5／M9（教練報告生成與送達，見 PROGRESS.md「MVP」段落）之後。
     另：archive 前必須先 archive add-ig-persona-chat-mvp，否則
     persona-chat 與 realtime-signals 的 delta 無主規格可套用。 -->

## 1. 實作前的查證（阻擋性）

- [x] 1.1 查證 Meta 的發訊速率限制屬 App 級或帳號級，據以估算單一 Meta App 可承載的客戶角色數
      2026-09-23 查證：**帳號級**。官方文件載明「per Instagram professional account」——
      文字／連結／表情／貼圖 100 calls/秒、音訊影片 10 calls/秒、Conversations API 2 calls/秒，
      皆以每個 IG 專業帳號為單位。多客戶不互相稀釋配額，承載量不受發訊速率限制約束。
      來源：https://developers.facebook.com/docs/instagram-platform/overview/
- [x] 1.2 查證 Meta 違規處分的波及範圍：單一 IG 帳號違規是否導致整個 App 受罰
      2026-09-23 查證：未見明文規範 App 層級的連坐，但 Business Portfolio 的限制確實會連坐
      （共用 IP／付款方式／登入 session 等訊號即視為同一實體），帳號層級處分亦會因共用
      Accounts Center 身分波及連結的應用。風險成立，但無法以拆 App 規避——
      Account Integrity 政策明文禁止以商業資產規避處分，拆 App 反可能被判定為 evasion。
- [x] 1.3 依 1.1、1.2 的結果評估是否需改為「一客戶一 App」，若需要則回頭修訂 design.md 與本任務清單
      2026-09-23 結論：**維持單一 Meta App**。拆 App 既不能解決連坐（訊號會被關聯），
      又有被判定為規避處分的風險。design.md 的風險段已據此改寫：風險控制完全倚賴審核關卡。
- [x] 1.4 與客戶方確認「客戶不可見終端使用者原始對話」這項假設可否接受；若不可接受，另開個資同意的評估，不在本 change 範圍
      2026-10-12 確認：**客戶不可見終端使用者原始對話**，僅提供彙總統計。design.md 決策 6 維持原設計，不需另開個資同意評估

## 2. 前置重構：人設卡欄位名稱改英文

- [x] 2.1 將 `app/personas/schema.py` 的中文欄位名稱改為英文，維持巢狀結構不變
- [x] 2.2 同步 `app/personas/prompt.py` 的取值處，確認渲染輸出的段落標題與內容完全未變
      另同步 `app/dialog/signals.py`（邊界清單）與 `app/dialog/pacing.py`（回覆延遲）的取值處
- [x] 2.3 同步 `app/personas/cards/sunny.yaml` 的欄位名稱
- [x] 2.4 更新受影響的測試，全套 pytest 維持綠燈；以既有的人設卡渲染結果逐字比對，確認提示內容零變化
      2026-10-12：273 passed／9 skipped，與改名前相同。晴晴的 system prompt（含話題記憶與前場次段落）
      改名前後逐字相同（1469 字）。`app/personas/test_dialog/` 的驗收腳本保留中文欄位名作為對照說明，未改
- [x] 2.5 移除「自己的煩惱」欄位（design.md 決策 9）：`schema.py` 的欄位與說明、`prompt.py` 的渲染段落、`sunny.yaml` 的內容、`tests/test_personas.py` 中以此欄位為例的測試。須在 2.4 綠燈後才做，兩者分開驗證
      2026-10-12：273 passed／9 skipped。晴晴的 system prompt 與改名後版本比對，唯一差異為
      「【你自己的煩惱】」段落消失（1469 → 1399 字）。巢狀缺漏測試改以 `background.interests` 為例
- [x] 2.6 同步 `docs/人設卡草案-晴晴.yaml`，註明此欄位已移除與原因
- [x] 2.7 真機重新確認晴晴：以真實 IG 對話檢查人設一致性，並觀察角色是否仍會流露情緒、讓使用者有機會接住（影響「情緒感知」維度）
      2026-10-12（session 39）：通過。語助詞、emoji、訊息長度、反問方式與移除前一致；角色仍會主動
      說出具體心事（班上孩子的事），未退化成只反問的客服；回覆延遲約 20 秒，在設定區間內。
      已知弱點：心事素材只剩「近況」一項，連續追問三次皆回答同一件事、無新細節。此屬角色內容
      而非結構問題，日後由客戶在「近況」等欄位自行填寫豐富內容，不在本任務處理

## 3. 資料層

- [x] 3.1 建立客戶（租戶）資料表，含帳號識別與狀態
      `tenants`：name（唯一）、status（active／disabled）。登入帳號屬 5.1，不在此表
- [x] 3.2 `personas` 新增擁有者外鍵，既有檔案來源角色的擁有者為空值（代表系統內建）
      另將 `ig_account_id` 改為可為 NULL：客戶角色核准並完成平台作業後才回填（決策 5），否則草稿無法存入
- [x] 3.3 `personas` 新增人設卡內容欄位，以單一 JSON 整份儲存
- [x] 3.4 建立教練設定的儲存結構，與角色關聯
      `persona_coaching`（一角色一列）：disabled_rule_ids、custom_rules、report_tone、report_length、
      report_sections，各欄 NULL＝沿用系統預設
- [x] 3.5 將新增欄位登記至既有的啟動補欄位機制
      2026-10-12：「啟動補欄位機制」已於 10/2 改用 Alembic 時取代，改為建立遷移
      `alembic/versions/0002_tenants_and_persona_content.py`（含降級）。`scripts/migrate_sqlite_to_postgres.py`
      的 TABLE_ORDER 同步加入兩張新表。測試：SQLite 281 passed（新增 `tests/test_tenancy_schema.py` 8 項）；
      Postgres 11 passed，含「遷移後結構與 models 完全一致」與「0002 可降級再升級」
      2026-10-12 已套用至正式資料庫（套用前備份 `data/cia-backup-20261012-181058-before-0002.dump`，前後筆數一致）

**依 design.md 決策 12、13（2026-10-13）調整資料層**：以下以遷移 0003 完成，套用前須再備份一次。
3.6～3.10 曾依「客戶版本」設計實作一版（10/13 上午，未套用至正式資料庫），依決策 13 改寫後重做

- [x] 3.6 新增使用者版本表：以 `user_persona_links`（使用者 × 共用角色）為單位，一個連結至多一份；存整份人設卡 JSON（名字與 IG 顯示名稱不存，沿用共用角色）與更新時間。無審核狀態
      `user_persona_versions`：link_id（唯一）、card（必填；設為 `none_as_null`，否則 Python 的 None
      會被存成 JSON 的 null 而繞過 NOT NULL——測試抓到）
- [x] 3.7 `persona_coaching` 改為掛在使用者版本上（取代掛在 `personas`）
      欄位 `persona_id` → `version_id`；表在 0002 後無資料，遷移中直接重建
- [x] 3.8 `user_persona_links` 新增歸屬欄位：所屬客戶、歸屬代碼、歸屬時間；`tenants` 新增唯一的專屬代碼
      `tenant_id`／`attribution_ref`（上限 2,083 字元，依平台規格）／`attributed_at`；`tenants.ref_code`（唯一、必填）
- [x] 3.9 移除 `personas.tenant_id`（共用角色屬於平台，不屬於客戶）；`personas.card` 保留供資料庫建立的共用角色使用
- [x] 3.10 建立遷移 0003（含降級），同步 `scripts/migrate_sqlite_to_postgres.py` 的 TABLE_ORDER；測試比照 3.5（SQLite 結構、Postgres 結構一致與升降級、在既有資料上套用）
      2026-10-13：`alembic/versions/0003_user_persona_versions_and_attribution.py`。SQLite 288 passed
      （`tests/test_tenancy_schema.py` 15 項）；Postgres 12 passed，含結構與 models 完全一致、0003 可降級
      再升級、在已有 POC 使用者與連結的資料庫上套用不影響既有資料。尚未套用至正式資料庫

## 4. 人設卡來源與驗證時機

- [x] 4.1 `app/personas/loader.py` 擴充為檔案與資料庫雙來源，兩者共用同一套 `PersonaCard` 驗證：檔案為共用角色的平台預設版本，資料庫為使用者版本（名字與 IG 顯示名稱由共用角色補入後再驗證）
      新增 `app/personas/user_versions.py`（`merge_user_card`／`save_user_card`／`resolve_card`），
      loader.py 僅將錯誤格式化函式改為公開供兩邊共用。合併規則：名字、IG 顯示名稱、slug、
      safety_note 取共用角色；其餘取使用者版本。回覆生成（main.py）與訊號偵測（signal_job.py）
      皆改為經 `resolve_card` 選卡
- [x] 4.2 實作寫入時驗證：內容無效即拒絕寫入並指出具體欄位位置，原生效版本不變
      `save_user_card`：先驗證再寫入。額外拒絕：修改名字或 IG 顯示名稱、含有 safety_note／slug 等
      非使用者段落的頂層欄位。存入資料庫的內容不含名字，共用角色改名時不需連動。
      HTTP 設定頁屬第 6 部分，此處只做驗證與保存函式
- [x] 4.3 回覆時依使用者是否有版本選擇人設；僅上線狀態的共用角色納入服務
      每次回覆都查資料庫，不快取，使保存後下一則訊息起生效。路由層
      （`get_persona_by_ig_account`）本來就只認上線狀態的共用角色，補測試釘住
- [x] 4.4 移除「資料庫來源的卡無效即啟動失敗」的行為：資料庫中的使用者版本若無法通過驗證，改套用平台預設版本並記錄；檔案來源維持原本的啟動失敗行為
      使用者版本壞掉 → 記錄 error 並套用平台預設版本；YAML 檔行為不變（`load_all_cards` 仍整批失敗）
- [x] 4.5 測試：無效的使用者版本不影響其他使用者、平台預設版本與服務啟動
      2026-10-14：`tests/test_user_versions.py` 26 項（單元 21、整合 5，整合走完整 webhook 管線）。
      做過突變檢查：把 `resolve_card` 改成永遠回傳預設版本，6 項測試如預期失敗，已還原。
      全套 pytest 見下方

## 5. 認證與授權

- [x] 5.1 使用者設定頁的存取：私訊關鍵字「設定」→ 回覆綁定「使用者 × 共用角色」、有時效的連結；此訊息不觸發一般角色回覆。新對話中的連結回覆須帶 AI 揭露；「設定」與連結回覆不進入對話上下文、話題萃取、訊號偵測與插話判定（design.md 決策 14）
      2026-10-14：`app/auth/settings_link.py`（HMAC-SHA256 簽章、無狀態、預設 30 分鐘有效）；
      連結 `<PUBLIC_BASE_URL>/settings?t=<token>`，頁面本身屬 6.1。兩則訊息以 `meta.system` 標記，
      `recent_messages`／`previous_sessions_messages`／`has_newer_inbound` 排除之。
      `SETTINGS_LINK_SECRET` 或 `PUBLIC_BASE_URL` 未設定時功能關閉、「設定」當一般訊息處理，啟動時記警告。
      測試 `tests/test_settings_link.py` 29 項；突變檢查（取消系統訊息過濾）3 項如預期失敗。
      測試抓到並修正：連結含非 ASCII 字元時驗證拋出未處理例外，改為回報「無效」
- [x] 5.2 客戶帳號與登入（僅供統計頁），帳號由管理員開通，不提供自助註冊。帳號密碼登入，密碼以 scrypt 雜湊，登入狀態以伺服器端 session（cookie 僅存隨機代碼）維持，登出與重設密碼即失效
      2026-10-14：`app/auth/accounts.py`、`app/web/backoffice.py`（/login、/logout）；遷移 0004 新增 `accounts`、
      `login_sessions`。cookie 為 HttpOnly、SameSite=Lax（擋跨站表單偽造）、對外網址為 https 時加 Secure；
      資料庫只存登入代碼的 SHA-256。帳號不存在與密碼錯誤花費相同時間、顯示相同訊息。
      同一帳號 15 分鐘內失敗 5 次即暫時拒絕登入（行程記憶體計數，單一 worker）。
      頁面以伺服器端產生 HTML（Jinja2），新增套件 `jinja2`、`python-multipart`
- [x] 5.3 管理員角色與權限判定：管理員與客戶共用同一套帳號與登入，以角色區分；非管理員存取管理員頁面一律回 404，不透露頁面存在
      客戶帳號必屬於一個客戶、管理員不屬於任何客戶，由資料庫 CHECK 約束保證。未登入存取 /admin 也回 404；
      管理員進 /client 回 404。另關閉 FastAPI 自動產生的 /docs、/redoc、/openapi.json——它們會列出所有路徑
- [x] 5.4 測試：連結逾時失效、竄改連結無法指向他人、客戶無法讀取對話與個人設定、未登入存取被拒且不洩漏資源是否存在
      連結部分見 5.1；帳號與存取規則 `tests/test_backoffice.py` 55 項（含 Postgres 上的 CHECK 約束 1 項）。
      突變檢查：把管理員頁面的權限判定改成「登入即可」，測試如預期失敗，已還原。
      「客戶無法讀取對話與個人設定」：目前沒有任何路徑提供這兩者給客戶，待端對端驗證（10.3）再測
- [x] 5.5 讓新使用者知道可以傳「設定」：AI 揭露訊息附上提示「想調整我的個性，隨時傳「設定」給我就好」（2026-10-14 確認，design.md 決策 14）。設定頁功能未啟用時不附
      `compose_reply` 新增 `hint` 參數；提示只隨揭露出現，連結回覆那則不附。測試 4 項（見 test_settings_link.py）
- [x] 5.6 管理員頁面：開通客戶（客戶名稱、專屬代碼、登入帳號，系統產生初始密碼並只顯示一次）、重設客戶密碼、新增其他管理員
      /admin。專屬代碼依平台限制只允許英數與 - _ =；名稱、代碼、帳號重複時顯示原因且不寫入任何資料。
      顯示密碼的頁面加 `Cache-Control: no-store`。頁面樣式為暫用，UI 另行設計（決策 8）
- [x] 5.7 第一個管理員以一次性指令建立（`scripts/create_admin.py`）：系統剛上線時沒有任何帳號，無法從頁面建立；之後的管理員與客戶帳號一律從管理員頁面建立
      已有管理員時拒絕執行。用法：`docker compose exec app python scripts/create_admin.py <登入帳號>`
      全套 pytest：SQLite 401 passed、Postgres 13 passed（2026-10-14）
- [x] 5.8 設定連結只能在第一個打開它的瀏覽器上使用（design.md 決策 14 補充）：第一次請求登記連結並交給瀏覽器一組隨機代碼（cookie），之後的讀取、保存、恢復預設都要帶；頁面本身不登記，避免被 IG 預覽機器人用掉；設定頁新增「已在別的地方打開過」的提示
      2026-10-20：遷移 0007 新增 `settings_link_claims`（套用前備份 `data/cia-backup-20261020-0926-before-0007.dump`），只存雜湊。
      `tests/test_settings_claim.py` 12 項；突變檢查（拿掉拒絕）4 項如預期失敗；全套 509 passed（含 Postgres）。
      正式環境用兩個獨立瀏覽器驗證：本人可重新整理，另一個瀏覽器看到「已經在別的地方打開過」。來源：使用者真機發現連結轉貼給另一個帳號也能用

## 6. 使用者自助頁面

**設計（2026-10-15）**：impeccable 流程——PRODUCT.md、方向抽選（選定「同學錄」）、設計合約
`.impeccable/surfaces/frontend-src-settings-settingspage-tsx.md`。配色經三輪定案為白底藍字、不做夜間版。
前端為 React + Vite + TypeScript（`frontend/`），Docker 多階段建置後由 FastAPI 提供。
⚠️ 本機 Node 在含中文的專案路徑下執行 vite／tsc 會當掉，開發伺服器與型別檢查改在 node 容器內跑（見 vite.config.ts）

- [x] 6.1 設定頁：顯示所屬共用角色，名字與 IG 顯示名稱唯讀；第一次開啟時帶入平台預設版本內容
      `frontend/src/settings/SettingsPage.tsx`；API `app/web/settings_api.py`（GET／POST／DELETE /api/settings、GET /settings）
- [x] 6.2 人設欄位：身分、背景、個性、語言風格、邊界反應、回覆節奏六組固定段落，標籤與說明為中文；欄位依規格分為唯讀、直接輸入、下拉選單、多項清單、長段落文字，下拉與清單的最後一項為「自訂」。需求依據見 design.md 決策 8、13，不做即時預覽面板
      九頁（六組人設＋三頁教練回饋），頁籤與上一頁／下一頁；跟預設不同的值有螢光筆標記，並可單欄還原
- [x] 6.3 系統底線：「絕不接」恆定包含系統底線項目且不可移除，使用者只能加項目；自訂文字保存前經自動檢查
      底線由 `save_user_card` 強制（任何呼叫端都繞不過）；自動檢查 `app/personas/content_check.py` 為規則比對，
      只查描述角色行為與喜好的欄位（「會變冷淡」「絕不接」不查）。會漏掉換句話說的寫法，M12 安全護欄仍為上線前提
- [x] 6.4 教練設定：偵測規則以「六項固定維度＋所屬規則」兩層呈現，可整批或逐條啟用；自訂規則需填描述、類型、歸屬維度，地雷另填嚴重程度；維度規則全停用時顯示警告
      預設值與驗證 `app/personas/coaching_settings.py`；規則代號與 `signals.SIGNAL_ITEMS` 一致（L1～H4）
- [x] 6.5 教練設定：報告語氣與篇幅的下拉選單（含自訂）；報告段落可逐段停用與新增自訂段落，至少保留一段
- [x] 6.6 保存：驗證完整性、格式與安全性，失敗時指出欄位位置與原因、原版本不變；成功則自下一則訊息起生效
      前後端都驗證；後端錯誤以 {欄位路徑: 訊息} 回傳，前端跳到該頁並以紅筆標示。測試抓到並修正：後端原本允許必填文字空白；
      YAML 多行文字的結尾換行讓「原樣保存」被判定成跟預設不同
- [x] 6.7 恢復預設：刪除使用者版本，改套用平台預設版本
- [x] 6.8 保存成功後顯示「回到聊天」連結（`ig.me/m/<帳號>`），讓使用者存完直接回到私訊（design.md 決策 14）
      帳號名稱取自新設定 `IG_USERNAME`；未設定時連到 Instagram 收件匣。
      測試 `tests/test_settings_api.py` 34 項；全套 435 passed（2026-10-15）。尚未部署、尚未真機驗證
<!-- 2026-10-15：原第 7 部分「客戶統計頁」、第 8 部分「共用角色的建立與平台作業」、
     第 12 部分「使用者歸屬」移出本 change（design.md 決策 16），其餘重新編號：
     原 9→7、10→8、11→9、13→10 -->

## 7. 教練設定的套用

- [x] 7.1 訊號偵測改為依該 session 所套用之使用者版本的規則設定：排除已停用的預設規則、加入自訂規則並標記其歸屬維度；平台預設版本與未設定者沿用系統預設
      2026-10-15：`signals.build_rule_set()` 依設定組出偵測項目：停用的預設規則整條拿掉（含程式判斷的 L10）、
      自訂規則依序給代號 C1、C2…，類型、嚴重程度、維度由設定查表填入，不讓模型自己填。相關代號全被停用的判定要點一併拿掉。
      設定由 `coaching_settings.session_coaching()` 依 session 找使用者版本取得，讀當下的設定、不做快照。
      資料庫裡不合法的自訂規則略過並記警告
- [x] 7.2 測試：沒有使用者版本、或版本未設定教練內容時，訊號偵測行為與本 change 導入前一致
      沒有設定、或設定與預設相同時，訊號與報告兩份提示皆與導入前逐字相同（另以導入前後實際輸出比對確認）。
      `tests/test_coaching_apply.py` 20 項，含整合測試：私訊 → 靜默結束 → 點擊邀請，報告少了取消的段落、偵測提示少了停用的規則。
      突變檢查：拿掉 main.py、signal_job.py 的設定傳遞、或讓停用規則無效，測試皆如預期失敗。全套 455 passed（2026-10-15）
- [x] 7.3 將報告語氣、篇幅與段落設定接入 `app/dialog/coach_report.py`（取代固定的 `REPORT_SECTIONS`），依 session 所套用的使用者版本取用；未設定者沿用現行五段與預設語氣
      2026-10-15：報告段落依設定，模型多寫的段落直接丟掉（真機回報「取消『下次可以試試』仍出現」即此問題）。
      預設段落沿用原本的鍵，自訂段落以標題為鍵，與預設段落的鍵重複時加編號；送達時依內容順序顯示，舊報告照常。
      語氣與篇幅寫進提示；非預設設定的輸出上限由 1024 提高到 2048（篇幅可選到約 800 字）

## 8. 長對話摘要壓縮（自 `add-ig-persona-chat-mvp` 6.6 移入，見 design.md 決策 10）

- [x] 8.1 `sessions` 新增摘要內容與摘要涵蓋範圍欄位，建立 Alembic migration
      2026-10-16：遷移 0005 新增 `sessions.summary`、`sessions.summary_until_message_id`（皆可為 NULL）。套用前備份 `data/cia-backup-20261016-1851-before-0005.dump`
- [x] 8.2 實作摘要產生：超過門檻時保留近期訊息原文，較早部分走 analysis 路徑產生摘要；之後以「既有摘要＋新超出的訊息」增量更新。門檻、保留則數、更新間隔皆為可調設定
      `app/dialog/summary.py`。設定 `SUMMARY_TRIGGER_MESSAGES`（60）、`SUMMARY_KEEP_RECENT`（40）、`SUMMARY_UPDATE_BATCH`（20），
      啟動時檢查保留則數小於門檻、不大於 `DIALOG_CONTEXT_MESSAGE_COUNT`。摘要只記雙方明說的事，角色說過關於自己的事也記，避免前後矛盾。
      涵蓋範圍以訊息 id 標記；寫入時確認範圍沒被別的任務先更新，行程內也避免同一場次同時跑兩次
- [x] 8.3 摘要於回覆送出後的背景執行，失敗時安靜退回僅用近期訊息
      在 `_send_reply` 送出回覆之後執行。用 `generate` 而非 `generate_or_fallback`：降級語句不是摘要，不能寫進去。失敗不寫入，下一則回覆後條件仍成立就自動重試
- [x] 8.4 摘要渲染為 system prompt 的獨立段落，不接在對話輪次中
      `render_system_prompt(conversation_summary=…)` 放在最後一段；已摘要的訊息不再以原文送入。沒有摘要時提示與導入前相同
- [x] 8.5 測試：未達門檻不產生摘要、超過門檻產生摘要、增量更新沿用舊摘要、失敗時退回、提示中確實含有摘要
      `tests/test_session_summary.py` 12 項（含 4 項走 webhook 的整合測試，門檻調小）。突變檢查：不濾掉已摘要的原文、不傳摘要，皆如預期失敗。
      全套 483 passed（含 Postgres 13 項，2026-10-16）
- [x] 8.6 真機驗證：在 IG 上單一 session 聊超過門檻，回頭問對話開頭提過的事，確認角色記得且與事實一致
      2026-10-19：通過。門檻暫時調成 10／6／4 測試，session 49 共 33 則、摘要增量更新多次。開頭說的「貓叫小黑、怕打雷」
      已移出原文窗口，晴晴仍正確回答，摘要內容與對話事實一致（含角色自己說過的事）。測完門檻已改回 60／40／20。
      已知小瑕疵：開場的 AI 揭露文字也被寫進摘要第一行，無害但佔篇幅

## 9. 發訊配速實測（自 `add-ig-persona-chat-mvp` 9.7 移入，見 design.md 決策 11）

- [x] 9.1 發訊錯誤的監測與告警：每次發訊失敗依 Meta 錯誤代碼分類（限速、疑似違規、權杖失效、視窗逾期、其他）寫進 `send_errors` 表；限速、疑似違規、權杖失效另記【告警】log；`scripts/send_health.py` 列出每小時發訊量與各類錯誤
      2026-10-19：遷移 0006（套用前備份 `data/cia-backup-20261019-1642-before-0006.dump`）。存表而不只寫 log：容器重新部署後舊 log 就消失。
      只有確定意義的代碼才歸類（368 疑似違規、190 權杖失效），其餘歸「其他」並保留原文。紀錄寫入失敗不影響發訊。
      `tests/test_send_errors.py` 14 項；突變檢查（拿掉紀錄）如預期失敗；全套 497 passed（含 Postgres）。推播通知（Discord／Telegram）暫不做

<!-- 2026-10-19：原 9.2「實測安全配速」、9.3「定案上限並估算承載量」移除（design.md 決策 11）。
     沒有測試人員，用唯一的正式帳號壓測風險太高；承載量估算原是為了決定「一個客戶要開幾個帳號」，
     決策 16 後已無此需求。改為上線後以 9.1 的監測持續觀察，SEND_RATE_LIMIT_PER_HOUR 維持 200 -->

## 10. 端對端驗證

- [x] 10.1 完整流程實測：使用者私訊傳「設定」→ 開啟連結 → 調整人設與教練設定並保存 → 下一則訊息起角色依新設定回覆 → 教練報告依新設定產生
      2026-10-20：使用者真機確認通過。測試中發現並已處理：換職業時近況未更新、取消的報告段落仍出現、語助詞過多、道歉不回暖、連結預覽圖片缺失
- [x] 10.2 驗證系統底線：嘗試移除底線項目、輸入不當自訂文字，皆被拒絕且原版本不變
      2026-10-20：使用者真機確認通過（底線項目有鎖頭、不當內容被擋且整份設定未保存）。已知限制：關鍵字比對會漏掉 Telegram、email、FB、「號碼」、「私下聯絡」等寫法，留待 M12 安全護欄補強
- [x] 10.3 驗證存取隔離：使用者只能進入自己的設定頁；客戶帳號看不到對話與個人設定
      2026-10-20：使用者以兩個帳號實測確認（A 的連結貼給 B，B 被擋下）。這項是在 5.8 補上「連結只能在第一個打開的瀏覽器使用」之後才成立；客戶帳號看不到對話與個人設定，由 `tests/test_backoffice.py` 驗證（目前沒有任何路徑提供這兩者給客戶）
- [x] 10.4 驗證未設定的使用者仍得到平台預設版本（晴晴），除 2.5 移除「自己的煩惱」外行為未受其他影響
      2026-10-20：使用者以沒有設定過的帳號實際聊天確認通過
- [x] 10.5 全套 pytest 綠燈
      2026-10-20：509 passed（含 Postgres 13 項）。收尾時有一次跑出 1 項失敗、沒抓到是哪一項；之後連續 4 次含 Postgres、6 次不含 Postgres 全數通過，無法重現。疑為時間相關的測試偶發，下次再遇到時要記下測試名稱
