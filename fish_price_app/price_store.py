"""SQLite for local development and PostgreSQL for the shared cloud version."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from io import BytesIO
import os
from pathlib import Path
import sqlite3
from typing import Any

from openpyxl import Workbook, load_workbook


_DATABASE_URL: str | None = None


def configure_database(database_url: str | None) -> None:
    global _DATABASE_URL
    _DATABASE_URL = database_url or None


class _Connection:
    def __init__(self, raw, postgres: bool):
        self.raw = raw
        self.postgres = postgres

    def execute(self, query: str, params=()):
        if self.postgres:
            query = query.replace("?", "%s")
        return self.raw.execute(query, params)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def _local_path() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    folder = Path(root) / "Trade81FishPrice" if root else Path.home() / ".trade81_fish_price"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "prices.db"


def connect() -> _Connection:
    postgres = bool(_DATABASE_URL)
    if postgres:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("云端数据库驱动未安装；请确认 requirements.txt 已包含 psycopg。") from exc
        raw = psycopg.connect(_DATABASE_URL, row_factory=dict_row, connect_timeout=15, prepare_threshold=None)
        db = _Connection(raw, True)
        db.execute("""
            CREATE TABLE IF NOT EXISTS price_catalog (
                product_key TEXT PRIMARY KEY,
                product_name TEXT NOT NULL,
                supplier_quote_jpy DOUBLE PRECISION NOT NULL,
                source_date TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source TEXT NOT NULL,
                updated_by TEXT NOT NULL DEFAULT ''
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id BIGSERIAL PRIMARY KEY,
                product_key TEXT NOT NULL,
                product_name TEXT NOT NULL,
                supplier_quote_jpy DOUBLE PRECISION NOT NULL,
                source_date TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source TEXT NOT NULL,
                updated_by TEXT NOT NULL DEFAULT ''
            )
        """)
        db.execute("ALTER TABLE price_catalog ADD COLUMN IF NOT EXISTS updated_by TEXT NOT NULL DEFAULT ''")
        db.execute("ALTER TABLE price_history ADD COLUMN IF NOT EXISTS updated_by TEXT NOT NULL DEFAULT ''")
        db.commit()
        return db

    raw = sqlite3.connect(_local_path())
    raw.row_factory = sqlite3.Row
    db = _Connection(raw, False)
    db.execute("""
        CREATE TABLE IF NOT EXISTS price_catalog (
            product_key TEXT PRIMARY KEY,
            product_name TEXT NOT NULL,
            supplier_quote_jpy REAL NOT NULL,
            source_date TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            source TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT ''
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_key TEXT NOT NULL,
            product_name TEXT NOT NULL,
            supplier_quote_jpy REAL NOT NULL,
            source_date TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            source TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT ''
        )
    """)
    # Add the audit field to a local database created by an earlier app version.
    for table in ("price_catalog", "price_history"):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
        if "updated_by" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN updated_by TEXT NOT NULL DEFAULT ''")
    db.commit()
    return db


@contextmanager
def db_session():
    db = connect()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def list_prices() -> list[dict[str, Any]]:
    with db_session() as db:
        rows = db.execute("SELECT * FROM price_catalog ORDER BY LOWER(product_name)").fetchall()
    return [dict(row) for row in rows]


def list_history(limit: int | None = 30) -> list[dict[str, Any]]:
    with db_session() as db:
        if limit is None:
            rows = db.execute("SELECT * FROM price_history ORDER BY id DESC").fetchall()
        else:
            rows = db.execute("SELECT * FROM price_history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def _record_change(db: _Connection, key: str, name: str, quote: float,
                   source_date: str, source: str, now: str, updated_by: str) -> None:
    db.execute("""
        INSERT INTO price_history(product_key, product_name, supplier_quote_jpy, source_date, updated_at, source, updated_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (key, name, quote, source_date, now, source, updated_by))


def apply_sheet(sheet: dict[str, Any], updated_by: str = "") -> dict[str, int]:
    """Import a dated sheet; newer source dates supersede older/manual values."""
    changed = skipped = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_session() as db:
        for item in sheet["rows"]:
            key = item["key"]
            quote = item["fishmonger_quote_jpy"]
            if not key or quote is None:
                continue
            old = db.execute("SELECT * FROM price_catalog WHERE product_key=?", (key,)).fetchone()
            if old and sheet["date"] < old["source_date"]:
                skipped += 1
                continue
            if old and sheet["date"] == old["source_date"] and old["source"] == "手动修改":
                skipped += 1
                continue
            if old and sheet["date"] == old["source_date"] and old["source"] == "价格表" and old["supplier_quote_jpy"] == quote:
                skipped += 1
                continue
            name = item["canonical_name"] or item["product"]
            db.execute("""
                INSERT INTO price_catalog(product_key, product_name, supplier_quote_jpy, source_date, updated_at, source, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_key) DO UPDATE SET
                    product_name=excluded.product_name,
                    supplier_quote_jpy=excluded.supplier_quote_jpy,
                    source_date=excluded.source_date,
                    updated_at=excluded.updated_at,
                    source=excluded.source,
                    updated_by=excluded.updated_by
            """, (key, name, quote, sheet["date"], now, "价格表", updated_by))
            _record_change(db, key, name, quote, sheet["date"], "价格表", now, updated_by)
            changed += 1
    return {"changed": changed, "skipped": skipped}


def manual_update(key: str, quote: float, updated_by: str = "") -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_session() as db:
        old = db.execute("SELECT * FROM price_catalog WHERE product_key=?", (key,)).fetchone()
        if not old:
            return
        name = old["product_name"]
        db.execute("""
            UPDATE price_catalog SET supplier_quote_jpy=?, updated_at=?, source='手动修改', updated_by=?
            WHERE product_key=?
        """, (quote, now, updated_by, key))
        _record_change(db, key, name, quote, old["source_date"], "手动修改", now, updated_by)


def export_backup_xlsx() -> bytes:
    wb = Workbook()
    catalog = wb.active
    catalog.title = "当前价格"
    catalog.append(["product_key", "product_name", "supplier_quote_jpy", "source_date", "updated_at", "source", "updated_by"])
    for row in list_prices():
        catalog.append([row.get(k) for k in ("product_key", "product_name", "supplier_quote_jpy", "source_date", "updated_at", "source", "updated_by")])
    history = wb.create_sheet("更新历史")
    history.append(["product_key", "product_name", "supplier_quote_jpy", "source_date", "updated_at", "source", "updated_by"])
    for row in reversed(list_history(limit=None)):
        history.append([row.get(k) for k in ("product_key", "product_name", "supplier_quote_jpy", "source_date", "updated_at", "source", "updated_by")])
    stream = BytesIO()
    wb.save(stream)
    return stream.getvalue()


def import_backup_xlsx(file) -> int:
    wb = load_workbook(file, data_only=True, read_only=True)
    if "当前价格" not in wb.sheetnames:
        raise ValueError("迁移文件里没有“当前价格”工作表。")
    catalog_ws = wb["当前价格"]
    headers = [cell.value for cell in catalog_ws[1]]
    records = [dict(zip(headers, row)) for row in catalog_ws.iter_rows(min_row=2, values_only=True) if any(v is not None for v in row)]
    if not records:
        raise ValueError("迁移文件中没有价格记录。")
    with db_session() as db:
        existing = db.execute("SELECT COUNT(*) AS n FROM price_catalog").fetchone()["n"]
        if existing:
            raise ValueError("云端价格库已有数据。为了避免覆盖，请仅在空价格库中导入迁移文件。")
        for row in records:
            db.execute("""
                INSERT INTO price_catalog(product_key, product_name, supplier_quote_jpy, source_date, updated_at, source, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, tuple(row.get(k) or "" for k in ("product_key", "product_name")) + (
                float(row.get("supplier_quote_jpy") or 0), str(row.get("source_date") or ""),
                str(row.get("updated_at") or ""), str(row.get("source") or "迁移导入"), str(row.get("updated_by") or ""),
            ))
        if "更新历史" in wb.sheetnames:
            history_ws = wb["更新历史"]
            history_headers = [cell.value for cell in history_ws[1]]
            for values in history_ws.iter_rows(min_row=2, values_only=True):
                if not any(v is not None for v in values):
                    continue
                row = dict(zip(history_headers, values))
                db.execute("""
                    INSERT INTO price_history(product_key, product_name, supplier_quote_jpy, source_date, updated_at, source, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    str(row.get("product_key") or ""), str(row.get("product_name") or ""),
                    float(row.get("supplier_quote_jpy") or 0), str(row.get("source_date") or ""),
                    str(row.get("updated_at") or ""), str(row.get("source") or "迁移导入"), str(row.get("updated_by") or ""),
                ))
    return len(records)
