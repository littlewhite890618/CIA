<!-- 範圍：SQLite → PostgreSQL 16、Docker Compose 容器化（app + db + ngrok）。
     重開機自動恢復、資料庫備份不在範圍，之後另開 change。
     做法與理由見 design.md，決策編號對照標在各任務後面。 -->

## 1. 環境前置驗證

- [x] 1.1 確認 Docker Desktop 能處理含中文字元的專案路徑：用最小的 Dockerfile 建置一次，並掛載專案內的目錄進容器讀檔（design 風險：專案路徑含中文字元）
      2026-10-02：以專案根目錄為 build context `COPY app/personas/cards/` 成功，
      bind mount 同目錄進 alpine 容器可列出並讀取 `sunny.yaml`。無問題。
- [x] 1.2 確認 `ngrok/ngrok` 映像檔的版本與固定網域參數（`--url` 或 `--domain`）
      2026-10-02：映像檔 3.39.8（主機為 3.39.9），`http --help` 只列 `--url`。
      compose 使用 `ngrok/ngrok:3` 標籤，固定網域參數用 `--url`。

## 2. 相依套件與設定

- [x] 2.1 `requirements.txt` 新增 `psycopg[binary]`（決策 1）
- [x] 2.2 `.env.example` 新增 `POSTGRES_USER`／`POSTGRES_PASSWORD`／`POSTGRES_DB`、`NGROK_AUTHTOKEN`、`NGROK_DOMAIN`，`DATABASE_URL` 範例改為 `postgresql+psycopg://...@127.0.0.1:15432/...`，SQLite 寫法保留為註解
      2026-10-02：主機用 127.0.0.1 而非 localhost（Windows 上 localhost 先試 IPv6，每次連線卡 5 秒）；
      埠改 15432（桌機原生 PostgreSQL 13／16 已佔用 5432、5433），可用 `POSTGRES_HOST_PORT` 調整。

## 3. Alembic 結構遷移

- [x] 3.1 初始化 Alembic（`alembic.ini`、`alembic/`），`env.py` 從 `get_settings().database_url` 取得連線字串，`target_metadata` 指向 `Base.metadata`（決策 2）
- [x] 3.2 對空的 Postgres 以 autogenerate 產生初始 revision，人工確認唯一限制、複合索引、外鍵、欄位註解齊全
- [x] 3.3 驗證：空資料庫 `alembic upgrade head` 成功；再執行一次不做任何變更；`alembic check` 回報模型與資料庫一致

## 4. 資料庫連線層

- [x] 4.1 `app/db/session.py` 的 `init_db()` 依資料庫類型分流：SQLite 維持 `create_all`；Postgres 只檢查連線並確認已在 Alembic 最新版本，連不上或版本落後時啟動失敗，並給出明確的錯誤訊息（決策 3）
- [x] 4.2 移除 `_ADDED_COLUMNS`、`_add_missing_columns()` 與 `tests/test_repository.py` 中對應的補欄位測試（決策 3）
- [x] 4.3 `/health` 改為實際執行 `SELECT 1`，失敗時回 503，並回報資料庫類型（決策 9）
- [x] 4.4 執行現有測試套件，確認全數通過（仍跑 SQLite）

## 5. Postgres 冒煙測試

- [x] 5.1 新增 `postgres` 測試標記，只在設定 `TEST_DATABASE_URL` 時執行，否則略過；`pytest.ini` 登記這個標記（決策 4）
- [x] 5.2 撰寫冒煙測試：對空的 Postgres 套用遷移，接著收訊息 → 落庫 → 重送同一則被去重 → 靜默結束 session，並驗證讀回的時間帶有 UTC 時區

## 6. 轉檔腳本

- [x] 6.1 `scripts/migrate_sqlite_to_postgres.py` 前置檢查：目的資料庫須在最新遷移版本，且所有業務表為空，否則拒絕執行；來源以 `mode=ro` 唯讀開啟（決策 5）
- [x] 6.2 資料預檢：字串超出欄位長度、文字含 NUL 字元時，列出表／id／欄位後停止
- [x] 6.3 依外鍵順序複製全部 9 張表，保留原始 id，無時區的時間補上 UTC，全部在單一交易內完成
- [x] 6.4 複製後對每張表重設序號為最大 id
- [x] 6.5 逐表輸出來源與目的筆數比對，並抽查外鍵關聯（訊息→對話、訊號→訊息、報告→對話）
- [x] 6.6 驗證失敗情境：目的資料庫已有資料時拒絕執行；中途注入錯誤後目的資料庫維持空的；執行前後 `poc.db` 的雜湊值不變
- [x] 6.7 驗證轉檔後新增一筆使用者、對話與訊息，取得的 id 不與既有資料衝突

## 7. 容器化

- [x] 7.1 撰寫 `Dockerfile`：`python:3.13-slim`、非 root 使用者、`WORKDIR /app` 且目錄結構與專案根目錄相同，啟動指令為 `alembic upgrade head && exec uvicorn ... --workers 1`，不使用 shell 腳本當入口、不使用 `--reload`（決策 6）
- [x] 7.2 撰寫 `.dockerignore`，排除 `.env`、`data/`、`.venv/`、`.pytest_cache/`、`__pycache__/`、`openspec/`、`docs/`
- [x] 7.3 撰寫 `docker-compose.yml`（決策 7）：
      `db`：`postgres:16`、named volume、`pg_isready` 健康檢查、`127.0.0.1:15432`
      `app`：等 `db` 健康檢查通過才啟動、`restart: unless-stopped`、`env_file: .env` 並覆寫 `DATABASE_URL`、人設卡唯讀掛載至容器內原本的路徑 `/app/app/personas/cards`（決策 8）、`/health` 健康檢查、`127.0.0.1:8000`
      `ngrok`：轉發到 `app:8000`、固定網域、`127.0.0.1:4040`
- [x] 7.4 驗證映像檔內不含 `.env`、`data/` 或任何祕密值
- [x] 7.5 驗證：`docker compose up -d` 後三個服務都健康；手動結束 `app` 行程後會自動重啟；`down` 後再 `up`，資料仍在；修改人設卡後重啟 `app` 即生效，不需重建映像檔；人設卡缺欄位時 `app` 啟動失敗並指出缺漏
- [x] 7.6 撰寫 `docker-compose.dev.yml`，只覆寫 `app`：掛載 `./app`、`./alembic`、`./alembic.ini`、`./scripts` 到 `/app/` 下的同名路徑；啟動指令先跑 `alembic upgrade head`，再以 `uvicorn --reload --reload-dir app` 啟動；設定 `WATCHFILES_FORCE_POLLING=true`、`PYTHONDONTWRITEBYTECODE=1`（決策 10）
- [x] 7.7 驗證開發模式：存檔後服務自動重載，不需重建映像檔；存入語法錯誤後重載失敗，修正後再存檔即恢復；本機的 `app/` 下沒有產生新的 `__pycache__`；新增的 Alembic revision 在重啟 `app` 後套用
- [x] 7.8 驗證模式區隔：只執行 `docker compose up -d`（不加 `-f`）時為正式模式，修改本機程式碼後服務行為不變

## 8. 文件

- [x] 8.1 [開發環境設定.md](../../../docs/開發環境設定.md) 新增「Docker 啟動方式」：首次啟動、常用指令、ngrok 檢視介面、主機與容器內 ngrok 不可同時執行的提醒；保留原本不用 Docker 的執行方式
- [x] 8.2 同一份文件新增「開發模式」：啟動指令、何時該用（對外開放後僅限維護時段）、如何確認目前跑的是哪種模式、改了 `requirements.txt` 要重建映像檔
- [x] 8.3 撰寫切換與回滾操作步驟（依 design.md「Migration Plan」），放在 [開發環境設定.md](../../../docs/開發環境設定.md) 或獨立文件

## 9. 正式切換

- [x] 9.1 事前演練：用 `poc.db` 的複本在空的 Postgres 上完整轉檔一次，校驗通過後執行 `docker compose down -v` 清空
      2026-10-02：在獨立的 postgres:16 容器（非 compose，因 `.env` 尚未填 `POSTGRES_*`）上以真實
      `poc.db` 複本演練：9 張表筆數一致（sessions 37、messages 414、realtime_signals 43…），
      6 組外鍵關聯一致，來源雜湊不變；重跑時正確拒絕。compose 本身的啟動另於 7.5 驗證。
      使用者正式切換前可再依 docs/切換到PostgreSQL.md 第 1 階段用 compose 演練一次。
- [x] 9.2 正式切換：停止主機上的 uvicorn 與 ngrok → 起 `db` → 套用遷移 → 轉檔 → 確認筆數一致 → `docker compose up -d`
- [x] 9.3 真機驗證：以真實 IG 帳號私訊晴晴並收到回覆，且回覆內容參考了跨場次的話題記憶
      2026-10-02：「嗨」→ 晴晴回覆並附 AI 揭露（新訊息 id 415，序號接續無衝突）；
      問「你還記得我什麼星座嗎？」→ 答「雙子座」，出自搬移過來的第 36 場對話與話題記憶。
- [x] 9.4 `.env` 的 `DATABASE_URL` 改為指向 Postgres；`poc.db` 保留不刪，作為轉檔時間點的資料副本
- [x] 9.5 更新 [PROGRESS.md](../../../docs/PROGRESS.md)，並將 `add-ig-persona-chat-mvp` task 1.2 標註為由本 change 完成
