"""SQLite for local development and PostgreSQL for the shared cloud version."""
from __future__ import annotations

from contextlib import contextmanager
from collections import defaultdict
from datetime import datetime, timedelta
from io import BytesIO
import os
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook
from price_parser import is_ignored_product


_DATABASE_URL: str | None = None
_SCHEMA_TARGET: str | None = None


def configure_database(database_url: str | None) -> None:
    global _DATABASE_URL, _SCHEMA_TARGET
    normalized = database_url or None
    if normalized != _DATABASE_URL:
        _SCHEMA_TARGET = None
    _DATABASE_URL = normalized


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


def _now_text() -> str:
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M:%S")


def connect() -> _Connection:
    global _SCHEMA_TARGET
    postgres = bool(_DATABASE_URL)
    if postgres:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("云端数据库驱动未安装；请确认 requirements.txt 已包含 psycopg。") from exc
        raw = psycopg.connect(_DATABASE_URL, row_factory=dict_row, connect_timeout=15, prepare_threshold=None)
        db = _Connection(raw, True)
        target = f"postgres:{_DATABASE_URL}"
        if _SCHEMA_TARGET != target:
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
            db.execute("""
            CREATE TABLE IF NOT EXISTS order_daily_counts (
                order_date TEXT NOT NULL,
                product_key TEXT NOT NULL,
                product_name TEXT NOT NULL,
                order_count BIGINT NOT NULL,
                PRIMARY KEY(order_date, product_key)
            )
            """)
            db.execute("""
            CREATE TABLE IF NOT EXISTS product_aliases (
                alias_key TEXT PRIMARY KEY,
                alias_name TEXT NOT NULL,
                canonical_key TEXT NOT NULL,
                canonical_name TEXT NOT NULL
            )
            """)
            db.execute("""
            CREATE TABLE IF NOT EXISTS excluded_products (
                product_key TEXT PRIMARY KEY,
                product_name TEXT NOT NULL,
                excluded_at TEXT NOT NULL
            )
            """)
            db.execute("ALTER TABLE price_catalog ADD COLUMN IF NOT EXISTS updated_by TEXT NOT NULL DEFAULT ''")
            db.execute("ALTER TABLE price_history ADD COLUMN IF NOT EXISTS updated_by TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS idx_price_history_product_key ON price_history(product_key)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_price_history_updated_at ON price_history(updated_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_order_counts_product_key ON order_daily_counts(product_key)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_product_aliases_canonical_key ON product_aliases(canonical_key)")
            db.commit()
            _SCHEMA_TARGET = target
        return db

    path = _local_path()
    target = f"sqlite:{path.resolve()}"
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    db = _Connection(raw, False)
    if _SCHEMA_TARGET != target:
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
        db.execute("""
        CREATE TABLE IF NOT EXISTS order_daily_counts (
            order_date TEXT NOT NULL,
            product_key TEXT NOT NULL,
            product_name TEXT NOT NULL,
            order_count INTEGER NOT NULL,
            PRIMARY KEY(order_date, product_key)
        )
        """)
        db.execute("""
        CREATE TABLE IF NOT EXISTS product_aliases (
            alias_key TEXT PRIMARY KEY,
            alias_name TEXT NOT NULL,
            canonical_key TEXT NOT NULL,
            canonical_name TEXT NOT NULL
        )
        """)
        db.execute("""
        CREATE TABLE IF NOT EXISTS excluded_products (
            product_key TEXT PRIMARY KEY,
            product_name TEXT NOT NULL,
            excluded_at TEXT NOT NULL
        )
        """)
        # Add the audit field to a local database created by an earlier app version.
        for table in ("price_catalog", "price_history"):
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
            if "updated_by" not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN updated_by TEXT NOT NULL DEFAULT ''")
        db.execute("CREATE INDEX IF NOT EXISTS idx_price_history_product_key ON price_history(product_key)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_price_history_updated_at ON price_history(updated_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_order_counts_product_key ON order_daily_counts(product_key)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_product_aliases_canonical_key ON product_aliases(canonical_key)")
        db.commit()
        _SCHEMA_TARGET = target
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
        price_rows = [dict(row) for row in db.execute("SELECT * FROM price_catalog").fetchall()]
        count_rows = [dict(row) for row in db.execute(
            "SELECT product_key, product_name, SUM(order_count) AS order_count "
            "FROM order_daily_counts GROUP BY product_key, product_name"
        ).fetchall()]
        alias_rows = [dict(row) for row in db.execute("SELECT alias_key, alias_name, canonical_key, canonical_name FROM product_aliases").fetchall()]
        excluded_keys = {row["product_key"] for row in db.execute("SELECT product_key FROM excluded_products").fetchall()}

    alias_map = {row["alias_key"]: row for row in alias_rows}
    groups: dict[str, dict[str, Any]] = {}

    def target_for(key: str, fallback_name: str = "") -> tuple[str, str]:
        alias = alias_map.get(key)
        return (alias["canonical_key"], alias["canonical_name"]) if alias else (key, fallback_name)

    for row in price_rows:
        if row["product_key"] in excluded_keys or is_ignored_product(row.get("product_name")):
            continue
        canonical_key, canonical_name = target_for(row["product_key"], row["product_name"])
        if is_ignored_product(canonical_name):
            continue
        bucket = groups.setdefault(canonical_key, {
            "product_key": canonical_key, "product_name": canonical_name,
            "member_keys": [], "order_count": 0, "_prices": [], "aliases": set(),
        })
        bucket["member_keys"].append(row["product_key"])
        bucket["_prices"].append(row)
    for row in count_rows:
        if row["product_key"] in excluded_keys or is_ignored_product(row.get("product_name")):
            continue
        canonical_key, _canonical_name = target_for(row["product_key"])
        if canonical_key in groups:
            groups[canonical_key]["order_count"] += int(row["order_count"] or 0)
    for row in alias_rows:
        if row["canonical_key"] in groups and row["alias_key"] != row["canonical_key"]:
            groups[row["canonical_key"]]["aliases"].add(row["alias_name"])

    result = []
    for bucket in groups.values():
        newest = max(bucket.pop("_prices"), key=lambda row: (row["source_date"], row["updated_at"], row["product_key"] == bucket["product_key"]))
        bucket.update({key: newest[key] for key in ("supplier_quote_jpy", "source_date", "updated_at", "source", "updated_by")})
        bucket["member_keys"] = sorted(set(bucket["member_keys"]))
        bucket["aliases"] = sorted(bucket["aliases"])
        result.append(bucket)
    return sorted(result, key=lambda row: (-row["order_count"], row["product_name"].casefold()))


def list_product_groups() -> list[dict[str, Any]]:
    with db_session() as db:
        rows = db.execute("""
            SELECT alias_key, alias_name, canonical_key, canonical_name
            FROM product_aliases ORDER BY canonical_name, alias_name
        """).fetchall()
        catalog = db.execute("SELECT product_key, product_name FROM price_catalog").fetchall()
        excluded = {row["product_key"] for row in db.execute("SELECT product_key FROM excluded_products").fetchall()}
    groups: dict[str, dict[str, Any]] = {}
    catalog_names = {row["product_key"]: row["product_name"] for row in catalog}
    for raw in rows:
        row = dict(raw)
        if is_ignored_product(row["canonical_name"]):
            continue
        group = groups.setdefault(row["canonical_key"], {
            "canonical_key": row["canonical_key"],
            "canonical_name": row["canonical_name"],
            "aliases": [],
            "members": {},
        })
        if row["alias_key"] != row["canonical_key"] and row["alias_key"] not in excluded and not is_ignored_product(row["alias_name"]):
            group["aliases"].append(row["alias_name"])
            group["members"][row["alias_key"]] = row["alias_name"]
    for group in groups.values():
        if group["canonical_key"] in catalog_names and group["canonical_key"] not in excluded:
            group["members"][group["canonical_key"]] = catalog_names[group["canonical_key"]]
        group["members"] = [
            {"key": key, "name": name}
            for key, name in sorted(group["members"].items(), key=lambda pair: pair[1].casefold())
        ]
    return list(groups.values())


def resolve_sheet_products(sheet: dict[str, Any]) -> dict[str, Any]:
    """Attach custom display keys to rows, retaining raw keys for price/order storage."""
    with db_session() as db:
        aliases = {
            row["alias_key"]: dict(row)
            for row in db.execute("SELECT alias_key, alias_name, canonical_key, canonical_name FROM product_aliases").fetchall()
        }
        excluded_keys = {row["product_key"] for row in db.execute("SELECT product_key FROM excluded_products").fetchall()}
    resolved = {**sheet, "rows": []}
    for original in sheet.get("rows", []):
        row = dict(original)
        row.setdefault("raw_key", row.get("key", ""))
        row.setdefault("raw_name", row.get("canonical_name") or row.get("product") or row.get("key", ""))
        if row["raw_key"] in excluded_keys or is_ignored_product(row["raw_name"]):
            continue
        alias = aliases.get(row["raw_key"])
        if alias:
            if is_ignored_product(alias["canonical_name"]):
                continue
            row["key"] = alias["canonical_key"]
            row["canonical_name"] = alias["canonical_name"]
        resolved["rows"].append(row)
    return resolved


def save_product_group(primary_name: str, alias_names: list[str]) -> None:
    """Add or combine user-defined names under one display name without deleting source rows."""
    from price_parser import canonical_product_name, product_key

    canonical_name = canonical_product_name(primary_name)
    canonical_key = product_key(canonical_name)
    if not canonical_key:
        raise ValueError("请填写主要显示品名。")

    submitted: dict[str, str] = {canonical_key: canonical_name}
    for name in alias_names:
        display_name = canonical_product_name(name)
        key = product_key(display_name)
        if key:
            submitted[key] = display_name
    if len(submitted) < 2:
        raise ValueError("请至少填写一个需要合并的其他品名。")

    with db_session() as db:
        existing = [dict(row) for row in db.execute("SELECT alias_key, alias_name, canonical_key, canonical_name FROM product_aliases").fetchall()]
        submitted_keys = set(submitted)
        source_groups = {canonical_key}
        source_groups.update(row["canonical_key"] for row in existing if row["alias_key"] in submitted_keys)
        members = dict(submitted)
        for row in existing:
            if row["canonical_key"] in source_groups:
                members[row["alias_key"]] = row["alias_name"]

        # Existing groups touched by any selected name are reassigned as a whole.
        db.execute("DELETE FROM product_aliases")
        for alias_key, alias_name in members.items():
            db.execute("""
                INSERT INTO product_aliases(alias_key, alias_name, canonical_key, canonical_name)
                VALUES (?, ?, ?, ?)
            """, (alias_key, alias_name, canonical_key, canonical_name))
        # Preserve untouched groups.
        for row in existing:
            if row["canonical_key"] not in source_groups and row["alias_key"] not in members:
                db.execute("""
                    INSERT INTO product_aliases(alias_key, alias_name, canonical_key, canonical_name)
                    VALUES (?, ?, ?, ?)
                """, (row["alias_key"], row["alias_name"], row["canonical_key"], row["canonical_name"]))


def merge_product_groups(primary_name: str, selected_keys: list[str]) -> None:
    """Merge selected existing catalog rows/groups under a chosen display name."""
    from price_parser import canonical_product_name, product_key

    canonical_name = canonical_product_name(primary_name)
    display_key = product_key(canonical_name)
    selected = {str(key) for key in selected_keys if str(key)}
    if not display_key:
        raise ValueError("请填写合并后的主要显示品名。")
    if len(selected) < 2:
        raise ValueError("请至少选择两个现有品种。")

    with db_session() as db:
        catalog = [dict(row) for row in db.execute(
            "SELECT product_key, product_name FROM price_catalog"
        ).fetchall()]
        existing = [dict(row) for row in db.execute(
            "SELECT alias_key, alias_name, canonical_key, canonical_name FROM product_aliases"
        ).fetchall()]

        alias_map = {row["alias_key"]: row for row in existing}
        selected_groups = {
            alias_map[key]["canonical_key"] if key in alias_map else key
            for key in selected
        }
        selected_groups.update(
            row["canonical_key"] for row in existing if row["canonical_name"] == canonical_name
        )
        if display_key in alias_map:
            selected_groups.add(alias_map[display_key]["canonical_key"])
        elif any(row["product_key"] == display_key for row in catalog):
            selected.add(display_key)
        # If a selected group already has this display name, retain its stable ID.
        target_key = next((
            group_key for group_key in selected_groups
            if any(row["canonical_key"] == group_key and row["canonical_name"] == canonical_name for row in existing)
            and group_key.startswith("__group__:")
        ), f"__group__:{uuid4().hex}")

        members: dict[str, str] = {}
        for row in catalog:
            mapped = alias_map.get(row["product_key"])
            group_key = mapped["canonical_key"] if mapped else row["product_key"]
            if row["product_key"] in selected or group_key in selected_groups:
                members[row["product_key"]] = row["product_name"]
        for row in existing:
            if row["canonical_key"] in selected_groups or row["alias_key"] in selected:
                # Identity rows mark a group; they are recreated below under the
                # new stable ID, not copied as a duplicate member.
                if row["alias_key"] != row["canonical_key"]:
                    members[row["alias_key"]] = row["alias_name"]
        # Keep the selected canonical identities even if a member has no current price row.
        for key in selected:
            if key in alias_map:
                if key != alias_map[key]["canonical_key"]:
                    members[key] = alias_map[key]["alias_name"]
            elif key not in members:
                members[key] = next(
                    (row["product_name"] for row in catalog if row["product_key"] == key), key
                )

        members[target_key] = canonical_name
        selected_groups.add(target_key)
        db.execute("DELETE FROM product_aliases")
        for alias_key, alias_name in members.items():
            db.execute(
                """INSERT INTO product_aliases(alias_key, alias_name, canonical_key, canonical_name)
                   VALUES (?, ?, ?, ?)""",
                (alias_key, alias_name, target_key, canonical_name),
            )
        for row in existing:
            if row["canonical_key"] not in selected_groups and row["alias_key"] not in members:
                db.execute(
                    """INSERT INTO product_aliases(alias_key, alias_name, canonical_key, canonical_name)
                       VALUES (?, ?, ?, ?)""",
                    (row["alias_key"], row["alias_name"], row["canonical_key"], row["canonical_name"]),
                )


def remove_product_from_group(canonical_key: str, member_key: str) -> None:
    """Remove one actual product from a group while keeping the other members together."""
    with db_session() as db:
        group_rows = [dict(row) for row in db.execute(
            "SELECT alias_key, alias_name, canonical_key, canonical_name "
            "FROM product_aliases WHERE canonical_key=?", (canonical_key,)
        ).fetchall()]
        if not group_rows:
            return
        product = db.execute("SELECT product_key FROM price_catalog WHERE product_key=?", (member_key,)).fetchone()
        if member_key == canonical_key and not product:
            raise ValueError("这个成员没有当前商品记录，无法单独移除。")

        new_key = canonical_key
        if member_key == canonical_key:
            # Older groups used the product key as their group ID. Move the group
            # to a synthetic key before releasing that product back to the catalog.
            new_key = f"__group__:{uuid4().hex}"

        db.execute("DELETE FROM product_aliases WHERE alias_key=?", (member_key,))
        if new_key != canonical_key:
            db.execute("UPDATE product_aliases SET canonical_key=? WHERE canonical_key=?", (new_key, canonical_key))
            primary = group_rows[0]["canonical_name"]
            db.execute(
                """INSERT INTO product_aliases(alias_key, alias_name, canonical_key, canonical_name)
                   VALUES (?, ?, ?, ?)""",
                (new_key, primary, new_key, primary),
            )
        remaining = db.execute(
            "SELECT COUNT(*) AS n FROM product_aliases WHERE canonical_key=? AND alias_key<>canonical_key",
            (new_key,),
        ).fetchone()["n"]
        if remaining < 2:
            db.execute("DELETE FROM product_aliases WHERE canonical_key=?", (new_key,))


def list_excluded_products() -> list[dict[str, Any]]:
    with db_session() as db:
        rows = db.execute(
            "SELECT product_key, product_name, excluded_at FROM excluded_products ORDER BY product_name"
        ).fetchall()
    return [dict(row) for row in rows]


def exclude_products(product_keys: list[str]) -> int:
    """Hide selected catalog products and keep them out of future price imports."""
    keys = {str(key) for key in product_keys if str(key)}
    if not keys:
        return 0
    now = _now_text()
    with db_session() as db:
        alias_rows = [dict(row) for row in db.execute(
            "SELECT alias_key, alias_name, canonical_key FROM product_aliases"
        ).fetchall()]
        catalog_rows = [dict(row) for row in db.execute("SELECT product_key, product_name FROM price_catalog").fetchall()]
        catalog_keys = {row["product_key"] for row in catalog_rows}
        group_keys = {row["canonical_key"] for row in alias_rows if row["alias_key"] in keys or row["canonical_key"] in keys}
        keys.update(
            row["alias_key"] for row in alias_rows
            if row["canonical_key"] in group_keys
            and (row["alias_key"] != row["canonical_key"] or row["alias_key"] in catalog_keys)
        )
        names = {row["product_key"]: row["product_name"] for row in catalog_rows}
        names.update({row["alias_key"]: row["alias_name"] for row in alias_rows})
        for key in keys:
            db.execute(
                """INSERT INTO excluded_products(product_key, product_name, excluded_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(product_key) DO UPDATE SET product_name=excluded.product_name, excluded_at=excluded.excluded_at""",
                (key, names.get(key, key), now),
            )
    return len(keys)


def restore_excluded_products(product_keys: list[str]) -> int:
    keys = {str(key) for key in product_keys if str(key)}
    if not keys:
        return 0
    with db_session() as db:
        count = 0
        for key in keys:
            cursor = db.execute("DELETE FROM excluded_products WHERE product_key=?", (key,))
            count += max(0, cursor.rowcount)
    return count


def delete_product_group(canonical_key: str) -> None:
    """Remove a display grouping; original prices, history, and counts remain intact."""
    with db_session() as db:
        db.execute("DELETE FROM product_aliases WHERE canonical_key=?", (canonical_key,))


def record_order_counts(sheet: dict[str, Any]) -> int:
    """Replace one date's counts with occurrences of each item in the uploaded sheet.

    Each product row is one order occurrence (the quantity column is not used).
    Re-uploading a corrected workbook for the same date replaces that date's counts,
    so retries do not inflate cumulative totals.
    """
    order_date = str(sheet.get("date") or "")
    if not order_date:
        raise ValueError("订单统计需要有有效日期。")

    counts: dict[str, dict[str, Any]] = {}
    for item in sheet.get("rows", []):
        key = str(item.get("raw_key") or item.get("key") or "").strip()
        item_name = str(item.get("raw_name") or item.get("canonical_name") or item.get("product") or key)
        if not key or is_ignored_product(item_name):
            continue
        if key not in counts:
            counts[key] = {
                "name": item_name,
                "count": 0,
            }
        counts[key]["count"] += 1

    with db_session() as db:
        db.execute("DELETE FROM order_daily_counts WHERE order_date=?", (order_date,))
        excluded_keys = {row["product_key"] for row in db.execute("SELECT product_key FROM excluded_products").fetchall()}
        for key, item in counts.items():
            if key in excluded_keys:
                continue
            db.execute("""
                INSERT INTO order_daily_counts(order_date, product_key, product_name, order_count)
                VALUES (?, ?, ?, ?)
            """, (order_date, key, item["name"], item["count"]))
    return sum(item["count"] for key, item in counts.items() if key not in excluded_keys)


def list_history(limit: int | None = 30, days: int = 7) -> list[dict[str, Any]]:
    cutoff = (datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=max(days - 1, 0))).strftime("%Y-%m-%d") + " 00:00:00"
    with db_session() as db:
        if limit is None:
            recent_rows = db.execute(
                "SELECT * FROM price_history WHERE updated_at>=? ORDER BY updated_at, id",
                (cutoff,),
            ).fetchall()
        else:
            fetch_limit = max(limit * 50, limit + 500)
            recent_rows = db.execute(
                """SELECT * FROM (
                       SELECT * FROM price_history WHERE updated_at>=?
                       ORDER BY updated_at DESC, id DESC LIMIT ?
                   ) AS latest_window ORDER BY updated_at, id""",
                (cutoff, fetch_limit),
            ).fetchall()
        aliases = {
            row["alias_key"]: dict(row)
            for row in db.execute("SELECT alias_key, canonical_key, canonical_name FROM product_aliases").fetchall()
        }
        excluded_keys = {row["product_key"] for row in db.execute("SELECT product_key FROM excluded_products").fetchall()}
        usable_recent = []
        relevant_groups = set()
        for raw in recent_rows:
            row = dict(raw)
            alias = aliases.get(row["product_key"])
            group_key = alias["canonical_key"] if alias else row["product_key"]
            group_name = alias["canonical_name"] if alias else row["product_name"]
            if row["product_key"] in excluded_keys or is_ignored_product(row["product_name"]) or is_ignored_product(group_name):
                continue
            usable_recent.append((row, group_key, group_name))
            relevant_groups.add(group_key)

        relevant_raw_keys = {
            alias_key for alias_key, alias in aliases.items()
            if alias["canonical_key"] in relevant_groups and alias_key not in excluded_keys
        }
        relevant_raw_keys.update(row["product_key"] for row, _group_key, _group_name in usable_recent)
        baseline_rows = []
        if relevant_raw_keys:
            keys = sorted(relevant_raw_keys)
            for offset in range(0, len(keys), 900):
                chunk = keys[offset:offset + 900]
                placeholders = ",".join("?" for _ in chunk)
                baseline_rows.extend(db.execute(
                    f"""SELECT id, product_key, product_name, supplier_quote_jpy, source_date, updated_at, source, updated_by
                        FROM (
                            SELECT id, product_key, product_name, supplier_quote_jpy, source_date, updated_at, source, updated_by,
                                   ROW_NUMBER() OVER (PARTITION BY product_key ORDER BY updated_at DESC, id DESC) AS row_num
                            FROM price_history
                            WHERE updated_at < ? AND product_key IN ({placeholders})
                        ) AS recent_before
                        WHERE row_num=1""",
                    (cutoff, *chunk),
                ).fetchall())

    def group_for(row):
        alias = aliases.get(row["product_key"])
        return (alias["canonical_key"], alias["canonical_name"]) if alias else (row["product_key"], row["product_name"])

    previous_quote: dict[str, float] = {}
    previous_stamp: dict[str, tuple[str, int]] = {}
    for raw in baseline_rows:
        row = dict(raw)
        if row["product_key"] in excluded_keys or is_ignored_product(row["product_name"]):
            continue
        key, name = group_for(row)
        if is_ignored_product(name):
            continue
        stamp = (row["updated_at"], int(row["id"]))
        if key not in previous_stamp or stamp > previous_stamp[key]:
            previous_quote[key] = float(row["supplier_quote_jpy"])
            previous_stamp[key] = stamp

    events: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
    for row, key, name in usable_recent:
        row["product_key"] = key
        row["product_name"] = name
        events[key].append((row, name))

    changes = []
    for key, group_events in events.items():
        quote_before_window = previous_quote.get(key)
        last_quote = quote_before_window
        for row, _name in group_events:
            quote = float(row["supplier_quote_jpy"])
            if last_quote is None or quote != last_quote:
                changes.append(row)
            last_quote = quote
    changes.sort(key=lambda row: (row["updated_at"], int(row["id"])), reverse=True)
    # The landing panel shows one most-recent entry per product; the item detail
    # view remains the place to inspect all of its individual changes.
    latest_by_product = {}
    for row in changes:
        latest_by_product.setdefault(row["product_key"], row)
    latest = list(latest_by_product.values())
    return latest if limit is None else latest[:limit]


def list_product_history(product_key: str, member_keys: list[str] | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Return distinct quote changes for one visible catalog row, newest first."""
    with db_session() as db:
        aliases = [dict(row) for row in db.execute(
            "SELECT alias_key, alias_name, canonical_key, canonical_name FROM product_aliases"
        ).fetchall()]
        alias_map = {row["alias_key"]: row for row in aliases}
        canonical_key = alias_map.get(product_key, {}).get("canonical_key", product_key)
        canonical_name = alias_map.get(product_key, {}).get("canonical_name", "")
        keys = set(member_keys or [product_key])
        keys.update(row["alias_key"] for row in aliases if row["canonical_key"] == canonical_key)
        excluded = {row["product_key"] for row in db.execute("SELECT product_key FROM excluded_products").fetchall()}
        keys.difference_update(excluded)
        if not keys:
            return []
        placeholders = ",".join("?" for _ in keys)
        rows = db.execute(
            f"SELECT * FROM price_history WHERE product_key IN ({placeholders}) ORDER BY updated_at DESC, id DESC LIMIT ?",
            (*sorted(keys), max(limit * 20, limit + 200)),
        ).fetchall()

    result = []
    last_quote: dict[str, float] = {}
    for raw in rows:
        row = dict(raw)
        if is_ignored_product(row.get("product_name")):
            continue
        alias = alias_map.get(row["product_key"])
        if alias:
            row["product_key"] = alias["canonical_key"]
            row["product_name"] = alias["canonical_name"]
        elif canonical_name and row["product_key"] in keys:
            row["product_name"] = canonical_name
        group_key = row["product_key"]
        quote = float(row["supplier_quote_jpy"])
        if group_key in last_quote and last_quote[group_key] == quote:
            continue
        last_quote[group_key] = quote
        result.append(row)
        if len(result) >= limit:
            break
    return result


def _record_change(db: _Connection, key: str, name: str, quote: float,
                   source_date: str, source: str, now: str, updated_by: str) -> None:
    db.execute("""
        INSERT INTO price_history(product_key, product_name, supplier_quote_jpy, source_date, updated_at, source, updated_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (key, name, quote, source_date, now, source, updated_by))


def apply_sheet(sheet: dict[str, Any], updated_by: str = "") -> dict[str, int]:
    """Import prices while recording only actual quote changes in price history."""
    changed = skipped = unchanged = 0
    now = _now_text()
    with db_session() as db:
        excluded_keys = {row["product_key"] for row in db.execute("SELECT product_key FROM excluded_products").fetchall()}
        for item in sheet["rows"]:
            key = item.get("raw_key") or item["key"]
            quote = item["fishmonger_quote_jpy"]
            raw_name = item.get("raw_name") or item.get("canonical_name") or item.get("product", "")
            if not key or quote is None or key in excluded_keys or is_ignored_product(raw_name):
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
            name = raw_name
            if old and old["supplier_quote_jpy"] == quote:
                db.execute("""
                    UPDATE price_catalog SET product_name=?, source_date=?, updated_at=?, source='价格表', updated_by=?
                    WHERE product_key=?
                """, (name, sheet["date"], now, updated_by, key))
                unchanged += 1
                continue
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
    return {"changed": changed, "unchanged": unchanged, "skipped": skipped}


def manual_update(key: str, quote: float, updated_by: str = "", member_keys: list[str] | None = None) -> None:
    now = _now_text()
    with db_session() as db:
        keys = sorted(set(member_keys or [key]))
        for raw_key in keys:
            old = db.execute("SELECT * FROM price_catalog WHERE product_key=?", (raw_key,)).fetchone()
            if not old or float(old["supplier_quote_jpy"]) == float(quote):
                continue
            name = old["product_name"]
            db.execute("""
                UPDATE price_catalog SET supplier_quote_jpy=?, updated_at=?, source='手动修改', updated_by=?
                WHERE product_key=?
            """, (quote, now, updated_by, raw_key))
            _record_change(db, raw_key, name, quote, old["source_date"], "手动修改", now, updated_by)


def export_backup_xlsx() -> bytes:
    wb = Workbook()
    catalog = wb.active
    catalog.title = "当前价格"
    catalog.append(["product_key", "product_name", "supplier_quote_jpy", "source_date", "updated_at", "source", "updated_by"])
    for row in list_raw_prices():
        catalog.append([row.get(k) for k in ("product_key", "product_name", "supplier_quote_jpy", "source_date", "updated_at", "source", "updated_by")])
    history = wb.create_sheet("更新历史")
    history.append(["product_key", "product_name", "supplier_quote_jpy", "source_date", "updated_at", "source", "updated_by"])
    for row in reversed(list_raw_history()):
        history.append([row.get(k) for k in ("product_key", "product_name", "supplier_quote_jpy", "source_date", "updated_at", "source", "updated_by")])
    counts = wb.create_sheet("每日下单统计")
    counts.append(["order_date", "product_key", "product_name", "order_count"])
    for row in list_order_counts():
        counts.append([row.get(k) for k in ("order_date", "product_key", "product_name", "order_count")])
    groups = wb.create_sheet("品名归并")
    groups.append(["alias_key", "alias_name", "canonical_key", "canonical_name"])
    for row in list_alias_rows():
        groups.append([row.get(k) for k in ("alias_key", "alias_name", "canonical_key", "canonical_name")])
    excluded = wb.create_sheet("已忽略品种")
    excluded.append(["product_key", "product_name", "excluded_at"])
    for row in list_excluded_products():
        excluded.append([row.get(k) for k in ("product_key", "product_name", "excluded_at")])
    stream = BytesIO()
    wb.save(stream)
    return stream.getvalue()


def list_raw_prices() -> list[dict[str, Any]]:
    with db_session() as db:
        rows = db.execute("SELECT * FROM price_catalog ORDER BY LOWER(product_name)").fetchall()
    return [dict(row) for row in rows]


def list_raw_history() -> list[dict[str, Any]]:
    with db_session() as db:
        rows = db.execute("SELECT * FROM price_history ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def list_order_counts() -> list[dict[str, Any]]:
    with db_session() as db:
        rows = db.execute("""
            SELECT order_date, product_key, product_name, order_count
            FROM order_daily_counts
            ORDER BY order_date, product_key
        """).fetchall()
    return [dict(row) for row in rows]


def list_alias_rows() -> list[dict[str, Any]]:
    with db_session() as db:
        rows = db.execute("""
            SELECT alias_key, alias_name, canonical_key, canonical_name
            FROM product_aliases ORDER BY canonical_name, alias_name
        """).fetchall()
    return [dict(row) for row in rows]


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
        if "每日下单统计" in wb.sheetnames:
            counts_ws = wb["每日下单统计"]
            count_headers = [cell.value for cell in counts_ws[1]]
            for values in counts_ws.iter_rows(min_row=2, values_only=True):
                if not any(v is not None for v in values):
                    continue
                row = dict(zip(count_headers, values))
                db.execute("""
                    INSERT INTO order_daily_counts(order_date, product_key, product_name, order_count)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(order_date, product_key) DO UPDATE SET
                        product_name=excluded.product_name,
                        order_count=excluded.order_count
                """, (
                    str(row.get("order_date") or ""), str(row.get("product_key") or ""),
                    str(row.get("product_name") or ""), int(row.get("order_count") or 0),
                ))
        if "品名归并" in wb.sheetnames:
            groups_ws = wb["品名归并"]
            group_headers = [cell.value for cell in groups_ws[1]]
            for values in groups_ws.iter_rows(min_row=2, values_only=True):
                if not any(v is not None for v in values):
                    continue
                row = dict(zip(group_headers, values))
                db.execute("""
                    INSERT INTO product_aliases(alias_key, alias_name, canonical_key, canonical_name)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(alias_key) DO UPDATE SET
                        alias_name=excluded.alias_name,
                        canonical_key=excluded.canonical_key,
                        canonical_name=excluded.canonical_name
                """, tuple(str(row.get(k) or "") for k in ("alias_key", "alias_name", "canonical_key", "canonical_name")))
        if "已忽略品种" in wb.sheetnames:
            excluded_ws = wb["已忽略品种"]
            excluded_headers = [cell.value for cell in excluded_ws[1]]
            for values in excluded_ws.iter_rows(min_row=2, values_only=True):
                if not any(v is not None for v in values):
                    continue
                row = dict(zip(excluded_headers, values))
                db.execute(
                    "INSERT INTO excluded_products(product_key, product_name, excluded_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(product_key) DO NOTHING",
                    (str(row.get("product_key") or ""), str(row.get("product_name") or ""), str(row.get("excluded_at") or "")),
                )
    return len(records)
