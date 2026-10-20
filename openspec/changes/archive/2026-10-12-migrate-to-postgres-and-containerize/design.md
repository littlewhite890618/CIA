## Context

動機見 proposal.md。這裡只列影響做法的現況與限制：

- **資料層從一開始就為換 Postgres 做了準備**：[models.py](../../../app/db/models.py) 全部使用 SQLAlchemy 泛型型別（`JSON`、`DateTime(timezone=True)`、字串常數取代原生 enum），[session.py](../../../app/db/session.py) 的 SQLite 專屬設定都包在 `is_sqlite` 判斷裡。程式碼中沒有任何原生 SQL 或 SQLite 專屬函式（唯一的 `text()` 在 `_ADDED_COLUMNS` 補欄位）。
- **結構變更目前靠 `_ADDED_COLUMNS`**：`create_all` 只建新表，後加的 5 個欄位靠啟動時比對補上。這是 POC 權宜之計，Alembic 已在 `requirements.txt` 但從未使用。
- **SQLite 讀回的時間沒有時區**：程式在兩處補 UTC（[repository.py:231](../../../app/db/repository.py#L231)、[disclosure.py:46](../../../app/personas/disclosure.py#L46)）。Postgres 的 `TIMESTAMPTZ` 讀回一律帶時區，這兩處補丁會變成不必要但無害。
- **單一行程假設**：速率限制器（[main.py:65](../../../app/main.py#L65)）與靜默輪詢都存在行程記憶體內。
- **人設卡路徑以原始碼位置推算**：[loader.py:18](../../../app/personas/loader.py#L18) 的 `CARDS_DIR` 是 `loader.py` 所在目錄下的 `cards/`。只要容器內的目錄結構和本機一致，就不需要改程式。
- **POC 資料量**：personas 1、users 5、user_persona_links 5、sessions 37、messages 414、realtime_signals 43、coach_reports 5、rate_budget 28、safety_events 0（9/24 快照）。
- **主機**：Windows 10 Pro、Docker 27.3.1（Docker Desktop／WSL2）、本機 Python 3.13.1。專案資料夾路徑含中文字元。
- **測試**：全部以 SQLite 執行（記憶體或暫存檔），不依賴外部服務。

## Goals / Non-Goals

**Goals:**
- 切換後所有既有行為不變，現有測試套件全數通過
- 轉檔可重複演練：在空的 Postgres 上跑幾次都得到相同結果，失敗時不留下半套資料
- 回滾簡單：切換後短時間內出問題，可以停掉容器、改回 SQLite 原樣運作

**Non-Goals:**
- 不把 `JSON` 改為 `JSONB`、不新增索引或調整結構——這次只換資料庫，不改資料模型
- 不移除程式中處理無時區時間的補丁（無害，且測試與本機執行仍用 SQLite）
- 不做連線池調校；單一副本、低流量下 SQLAlchemy 預設值足夠

## Decisions

### 1. 驅動程式用 psycopg 3（`psycopg[binary]`）

連線字串為 `postgresql+psycopg://...`，與 `.env.example` 現有的註解一致。

- **替代方案 psycopg2**：仍可用但已進入維護模式，SQLAlchemy 2.0 與 psycopg 3 的整合是目前主線
- **`[binary]` 版本**：附帶預先編譯好的 libpq，slim 映像檔不必另裝編譯工具與 `libpq-dev`

### 2. Alembic 初始遷移從現有模型自動產生，再人工檢查

用 `alembic revision --autogenerate` 對空資料庫產生第一版，人工確認以下項目都有正確產出：唯一限制（`uq_persona_igsid`、`uq_platform_message_id`、`uq_rate_window`）、複合索引（`ix_session_silence_scan` 等）、外鍵、欄位註解。之後所有結構變更都走新的 revision。

`alembic/env.py` 從 `app.config.get_settings().database_url` 取得連線字串，不在 `alembic.ini` 寫死，讓容器內外都讀同一份設定。

### 3. `init_db()` 依資料庫類型分流

| 資料庫 | `init_db()` 行為 | 用途 |
|---|---|---|
| SQLite | `create_all`（維持現狀） | 測試、不用 Docker 的本機執行 |
| Postgres | 只檢查連線，並確認資料庫已在 Alembic 最新版本；不是最新版就啟動失敗並提示執行 `alembic upgrade head` | 正式環境 |

`_ADDED_COLUMNS` 補欄位機制與對應的測試（[test_repository.py:364](../../../tests/test_repository.py#L364) 起的舊資料庫補欄位測試）一併移除。它要處理的情境是「欄位加入之前就建好的 SQLite 檔」，而唯一這樣的檔案 `poc.db` 在轉檔後就退役了。往後 SQLite 只會是全新建立的檔案，`create_all` 會直接建出完整結構。

- **替代方案：Postgres 啟動時自動執行 `alembic upgrade head`**：會讓應用程式行程同時負責修改資料庫結構。單一副本下技術上可行，但遷移失敗時的錯誤和一般啟動錯誤混在一起，難以判斷。改由容器的啟動指令先跑遷移、成功才啟動服務（見決策 6），責任分得比較清楚

### 4. 測試維持 SQLite，另加一組可選的 Postgres 冒煙測試

現有測試不動，繼續用 SQLite 快速執行。另外新增一組標記為 `postgres` 的測試，只在設定 `TEST_DATABASE_URL` 時執行：對空的 Postgres 套用所有遷移，然後跑一次「收訊息 → 落庫 → 去重 → 結束 session」的流程。這組測試專門抓兩種資料庫行為不同的地方，例如字串長度限制、時區、布林值。

- **替代方案：全部測試改跑 Postgres**：最真實，但每次跑測試都要先起資料庫容器，違反 spec「測試不依賴 PostgreSQL 或 Docker」

### 5. 轉檔腳本以應用程式自己的資料模型讀寫兩端

`scripts/migrate_sqlite_to_postgres.py` 的做法：

1. **前置檢查**：目的資料庫必須已套用最新遷移，且所有業務表都是空的，否則拒絕執行。來源以唯讀模式開啟（`file:...?mode=ro`），保證不會修改 `poc.db`
2. **資料預檢**：逐欄檢查字串長度是否超過欄位定義（SQLite 不檢查 `VARCHAR(n)` 長度，Postgres 會），也檢查文字中是否含 NUL 字元（Postgres 不接受）。發現問題就列出具體的表、id、欄位後停止，不自動截斷
3. **依外鍵順序複製**：personas → users → user_persona_links → sessions → messages → realtime_signals → coach_reports → safety_events → rate_budget。兩端都使用 `Base.metadata` 的表定義，SQLAlchemy 會處理布林值、JSON、時間字串的型別轉換；讀到沒有時區的時間就補上 UTC
4. **單一交易**：全部寫入在同一個交易內完成，任何一筆失敗就整批回復
5. **重設序號**：因為保留原始 id，搬完後要把每張表的序號（`setval`）設為該表最大 id，否則新資料寫入時會從 1 開始，撞上既有的主鍵
6. **校驗**：逐表輸出來源筆數與目的筆數，抽查外鍵關聯是否完整

腳本在主機上用 `.venv` 執行，透過只開放給本機（127.0.0.1）的 15432 埠連進容器內的 Postgres（埠號理由見決策 7）。

- **替代方案：`pgloader`**：專門做 SQLite → Postgres 的工具，但它會依 SQLite 的型別推斷建表，和 Alembic 建出的結構會衝突，時區與布林值的轉換也要另外設定。資料量只有幾百筆，自己寫比較容易控制
- **替代方案：在應用程式容器內執行**：要另外掛載 `poc.db`，徒增設定。在主機上執行可以直接沿用 `.venv`

### 6. 映像檔：`python:3.13-slim`，單一 worker，不用 shell 腳本當入口

- 基底映像版本對齊本機的 Python 3.13，避免「本機能跑、容器不能跑」
- 以非 root 使用者執行
- `WORKDIR /app`，程式碼複製到 `/app` 下，與專案根目錄的結構相同（如 `/app/app/main.py`、`/app/alembic/`）。容器內路徑和本機一一對應，決策 8 與決策 10 的掛載都依賴這一點
- 啟動指令直接寫在 Dockerfile：`sh -c "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"`。**不另寫 `entrypoint.sh`**：Windows 上編輯的 shell 檔容易被存成 CRLF 換行，在 Linux 容器內會出現「找不到檔案」這種難以理解的錯誤
- 正式模式不使用 `--reload`（開發模式另見決策 10）；`--workers 1` 明確寫出來，避免日後有人改成多個 worker，破壞單一行程的假設
- `.dockerignore` 排除 `.env`、`data/`、`.venv/`、`.pytest_cache/`、`__pycache__/`、`openspec/`、`docs/`

### 7. Compose 組成

| 服務 | 映像檔 | 重點設定 |
|---|---|---|
| `db` | `postgres:16` | named volume `pgdata`；健康檢查 `pg_isready`；埠只綁 `127.0.0.1:15432`（供轉檔腳本與本機除錯，不對外；可用 `POSTGRES_HOST_PORT` 調整） |
| `app` | 本專案 Dockerfile | `depends_on: db (service_healthy)`；`restart: unless-stopped`；`env_file: .env`，並在 `environment` 以 `db` 主機名覆寫 `DATABASE_URL`；人設卡目錄唯讀掛載到 `/app/app/personas/cards`（見決策 8）；埠綁 `127.0.0.1:8000` |
| `ngrok` | `ngrok/ngrok` | 將流量轉到 `app:8000`，使用既有的固定網域；`NGROK_AUTHTOKEN` 與網域從 `.env` 讀取；檢視介面綁 `127.0.0.1:4040`，保留 Replay 重送請求的除錯方式 |

主機對外埠用 15432 而非預設的 5432：這台桌機已有原生安裝的 PostgreSQL 13／16 服務佔用 5432、5433（2026-10-02 實作時發現），沿用 5432 不只會啟動失敗，主機上的連線字串也可能誤連到別的資料庫。`.env` 裡的 `DATABASE_URL` 指向 `127.0.0.1:15432`（不用 `localhost`：Windows 上會先試 IPv6，每次連線多卡數秒），給主機上直接執行用；容器內則由 compose 的 `environment` 覆寫成 `db`。pydantic-settings 中環境變數的優先順序高於 `.env` 檔，所以兩邊共用同一份 `.env` 不會衝突。

- **替代方案：用 Windows bind mount 存 Postgres 資料**：跨 WSL2 與 Windows 檔案系統的掛載有權限與效能問題，Postgres 官方映像在這種掛載下常因權限問題無法初始化。spec 已明定使用 named volume
- **替代方案：`ngrok` 繼續在主機上跑**：使用者已決定放進 compose

### 8. 人設卡掛載到容器內原本的路徑

compose 把主機的 `./app/personas/cards` 唯讀掛載到容器內的 `/app/app/personas/cards`，覆蓋映像檔裡的那一份。因為容器內路徑與本機一致（決策 6），`loader.py` 的 `CARDS_DIR` 不需修改，也不新增任何設定。兩種模式都使用這個掛載，因此正式模式下改人設卡也只要重啟 `app`，不必重建映像檔。

- **替代方案：新增 `PERSONA_CARDS_DIR` 設定，掛到獨立路徑（如 `/persona-cards`）**：能把「程式碼」和「掛載進來的資料」分在不同目錄，但要多改程式、多一個設定。開發模式本來就會把整個 `app/` 掛進容器，分開放的好處在這個專案裡不明顯，因此不採用

### 9. `/health` 實際查詢資料庫

目前 `/health` 只回報設定值，不碰資料庫。改為執行 `SELECT 1`，失敗時回 503。compose 的 `app` 健康檢查使用這個端點；slim 映像沒有 curl，改用 `python -c` 搭配 `urllib` 發送請求。

### 10. 開發模式：另一個 compose 檔，以 `-f` 明確疊加

新增 `docker-compose.dev.yml`，只覆寫 `app` 服務，啟動方式為：

```
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

覆寫內容：

| 項目 | 設定 | 理由 |
|---|---|---|
| 掛載 | `./app`、`./alembic`、`./alembic.ini`、`./scripts` 分別掛到 `/app/` 下的同名路徑 | 本機修改直接反映到容器內原本的路徑 |
| 啟動指令 | 仍先執行 `alembic upgrade head`，再以 `uvicorn ... --reload --reload-dir app` 啟動 | 新增的 Alembic revision 在重啟時會套用；只監看 `app/`，避免改文件也觸發重載 |
| `WATCHFILES_FORCE_POLLING=true` | 改用定時檢查檔案變動 | 從 Windows 掛進 WSL2 容器時，檔案變動的通知傳不進容器，不開輪詢的話 `--reload` 偵測不到修改 |
| `PYTHONDONTWRITEBYTECODE=1` | 不產生 `.pyc` | 避免容器在本機的 `app/` 底下寫入 `__pycache__` |

`requirements.txt` 不掛載：改了套件仍要重建映像檔，因為套件是安裝在映像檔裡的。

`--reload` 下 uvicorn 由一個監看行程加上一個工作行程組成，實際處理 webhook 與執行靜默輪詢的仍只有一個行程，不違反單一副本的要求。

- **替代方案：`docker-compose.override.yml`**：Docker 會自動載入這個檔名，不用加 `-f`。但這台桌機同時對外服務，自動載入等於讓開發模式成為預設，違反 spec「預設為正式模式」
- **替代方案：一律掛載並自動重載**：最簡單，但對外開放後，每次存檔都會立刻影響真實使用者；存到一半的檔案也會讓服務中斷
- **替代方案：一律掛載但不自動重載**：可以控制生效時機，但正式模式就會使用本機目錄中尚未完成的修改，正式環境實際跑的是哪一版程式碼不再明確
## Risks / Trade-offs

- **[ngrok 免費版同一時間只允許一個 agent 連線]** → 主機上原本執行的 ngrok 必須先關掉，否則容器內的 ngrok 會連線失敗。寫進操作文件，並在切換步驟中列為第一步
- **[ngrok 新版 CLI 改用 `--url`，舊版用 `--domain`]** → 實作時以映像檔實際的版本為準，並同步更新 [開發環境設定.md](../../../docs/開發環境設定.md)
- **[SQLite 與 Postgres 行為不同的地方只在正式環境才會出現]** → 決策 4 的 Postgres 冒煙測試、決策 5 的資料預檢，加上切換後的真機驗證
- **[Docker Desktop 只有使用者登入 Windows 後才會啟動]** → 本 change 不處理（明確不在範圍），`restart: unless-stopped` 只負責 Docker 執行期間的自動重啟。在後續處理重開機自動恢復的 change 之前，桌機重開後必須有人登入
- **[轉檔期間停機]** → 資料量只有幾百筆，轉檔只需幾秒；但停 ngrok → 起 compose → 轉檔 → 驗證，整段預估 15–30 分鐘，這段時間收不到 IG 私訊。目前沒有真實使用者，影響可接受
- **[Postgres 資料只存在 Docker volume 內，沒有備份]** → 本 change 不處理備份。在備份機制完成之前，`poc.db` 不刪除，作為轉檔時間點的資料副本
- **[專案路徑含中文字元]** → Docker Desktop 的 build context 與掛載路徑在 Windows 上處理非 ASCII 字元的情況需要實測，列入任務中第一個驗證的項目
- **[對外開放後，在開發模式下存檔會直接影響真實使用者]** → 文件寫明：對外開放後，平時以正式模式運行，開發模式只在維護時段使用；並提供確認目前模式的方法（`docker compose ps` 搭配查看 `app` 的啟動指令是否含 `--reload`）
- **[輪詢偵測檔案變動會多用一點 CPU]** → 只監看 `app/`，且只在開發模式啟用，正式模式不受影響

## Migration Plan

**事前演練**（不停機）：
1. 在主機上起 compose 的 `db`，套用遷移，用 `poc.db` 的複本跑轉檔腳本，確認校驗通過
2. 執行 `docker compose down -v` 清空演練資料

**正式切換**：
1. 停止主機上的 uvicorn 與 ngrok
2. `docker compose up -d db`，等健康檢查通過
3. `alembic upgrade head`
4. 執行轉檔腳本，確認逐表筆數一致
5. `docker compose up -d`，確認 `/health` 回報 postgres 且連線正常
6. 用真實 IG 帳號傳一則私訊給晴晴，確認收到回覆，且回覆內容參考了跨場次的話題記憶（證明搬過去的歷史資料有被讀到）
7. 將 `.env` 的 `DATABASE_URL` 改為指向 Postgres（給之後在主機上直接執行時使用）

**回滾**（切換後發現問題時）：
1. `docker compose down`（不加 `-v`，保留 Postgres 資料供事後排查）
2. `.env` 的 `DATABASE_URL` 改回 `sqlite:///./data/poc.db`
3. 依原方式啟動 uvicorn 與 ngrok

這套回滾依賴 `poc.db` 在轉檔時沒有被修改（spec 明定以唯讀模式開啟）。限制是：切換後寫進 Postgres 的新對話不會出現在回滾後的 SQLite 中。

## Open Questions

- **桌機上的 `ENVIRONMENT` 要設成 `development` 還是 `production`**：這個值會決定 log 格式，以及 `WEBHOOK_SKIP_SIGNATURE_CHECK` 這個除錯開關是否生效。對外開放前必須是 `production`，但切換當下沿用 `development` 也不影響這次的範圍，可以在對外開放前再決定
