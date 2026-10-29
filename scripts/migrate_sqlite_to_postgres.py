"""一次性轉檔：SQLite POC 資料庫 → PostgreSQL（migrate-to-postgres-and-containerize 決策 5）。

把 data/poc.db 的 9 張表完整搬進已套用最新遷移的空 Postgres，保留原始 id
與關聯。全部在單一交易內完成，任何一步失敗就整批回復，目的資料庫維持空的。
來源以唯讀模式開啟，poc.db 不會被修改——這是回滾到 SQLite 的前提。

前置條件：
- 服務已停止（不再寫入 poc.db）
- 目的資料庫已執行 `alembic upgrade head`，且尚未有任何業務資料
  （不要先啟動 app：它啟動時會寫入晴晴的角色資料，轉檔就會被拒絕）

用法（在專案根目錄執行）：

    .venv/Scripts/python.exe scripts/migrate_sqlite_to_postgres.py
    .venv/Scripts/python.exe scripts/migrate_sqlite_to_postgres.py --source data/poc.db \\
        --target postgresql+psycopg://cia:密碼@127.0.0.1:5432/cia

--target 省略時使用 .env 的 DATABASE_URL。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 讓腳本能從專案根目錄以外的位置執行
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from alembic.runtime.migration import MigrationContext  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import Engine, String, Table, Text, create_engine, func, inspect, select, text  # noqa: E402

from app.db.models import Base  # noqa: E402

# 依外鍵相依順序：被參照的表先搬
TABLE_ORDER = [
    "tenants",
    "accounts",
    "login_sessions",
    "personas",
    "users",
    "user_persona_links",
    "user_persona_versions",
    "persona_coaching",
    "coach_digests",
    "sessions",
    "messages",
    "realtime_signals",
    "coach_reports",
    "safety_events",
    "rate_budget",
    "send_errors",
    "settings_link_claims",
]

# 轉檔後逐筆比對的外鍵關聯：(表, 外鍵欄位)
RELATIONS_TO_VERIFY = [
    ("messages", "session_id"),
    ("realtime_signals", "message_id"),
    ("realtime_signals", "session_id"),
    ("coach_reports", "session_id"),
    ("user_persona_links", "user_id"),
    ("sessions", "user_id"),
]


class MigrationRefused(Exception):
    """前置檢查或資料預檢不通過。訊息會指出具體原因，目的資料庫未被動到。"""


def _tables() -> list[Table]:
    assert set(TABLE_ORDER) == set(Base.metadata.tables), "TABLE_ORDER 與資料模型不一致"
    return [Base.metadata.tables[name] for name in TABLE_ORDER]


def source_engine(path: Path) -> Engine:
    """以唯讀模式開啟 SQLite（mode=ro），保證不會寫入來源檔案。"""
    uri = path.resolve().as_uri() + "?mode=ro"
    return create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))


# ── 前置檢查 ────────────────────────────────────────────────


def check_source(src: Engine) -> None:
    """來源必須有資料模型定義的每張表與每個欄位，否則讀出來的資料會缺欄位。"""
    inspector = inspect(src)
    existing = set(inspector.get_table_names())
    problems = []
    for table in _tables():
        if table.name not in existing:
            problems.append(f"來源缺少資料表 {table.name}")
            continue
        columns = {c["name"] for c in inspector.get_columns(table.name)}
        missing = [c.name for c in table.columns if c.name not in columns]
        if missing:
            problems.append(f"來源資料表 {table.name} 缺少欄位 {missing}")
    if problems:
        raise MigrationRefused("；".join(problems))


def check_target(dst: Engine) -> None:
    if dst.dialect.name != "postgresql":
        raise MigrationRefused(f"目的資料庫必須是 PostgreSQL，目前是 {dst.dialect.name}")

    expected = set(ScriptDirectory(str(ROOT / "alembic")).get_heads())
    with dst.connect() as conn:
        current = set(MigrationContext.configure(conn).get_current_heads())
        if current != expected:
            raise MigrationRefused(
                f"目的資料庫結構版本為 {sorted(current) or '（尚未套用任何遷移）'}，"
                f"需要 {sorted(expected)}。請先執行 `alembic upgrade head`"
            )

        non_empty = {
            t.name: n
            for t in _tables()
            if (n := conn.execute(select(func.count()).select_from(t)).scalar_one())
        }
    if non_empty:
        raise MigrationRefused(
            f"目的資料庫已有資料 {non_empty}。轉檔只會寫入空的資料庫，不覆蓋也不合併"
        )


# ── 讀取與資料預檢 ──────────────────────────────────────────


def read_source(src: Engine) -> dict[str, list[dict[str, Any]]]:
    """透過資料模型的表定義讀取，SQLAlchemy 會把 SQLite 存的時間字串、
    0/1、JSON 字串轉成 datetime、bool、dict/list。無時區的時間補上 UTC
    （應用程式寫入的一律是 UTC，只是 SQLite 不保存時區）。
    """
    data: dict[str, list[dict[str, Any]]] = {}
    with src.connect() as conn:
        for table in _tables():
            rows = conn.execute(select(table).order_by(table.c.id)).mappings().all()
            data[table.name] = [{k: _with_utc(v) for k, v in row.items()} for row in rows]
    return data


def _with_utc(value: Any) -> Any:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def precheck(data: dict[str, list[dict[str, Any]]]) -> None:
    """SQLite 不檢查 VARCHAR(n) 長度，也接受 NUL 字元；Postgres 兩者都會拒絕。
    先全部掃過一遍再一次列出，不要搬到一半才失敗、而且只看到第一個問題。
    """
    problems = []
    for table in _tables():
        text_columns = [c for c in table.columns if isinstance(c.type, (String, Text))]
        for row in data[table.name]:
            for col in text_columns:
                value = row[col.name]
                if not isinstance(value, str):
                    continue
                limit = getattr(col.type, "length", None)
                if limit is not None and len(value) > limit:
                    problems.append(
                        f"{table.name} id={row['id']} 欄位 {col.name} 長度 {len(value)} 超過上限 {limit}"
                    )
                if "\x00" in value:
                    problems.append(f"{table.name} id={row['id']} 欄位 {col.name} 含 NUL 字元")
    if problems:
        raise MigrationRefused("資料預檢未通過，未做任何寫入：\n  " + "\n  ".join(problems))


# ── 寫入與校驗 ──────────────────────────────────────────────


def copy_all(dst: Engine, data: dict[str, list[dict[str, Any]]], out: Callable[[str], None]) -> None:
    """單一交易：寫入 → 重設序號 → 校驗。任一步失敗，整批回復。"""
    with dst.begin() as conn:
        for table in _tables():
            rows = data[table.name]
            if rows:
                conn.execute(table.insert(), rows)

        # 保留了原始 id，序號卻還停在 1；不重設的話，搬完後第一筆新資料
        # 就會撞上既有的主鍵。空表設為 1 且 is_called=false，下一個取得 1。
        for table in _tables():
            conn.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                    f"COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table.name}"
                )
            )

        _verify(conn, data, out)


def _verify(conn, data: dict[str, list[dict[str, Any]]], out: Callable[[str], None]) -> None:
    out(f"{'資料表':<20}{'來源':>8}{'目的':>8}")
    mismatched = []
    for table in _tables():
        expected = len(data[table.name])
        actual = conn.execute(select(func.count()).select_from(table)).scalar_one()
        mark = "" if expected == actual else "  ✗"
        out(f"{table.name:<20}{expected:>8}{actual:>8}{mark}")
        if expected != actual:
            mismatched.append(table.name)
    if mismatched:
        raise RuntimeError(f"筆數不一致：{mismatched}")

    for table_name, fk in RELATIONS_TO_VERIFY:
        table = Base.metadata.tables[table_name]
        expected = {(r["id"], r[fk]) for r in data[table_name]}
        actual = set(conn.execute(select(table.c.id, table.c[fk])).tuples().all())
        if expected != actual:
            raise RuntimeError(f"關聯不一致：{table_name}.{fk}")
        out(f"關聯一致：{table_name}.{fk}（{len(expected)} 筆）")


def migrate(source: Path, target_url: str, out: Callable[[str], None] = print) -> None:
    if not source.is_file():
        raise MigrationRefused(f"找不到來源檔案 {source}")

    src = source_engine(source)
    dst = create_engine(target_url, connect_args={"connect_timeout": 10})
    try:
        check_source(src)
        check_target(dst)
        data = read_source(src)
        precheck(data)
        copy_all(dst, data, out)
    finally:
        src.dispose()
        dst.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite POC 資料庫 → PostgreSQL 一次性轉檔")
    parser.add_argument("--source", type=Path, default=ROOT / "data" / "poc.db")
    parser.add_argument("--target", help="Postgres 連線字串，省略時使用 .env 的 DATABASE_URL")
    args = parser.parse_args()

    if args.target:
        target = args.target
    else:
        from app.config import get_settings

        target = get_settings().database_url

    try:
        migrate(args.source, target)
    except MigrationRefused as exc:
        print(f"轉檔未執行：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 —— 交易已回復，說清楚狀態後結束
        print(f"轉檔失敗，已整批回復，目的資料庫未寫入任何資料：{exc}", file=sys.stderr)
        return 1

    print("轉檔完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
