"""Read Exiros CSV/Excel exports, detect their columns and load them into SQLite.

Exiros exports don't have a fixed layout we can rely on, so every field has a
list of header aliases. The upload screen shows the guessed mapping and lets
the user correct it before anything is written.
"""

import csv
import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    required: bool
    aliases: tuple[str, ...]


SALES_FIELDS = (
    Field("date", "Date", True, ("date", "invoice date", "inv date", "doc date", "document date", "transaction date", "trans date", "posting date", "sale date", "order date")),
    Field("invoice", "Invoice / document no.", False, ("invoice", "invoice no", "invoice number", "inv no", "inv", "invoice #", "doc no", "document no", "document number", "voucher no", "bill no", "order no", "reference", "ref no")),
    Field("customer", "Customer", True, ("customer", "customer name", "client", "client name", "account", "account name", "party", "party name", "buyer", "customer code")),
    Field("product_code", "Item code", True, ("item code", "product code", "sku", "item no", "item number", "item", "product", "part no", "part number", "material", "code", "barcode")),
    Field("product_name", "Item description", False, ("item name", "product name", "description", "item description", "product description", "item desc", "desc", "name")),
    Field("quantity", "Quantity", True, ("quantity", "qty", "qty sold", "units", "sold qty", "quantity sold", "pcs")),
    Field("unit_price", "Unit price", False, ("unit price", "price", "rate", "selling price", "sale price", "unit rate")),
    Field("amount", "Line amount", False, ("amount", "net amount", "line total", "total", "total amount", "value", "net value", "sales value", "sales amount", "line amount", "revenue", "net sales")),
    Field("unit_cost", "Unit cost", False, ("unit cost", "cost", "cost price", "avg cost", "average cost", "purchase price")),
    Field("salesperson", "Salesperson", False, ("salesperson", "sales person", "salesman", "sales rep", "rep", "agent", "sales agent")),
)

INVENTORY_FIELDS = (
    Field("product_code", "Item code", True, ("item code", "product code", "sku", "item no", "item number", "item", "product", "part no", "part number", "material", "code", "barcode")),
    Field("product_name", "Item description", False, ("item name", "product name", "description", "item description", "product description", "item desc", "desc", "name")),
    Field("category", "Category", False, ("category", "item category", "group", "item group", "product group", "class", "family", "brand")),
    Field("on_hand", "Quantity on hand", True, ("on hand", "qty on hand", "stock", "stock qty", "quantity", "qty", "available", "available qty", "balance", "balance qty", "closing stock", "closing qty", "soh")),
    Field("unit_cost", "Unit cost", False, ("unit cost", "cost", "cost price", "avg cost", "average cost", "purchase price")),
    Field("supplier", "Supplier", False, ("supplier", "vendor", "supplier name", "vendor name", "manufacturer")),
    Field("lead_time_days", "Lead time (days)", False, ("lead time", "lead time days", "leadtime", "lt days")),
)

DATASETS = {"sales": SALES_FIELDS, "inventory": INVENTORY_FIELDS}


class ImportFailed(Exception):
    """Raised when a file can't be imported; the message is shown to the user."""


def normalize(header: object) -> str:
    text = str(header).strip().lower().replace("#", " no ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def read_raw(path: Path) -> pd.DataFrame:
    """Read the whole sheet without assuming where the header row is."""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(path, header=None, dtype=object)
    if suffix in (".csv", ".txt"):
        data = path.read_bytes()
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        # Title rows above the header have fewer cells than the data, which
        # pandas' CSV parser rejects, so read rows ourselves and pad them.
        lines = [line for line in text.splitlines() if line.strip()]
        sample = "\n".join(lines[:50])
        delimiter = max([",", ";", "\t", "|"], key=sample.count)
        rows = list(csv.reader(lines, delimiter=delimiter))
        width = max((len(r) for r in rows), default=0)
        return pd.DataFrame([r + [None] * (width - len(r)) for r in rows], dtype=object).replace("", None)
    raise ImportFailed(f"Unsupported file type '{suffix}'. Upload a .csv or .xlsx export.")


def _alias_score(headers: list[str], fields: tuple[Field, ...]) -> int:
    normalized = {normalize(h) for h in headers}
    return sum(1 for f in fields if any(normalize(a) in normalized for a in f.aliases))


def find_header_row(raw: pd.DataFrame, fields: tuple[Field, ...], scan: int = 15) -> int:
    """ERP exports often start with a title block; pick the row that looks most like a header."""
    best_row, best_score = 0, -1
    for i in range(min(scan, len(raw))):
        score = _alias_score([v for v in raw.iloc[i].tolist() if pd.notna(v)], fields)
        if score > best_score:
            best_row, best_score = i, score
    return best_row


def load_table(path: Path, dataset: str) -> pd.DataFrame:
    fields = DATASETS[dataset]
    raw = read_raw(path).dropna(how="all")
    if raw.empty:
        raise ImportFailed("The file is empty.")
    raw = raw.reset_index(drop=True)
    header_row = find_header_row(raw, fields)
    headers = []
    for i, value in enumerate(raw.iloc[header_row].tolist()):
        name = str(value).strip() if pd.notna(value) and str(value).strip() else f"Column {i + 1}"
        while name in headers:
            name += " (2)"
        headers.append(name)
    table = raw.iloc[header_row + 1 :].copy()
    table.columns = headers
    return table.dropna(how="all").reset_index(drop=True)


def guess_mapping(columns: list[str], dataset: str) -> dict[str, str | None]:
    """Map each field to a column, preferring earlier (more specific) aliases."""
    by_norm = {normalize(c): c for c in columns}
    used: set[str] = set()
    mapping: dict[str, str | None] = {}
    for f in DATASETS[dataset]:
        mapping[f.name] = None
        for alias in f.aliases:
            column = by_norm.get(normalize(alias))
            if column and column not in used:
                mapping[f.name] = column
                used.add(column)
                break
    return mapping


def parse_number(series: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(series):
        return series.astype(float)
    text = series.astype(str).str.strip()
    negative = text.str.match(r"^\(.*\)$") | text.str.endswith("-")
    # "2,50" is a decimal comma; "1,234" stays a thousands separator.
    text = text.str.replace(r"^([^.,]*\d),(\d{1,2})\)?-?$", r"\1.\2", regex=True)
    cleaned = text.str.replace(r"[^0-9.\-]", "", regex=True).str.rstrip("-")
    numbers = pd.to_numeric(cleaned, errors="coerce")
    return numbers.where(~negative, -numbers.abs())


def parse_dates(series: pd.Series, date_order: str = "auto") -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    values = series.where(series.notna(), None)
    if all(hasattr(v, "year") for v in values.dropna()):
        return pd.to_datetime(values, errors="coerce")
    text = values.astype(str).str.strip()

    def attempt(dayfirst: bool) -> pd.Series:
        return pd.to_datetime(text, errors="coerce", dayfirst=dayfirst, format="mixed")

    if date_order == "day_first":
        return attempt(True)
    if date_order == "month_first":
        return attempt(False)
    day, month = attempt(True), attempt(False)
    # Pick whichever reading parses more rows; on a tie the two only differ for
    # dates like 03/04, and day-first is the more common convention.
    return month if month.notna().sum() > day.notna().sum() else day


@dataclass
class ImportResult:
    rows_imported: int
    rows_skipped: int
    warnings: list[str] = field(default_factory=list)


def _text(series: pd.Series) -> pd.Series:
    return series.map(lambda v: None if pd.isna(v) or str(v).strip() == "" else str(v).strip())


def prepare(table: pd.DataFrame, dataset: str, mapping: dict[str, str | None], date_order: str = "auto") -> tuple[pd.DataFrame, list[str]]:
    """Convert a mapped table into clean rows; returns rows and warnings."""
    fields = DATASETS[dataset]
    missing = [f.label for f in fields if f.required and not mapping.get(f.name)]
    if dataset == "sales" and not (mapping.get("amount") or mapping.get("unit_price")):
        missing.append("Line amount or Unit price")
    if missing:
        raise ImportFailed("Map these required columns before importing: " + ", ".join(missing))

    def col(name: str) -> pd.Series | None:
        column = mapping.get(name)
        return table[column] if column else None

    out = pd.DataFrame(index=table.index)
    warnings: list[str] = []
    if dataset == "sales":
        out["date"] = parse_dates(col("date"), date_order)
        for name in ("invoice", "customer", "product_code", "product_name", "salesperson"):
            out[name] = _text(col(name)) if col(name) is not None else None
        out["quantity"] = parse_number(col("quantity"))
        if col("amount") is not None:
            out["amount"] = parse_number(col("amount"))
        else:
            out["amount"] = parse_number(col("unit_price")) * out["quantity"]
        out["unit_cost"] = parse_number(col("unit_cost")) if col("unit_cost") is not None else None
        required = ["date", "customer", "product_code", "quantity", "amount"]
        if col("unit_cost") is None:
            warnings.append("No cost column mapped, so gross profit and margin won't be shown.")
    else:
        for name in ("product_code", "product_name", "category", "supplier"):
            out[name] = _text(col(name)) if col(name) is not None else None
        out["on_hand"] = parse_number(col("on_hand"))
        for name in ("unit_cost", "lead_time_days"):
            out[name] = parse_number(col(name)) if col(name) is not None else None
        required = ["product_code", "on_hand"]

    valid = out[required].notna().all(axis=1)
    if dataset == "sales":
        # Skip subtotal/total rows that exports often append.
        totals = out["customer"].fillna("").str.lower().str.match(r"^(grand )?(sub)?total")
        valid &= ~totals
    skipped = int((~valid).sum())
    if skipped:
        warnings.append(f"{skipped} row(s) skipped because a required value was blank or unreadable.")
    out = out[valid]
    if dataset == "sales":
        out = out.assign(date=out["date"].dt.strftime("%Y-%m-%d"))
    else:
        duplicates = out["product_code"].duplicated(keep="last")
        if duplicates.any():
            warnings.append(f"{int(duplicates.sum())} duplicate item code(s); kept the last row for each.")
            out = out[~duplicates]
    return out, warnings


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def import_file(
    conn: sqlite3.Connection,
    path: Path,
    dataset: str,
    mapping: dict[str, str | None],
    *,
    filename: str | None = None,
    date_order: str = "auto",
    replace: bool = False,
) -> ImportResult:
    """Import a file. Inventory always replaces the current snapshot; sales append unless replace=True."""
    table = load_table(path, dataset)
    rows, warnings = prepare(table, dataset, mapping, date_order)
    if rows.empty:
        raise ImportFailed("No usable rows found. Check the column mapping.")
    digest = file_hash(path)
    if dataset == "sales" and not replace:
        seen = conn.execute(
            "SELECT 1 FROM imports i WHERE dataset = 'sales' AND file_hash = ? AND EXISTS (SELECT 1 FROM sales s WHERE s.import_id = i.id)",
            (digest,),
        ).fetchone()
        if seen:
            raise ImportFailed("This exact file was already imported. Choose 'Replace all sales data' to reload it.")

    skipped = len(table) - len(rows)
    with conn:
        cur = conn.execute(
            "INSERT INTO imports (dataset, filename, file_hash, rows_imported, rows_skipped) VALUES (?, ?, ?, ?, ?)",
            (dataset, filename or path.name, digest, len(rows), skipped),
        )
        import_id = cur.lastrowid
        records = rows.astype(object).where(rows.notna(), None)
        if dataset == "sales":
            if replace:
                conn.execute("DELETE FROM sales")
            cols = ["date", "invoice", "customer", "product_code", "product_name", "quantity", "amount", "unit_cost", "salesperson"]
            conn.executemany(
                f"INSERT INTO sales (import_id, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
                [(import_id, *r) for r in records[cols].itertuples(index=False)],
            )
        else:
            conn.execute("DELETE FROM inventory")
            cols = ["product_code", "product_name", "category", "on_hand", "unit_cost", "supplier", "lead_time_days"]
            conn.executemany(
                f"INSERT INTO inventory (import_id, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
                [(import_id, *r) for r in records[cols].itertuples(index=False)],
            )
    return ImportResult(len(rows), skipped, warnings)
