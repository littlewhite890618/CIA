# IG AI 戀愛顧問：應用程式映像檔（migrate-to-postgres-and-containerize 決策 6）
#
# 正式模式（docker compose up）用的是這裡複製進去的程式碼；開發模式
# （再疊加 docker-compose.dev.yml）沿用這裡裝好的 Python 與套件，程式碼
# 改由本機掛載。只有改了 requirements.txt 才需要重建。

# ── 第一階段：建置使用者設定頁的前端（React，add-persona-authoring-console 第 6 群）──
# 只把打包結果（frontend/dist）帶進執行用的映像檔，Node 不進正式環境
FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ── 第二階段：執行用的映像檔 ──
# 對齊本機開發用的 Python 3.13，避免「本機能跑、容器不能跑」
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 結構與專案根目錄相同（/app/app/main.py、/app/alembic/…），
# 容器內路徑與本機一一對應，人設卡與開發模式的掛載都依賴這一點
WORKDIR /app

# 先裝套件再複製程式碼：只改程式碼時，套件這層可以沿用快取
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY alembic/ alembic/
COPY app/ app/
COPY scripts/ scripts/
COPY --from=frontend /build/dist frontend/dist

RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000

# 先把資料表結構套用到最新版本，成功才啟動服務（init_db 也會再確認一次）。
# 不另寫 entrypoint.sh：Windows 上編輯的 shell 檔容易存成 CRLF，在 Linux
# 容器內會變成難以理解的「找不到檔案」。
# --workers 1 刻意寫明：速率配額與靜默輪詢都在行程內，多個 worker 會各算各的。
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
