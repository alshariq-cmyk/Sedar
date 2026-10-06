"""Local SQLite store of Exiros RFQs, their lines, and update runs."""

import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

import rfq_to_excel

SCHEMA = """
CREATE TABLE IF NOT EXISTS rfqs (
    rfq TEXT PRIMARY KEY,
    title TEXT,
    client TEXT,
    deadline TEXT,              -- submission deadline, 'YYYY-MM-DD HH:MM' local time
    deadline_source TEXT,       -- 'portal' or 'manual'
    currency TEXT NOT NULL DEFAULT 'SAR',
    status TEXT NOT NULL,       -- Quoted / Partially quoted / Not quoted
    lines INTEGER NOT NULL,
    quoted_lines INTEGER NOT NULL,
    total REAL NOT NULL,
    earliest_needed TEXT,
    source_file TEXT,
    first_seen TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    seen_at TEXT                -- when the user first opened it; NULL means new
);

CREATE TABLE IF NOT EXISTS lines (
    rfq TEXT NOT NULL REFERENCES rfqs(rfq) ON DELETE CASCADE,
    line INTEGER,
    description TEXT,
    long_description TEXT,
    client TEXT,
    requested_qty REAL,
    uom TEXT,
    date_needed TEXT,
    unit_price REAL,
    quoted_qty REAL,
    line_total REAL,
    delivery_days REAL,
    brand TEXT,
    notes TEXT,
    status TEXT
);
CREATE INDEX IF NOT EXISTS idx_lines_rfq ON lines(rfq);

CREATE TABLE IF NOT EXISTS updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT NOT NULL,
    finished TEXT,
    result TEXT,                -- ok / failed / cancelled
    rfqs_found INTEGER,
    new_rfqs INTEGER,
    message TEXT
);
"""

LINE_COLUMNS = ["line", "description", "long_description", "client", "requested_qty", "uom", "date_needed",
                "unit_price", "quoted_qty", "line_total", "delivery_days", "brand", "notes", "status"]


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def rfq_status(lines: pd.DataFrame) -> str:
    quoted = int((lines["status"] != "Not quoted").sum())
    if quoted == 0:
        return "Not quoted"
    return "Quoted" if quoted == len(lines) else "Partially quoted"


def _clean(value):
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if hasattr(value, "item"):  # numpy scalar
        return value.item()
    return value


def save_rfq(conn: sqlite3.Connection, lines: pd.DataFrame, *, deadline: str | None = None,
             title: str | None = None, source_file: str | None = None) -> bool:
    """Insert or refresh one RFQ from parsed report lines. Returns True if the RFQ is new."""
    rfq = str(lines["rfq"].iloc[0])
    existing = conn.execute("SELECT * FROM rfqs WHERE rfq = ?", (rfq,)).fetchone()
    clients = "; ".join(dict.fromkeys(c for c in lines["client"] if c))
    needed = lines["date_needed"].min()
    values = {
        "client": clients,
        "status": rfq_status(lines),
        "lines": len(lines),
        "quoted_lines": int((lines["status"] != "Not quoted").sum()),
        "total": float(lines["line_total"].fillna(0).sum()),
        "earliest_needed": None if pd.isna(needed) else needed.strftime("%Y-%m-%d"),
        "source_file": source_file or (lines["source_file"].iloc[0] if "source_file" in lines else None),
        "last_updated": now(),
    }
    with conn:
        if existing:
            sets = dict(values)
            if title:
                sets["title"] = title
            # A deadline read from the portal replaces one typed in by hand.
            if deadline:
                sets["deadline"], sets["deadline_source"] = deadline, "portal"
            conn.execute(f"UPDATE rfqs SET {', '.join(f'{k} = ?' for k in sets)} WHERE rfq = ?", (*sets.values(), rfq))
        else:
            row = {"rfq": rfq, "title": title, "deadline": deadline, "deadline_source": "portal" if deadline else None,
                   "first_seen": now(), **values}
            conn.execute(f"INSERT INTO rfqs ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        conn.execute("DELETE FROM lines WHERE rfq = ?", (rfq,))
        conn.executemany(
            f"INSERT INTO lines (rfq, {', '.join(LINE_COLUMNS)}) VALUES (?, {', '.join('?' * len(LINE_COLUMNS))})",
            [(rfq, *(_clean(r[c]) for c in LINE_COLUMNS)) for r in lines.to_dict("records")],
        )
    return existing is None


def import_report(conn: sqlite3.Connection, path: Path, *, deadline: str | None = None,
                  title: str | None = None, rfq: str | None = None) -> tuple[str, bool]:
    lines = rfq_to_excel.read_report(path)
    if rfq:
        lines["rfq"] = rfq
    if lines.empty:
        raise ValueError(f"{path.name}: no RFQ lines found")
    is_new = save_rfq(conn, lines, deadline=deadline, title=title, source_file=path.name)
    return str(lines["rfq"].iloc[0]), is_new


def set_deadline(conn: sqlite3.Connection, rfq: str, deadline: str | None) -> None:
    with conn:
        conn.execute("UPDATE rfqs SET deadline = ?, deadline_source = ? WHERE rfq = ?",
                     (deadline or None, "manual" if deadline else None, rfq))


def mark_seen(conn: sqlite3.Connection, rfq: str) -> None:
    with conn:
        conn.execute("UPDATE rfqs SET seen_at = ? WHERE rfq = ? AND seen_at IS NULL", (now(), rfq))


def mark_all_seen(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("UPDATE rfqs SET seen_at = ? WHERE seen_at IS NULL", (now(),))


def start_update(conn: sqlite3.Connection) -> int:
    with conn:
        return conn.execute("INSERT INTO updates (started) VALUES (?)", (now(),)).lastrowid


def finish_update(conn: sqlite3.Connection, update_id: int, result: str, found: int = 0, new: int = 0, message: str = "") -> None:
    with conn:
        conn.execute("UPDATE updates SET finished = ?, result = ?, rfqs_found = ?, new_rfqs = ?, message = ? WHERE id = ?",
                     (now(), result, found, new, message, update_id))


def last_update(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM updates WHERE result = 'ok' ORDER BY id DESC LIMIT 1").fetchone()


def _blanks_as_none(df: pd.DataFrame) -> pd.DataFrame:
    """pandas reads SQL NULLs in text columns as NaN; templates and Excel want None."""
    return df.astype(object).where(df.notna(), None)


def rfqs_frame(conn: sqlite3.Connection) -> pd.DataFrame:
    return _blanks_as_none(pd.read_sql_query("SELECT * FROM rfqs", conn))


def lines_frame(conn: sqlite3.Connection, rfq: str | None = None) -> pd.DataFrame:
    if rfq:
        return _blanks_as_none(pd.read_sql_query("SELECT * FROM lines WHERE rfq = ? ORDER BY line", conn, params=(rfq,)))
    return _blanks_as_none(pd.read_sql_query("SELECT l.*, r.deadline FROM lines l JOIN rfqs r USING (rfq) ORDER BY rfq, line", conn))
