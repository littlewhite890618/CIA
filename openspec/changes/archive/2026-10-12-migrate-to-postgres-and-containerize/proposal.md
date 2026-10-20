## Why

系統未來要對外開放給真實使用者，並確定部署在目前這台 Windows 10 Pro 桌機上。現況是「SQLite 檔案 + 手動開兩個終端機分別跑 uvicorn 與 ngrok」，這套做法適合 POC 驗證，但不適合長期對外服務：SQLite 不是設計上的正式資料庫（[技術可行性與架構規劃.md §3.1](../../../docs/技術可行性與架構規劃.md) 早已定為 Postgres 16），資料表結構靠 `_ADDED_COLUMNS` 手動補欄位，三個元件也要分別啟動。

POC 階段刻意延後的 `add-ig-persona-chat-mvp` task 1.2（Docker Compose 起 Postgres）在此一併完成。趁資料量還小（目前 37 場對話、414 則訊息）時遷移，成本最低。

## What Changes

- **資料庫從 SQLite 換成 PostgreSQL 16**：加入 `psycopg` 驅動，`DATABASE_URL` 改指向 Postgres
- **資料表結構改由 Alembic 管理**：建立初始遷移（對應現有 9 張表的完整結構），服務啟動前執行 `alembic upgrade head`；`init_db()` 的 `create_all` 與 `_ADDED_COLUMNS` 補欄位機制退役，僅保留給測試環境
- **一次性轉檔腳本**：把 `data/poc.db` 既有資料（使用者、歸戶、對話、訊號、教練報告、速率紀錄）完整搬進 Postgres，保留原始 id 與關聯，搬完後校驗筆數
- **以 Docker Compose 容器化**：三個服務——應用程式、Postgres、ngrok——一個指令全部啟動
  - 應用程式固定單一副本
  - Postgres 資料存在 Docker named volume
  - 人設卡目錄掛載到容器內原本的路徑，改人設不必重建映像檔
  - 祕密沿用 `.env`，不進映像檔
- **開發模式**：另一個 `docker-compose.dev.yml`，以 `-f` 明確疊加才會啟用。把本機程式碼掛載到容器內原本的路徑，存檔後服務自動重載。預設的 `docker compose up` 為正式模式，使用映像檔內的程式碼，本機修改不影響運行中的服務
- **保留本機直接執行的開發方式**：不用 Docker 也能照 [開發環境設定.md](../../../docs/開發環境設定.md) 在本機跑；測試仍可用 SQLite 快速執行

**不在範圍**（之後另開 change）：桌機重開機後自動恢復、資料庫備份與還原、改用 Cloudflare Tunnel、多副本或水平擴展。

## Capabilities

### New Capabilities
- `data-store`：正式資料庫為 PostgreSQL、資料表結構由版本化遷移管理、POC 既有資料可完整搬移且可校驗
- `container-runtime`：以 Docker Compose 在單一主機上啟動完整系統（應用程式、資料庫、對外通道），含單一副本限制、人設卡外掛、設定與祕密的注入方式，以及開發模式與正式模式的區隔

### Modified Capabilities
（無。`openspec/specs/` 尚無已歸檔的主規格；既有 change 的對話、發訊、訊號、報告等行為在遷移前後必須完全一致，不修改任何既有需求。）

## Impact

- **程式碼**：`app/db/session.py`（連線設定、`init_db` 角色改變）、`app/config.py`（`database_url` 預設值與 Postgres 判斷）、`app/main.py`（`/health` 回報資料庫狀態）
- **新增檔案**：`Dockerfile`、`docker-compose.yml`、`docker-compose.dev.yml`、`.dockerignore`、`alembic.ini`、`alembic/`（遷移腳本）、`scripts/migrate_sqlite_to_postgres.py`
- **相依套件**：新增 `psycopg[binary]`；`alembic` 已在 `requirements.txt` 但尚未使用
- **設定**：`.env.example` 新增 Postgres 帳密與 ngrok 相關變數；`DATABASE_URL` 範例改為 Postgres
- **文件**：[開發環境設定.md](../../../docs/開發環境設定.md) 新增 Docker 啟動方式（正式模式與開發模式）
- **外部系統**：桌機需安裝並執行 Docker Desktop（WSL2，已確認安裝 27.3.1）；ngrok authtoken 與固定網域沿用既有的
- **停機**：轉檔時需停止服務，停機期間的 IG 私訊無法接收
