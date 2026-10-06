"""SQLite storage for imported Exiros data."""

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(os.environ.get("SEDAR_DB", "data/sedar.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    rows_imported INTEGER NOT NULL,
    rows_skipped INTEGER NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    invoice TEXT,
    customer TEXT NOT NULL,
    product_code TEXT NOT NULL,
    product_name TEXT,
    quantity REAL NOT NULL,
    amount REAL NOT NULL,
    unit_cost REAL,
    salesperson TEXT
);
CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(date);
CREATE INDEX IF NOT EXISTS idx_sales_customer ON sales(customer);
CREATE INDEX IF NOT EXISTS idx_sales_product ON sales(product_code);

CREATE TABLE IF NOT EXISTS inventory (
    product_code TEXT PRIMARY KEY,
    product_name TEXT,
    category TEXT,
    on_hand REAL NOT NULL,
    unit_cost REAL,
    supplier TEXT,
    lead_time_days REAL,
    import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE
);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or DEFAULT_DB_PATH)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
