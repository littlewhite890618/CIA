"""資料庫連線與 Session 管理。

正式環境為 PostgreSQL 16，資料表結構由 Alembic 管理（alembic/）。
SQLite 仍保留給測試與不用 Docker 的本機執行，此檔的 SQLite 專屬設定
只在 DATABASE_URL 指向 SQLite 時生效。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import Base
from app.logging_config import get_logger

log = get_logger(__name__)

_settings = get_settings()

# 專案根目錄下的 alembic/。容器內 WORKDIR 為 /app，結構與本機相同。
_ALEMBIC_DIR = Path(__file__).resolve().parents[2] / "alembic"


class DatabaseNotReady(RuntimeError):
    """資料庫連不上，或結構版本不是最新。服務不該在這種狀態下接收 webhook。"""


# SQLite 需要關閉同執行緒檢查。Postgres 設連線逾時：psycopg 預設不逾時，
# 資料庫沒起來時啟動會無限期卡住，而不是失敗並說明原因。
_connect_args = (
    {"check_same_thread": False} if _settings.is_sqlite else {"connect_timeout": 10}
)

if _settings.is_sqlite:
    # 確保 data/ 目錄存在，否則 SQLite 無法建檔
    db_path = _settings.database_url.replace("sqlite:///", "")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine: Engine = create_engine(
    _settings.database_url,
    connect_args=_connect_args,
    echo=False,
    future=True,
)


if _settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _connection_record) -> None:
        """SQLite 預設不強制外鍵，需明確開啟。

        WAL 模式讓讀寫可並行 —— webhook 寫入與背景輪詢讀取會同時發生。
        """
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def db_session() -> Iterator[Session]:
    """交易範圍的 Session。

    用法：
        with db_session() as db:
            db.add(obj)
        # 離開時自動 commit，例外時自動 rollback
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依賴注入用。"""
    with db_session() as session:
        yield session


def init_db() -> None:
    """服務啟動時確認資料庫可用。

    - SQLite（測試、不用 Docker 的本機執行）：以 create_all 建表。只適用於
      全新的檔案——create_all 不會替已存在的表補欄位。
    - Postgres（正式環境）：不建表也不改結構，只確認連得上、且已套用到
      Alembic 最新版本。結構變更一律由 `alembic upgrade head` 負責（容器
      啟動指令會先跑它），這裡不自行補建，避免應用程式和遷移腳本各改一半。
    """
    if _settings.is_sqlite:
        Base.metadata.create_all(bind=engine)
        log.info(
            "資料表已就緒",
            extra={"tables": len(Base.metadata.tables), "url": _settings.database_url_for_log},
        )
        return

    _verify_postgres_ready()


def _verify_postgres_ready() -> None:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            current = set(MigrationContext.configure(conn).get_current_heads())
    except OperationalError as exc:
        log.error("資料庫無法連線，服務無法啟動", extra={"error": str(exc.orig)})
        raise DatabaseNotReady(
            f"無法連線到資料庫，請確認 Postgres 已啟動且 DATABASE_URL 正確：{exc.orig}"
        ) from exc

    expected = set(ScriptDirectory(str(_ALEMBIC_DIR)).get_heads())
    if current != expected:
        log.error(
            "資料庫結構不是最新版本，服務無法啟動",
            extra={"current": sorted(current), "expected": sorted(expected)},
        )
        raise DatabaseNotReady(
            f"資料庫結構版本為 {sorted(current) or '（尚未套用任何遷移）'}，"
            f"最新版本為 {sorted(expected)}。請先執行 `alembic upgrade head`"
        )

    log.info("資料庫已就緒", extra={"revision": sorted(current)})


def check_connection() -> bool:
    """健康檢查用：實際查詢一次資料庫，連不上回傳 False 而不拋錯。"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False
