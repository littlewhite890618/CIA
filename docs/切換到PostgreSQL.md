# 切換到 PostgreSQL（一次性操作）

更新日：2026-10-02　｜　對應 change：`migrate-to-postgres-and-containerize`

把服務從「SQLite + 主機上手動跑 uvicorn 與 ngrok」切換成「Docker Compose + PostgreSQL」，
並把 `data/poc.db` 的既有資料完整搬過去。只需要做一次。

| 階段 | 停機 | 預估時間 |
|---|---|---|
| 0. 準備 `.env` | 否 | 5 分鐘 |
| 1. 事前演練 | 否 | 10 分鐘 |
| 2. 正式切換 | **是**，期間收不到 IG 私訊 | 15–30 分鐘 |
| 3. 真機驗證 | 否 | 5 分鐘 |

以下指令都在專案根目錄、用 PowerShell 執行。

---

## 0. 準備 `.env`

在 `.env` 加上以下幾行（`.env.example` 有說明）：

```
POSTGRES_USER=cia
POSTGRES_PASSWORD=一組只含英數字的隨機密碼
POSTGRES_DB=cia
POSTGRES_HOST_PORT=15432
NGROK_AUTHTOKEN=你的 ngrok authtoken
NGROK_DOMAIN=你現在用的固定網域（不含 https://）
```

- **authtoken 在哪裡**：執行 `ngrok config check` 找到設定檔，裡面的 `authtoken` 就是
- **`DATABASE_URL` 先不要改**，維持 SQLite，第 2 階段最後才改

---

## 1. 事前演練（不停機）

用 `poc.db` 的複本完整跑一次轉檔，確認沒問題再正式切換。

1. 起資料庫：
   ```powershell
   docker compose up -d db
   ```
2. 複製一份 `poc.db`（連同 `-wal`、`-shm`）：
   ```powershell
   foreach ($s in '', '-wal', '-shm') { if (Test-Path "data\poc.db$s") { Copy-Item "data\poc.db$s" "data\rehearsal.db$s" -Force } }
   ```
3. 套用資料表結構（把下面的密碼換成你 `.env` 裡的）：
   ```powershell
   $env:DATABASE_URL = "postgresql+psycopg://cia:你的密碼@127.0.0.1:15432/cia"
   .venv\Scripts\alembic.exe upgrade head
   ```
4. 轉檔：
   ```powershell
   .venv\Scripts\python.exe scripts\migrate_sqlite_to_postgres.py --source data\rehearsal.db --target $env:DATABASE_URL
   ```
   最後一行要是「轉檔完成」，表格每一列的「來源」「目的」兩欄數字要一樣。
5. 清空演練資料：
   ```powershell
   docker compose down -v
   Remove-Item data\rehearsal.db*
   ```

演練失敗時，錯誤訊息會指出哪張表、哪一筆、哪個欄位。目的資料庫不會留下半套資料。

---

## 2. 正式切換（會停機）

1. **停掉主機上的 uvicorn 和 ngrok**（在各自的終端機按 Ctrl+C）。
   兩者都要停：uvicorn 不停，`poc.db` 還會繼續被寫入；ngrok 不停，容器裡的 ngrok 會連線失敗
2. 起資料庫，並套用資料表結構：
   ```powershell
   docker compose up -d db
   $env:DATABASE_URL = "postgresql+psycopg://cia:你的密碼@127.0.0.1:15432/cia"
   .venv\Scripts\alembic.exe upgrade head
   ```
3. 轉檔（這次用真正的 `poc.db`，腳本以唯讀模式開啟，不會改動它）：
   ```powershell
   .venv\Scripts\python.exe scripts\migrate_sqlite_to_postgres.py --source data\poc.db --target $env:DATABASE_URL
   ```
   確認每張表的筆數一致、最後一行是「轉檔完成」
4. 啟動全部服務：
   ```powershell
   docker compose up -d --build
   docker compose ps
   ```
   `db`、`app` 要顯示 healthy，`ngrok` 要顯示 running
5. 確認服務接上的是 Postgres：
   ```powershell
   (Invoke-WebRequest http://127.0.0.1:8000/health -UseBasicParsing).Content
   ```
   要看到 `"database":"postgres"` 和 `"database_ok":true`
6. 把 `.env` 的 `DATABASE_URL` 改成指向 Postgres（之後在主機上跑 alembic、腳本時用）：
   ```
   DATABASE_URL=postgresql+psycopg://cia:你的密碼@127.0.0.1:15432/cia
   ```
   主機名用 `127.0.0.1`，不要用 `localhost`（Windows 上每次連線會多卡 5 秒）

`data/poc.db` **不要刪**，它是切換當下的資料副本，也是回滾的依據。

---

## 3. 真機驗證

1. 用你的 IG 帳號私訊晴晴，確認收到回覆
2. 打開 <http://127.0.0.1:4040>，確認看得到 Meta 送進來的請求
3. 確認回覆有參考到以前聊過的話題——這代表搬過去的歷史資料有被讀到

---

## 回滾（切換後發現問題時）

1. 停掉容器，**不要加 `-v`**（保留 Postgres 資料供事後查原因）：
   ```powershell
   docker compose down
   ```
2. 把 `.env` 的 `DATABASE_URL` 改回：
   ```
   DATABASE_URL=sqlite:///./data/poc.db
   ```
3. 照原本的方式在主機上啟動 ngrok 與 uvicorn（見 [開發環境設定.md](開發環境設定.md) 的「不用 Docker」流程）

限制：切換後才寫進 Postgres 的新對話，回滾後不會出現在 SQLite 裡。
