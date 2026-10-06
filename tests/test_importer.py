import pandas as pd
import pytest

from sedar import db, importer


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_detects_header_below_title_rows(sample_files):
    sales_path, stock_path = sample_files
    table = importer.load_table(sales_path, "sales")
    assert table.columns[0] == "Inv Date"
    mapping = importer.guess_mapping(list(table.columns), "sales")
    assert mapping["date"] == "Inv Date"
    assert mapping["customer"] == "Customer Name"
    assert mapping["amount"] == "Net Amount"
    assert mapping["unit_cost"] == "Cost Price"

    stock = importer.load_table(stock_path, "inventory")
    stock_mapping = importer.guess_mapping(list(stock.columns), "inventory")
    assert stock_mapping["on_hand"] == "Closing Stock"
    assert stock_mapping["lead_time_days"] == "Lead Time"


def test_parse_number_handles_erp_formats():
    values = pd.Series(["1,234.50", "AED 99", "(12.00)", "5-", "", "abc", "2,5", "1,234"])
    result = importer.parse_number(values).tolist()
    assert result[:4] == [1234.5, 99.0, -12.0, -5.0]
    assert pd.isna(result[4]) and pd.isna(result[5])
    assert result[6:] == [2.5, 1234.0]


def test_parse_dates_auto_detects_day_first():
    parsed = importer.parse_dates(pd.Series(["01/02/2026", "25/02/2026"]))
    assert parsed.dt.strftime("%Y-%m-%d").tolist() == ["2026-02-01", "2026-02-25"]
    parsed = importer.parse_dates(pd.Series(["02/25/2026", "12/31/2025"]))
    assert parsed.dt.strftime("%Y-%m-%d").tolist() == ["2026-02-25", "2025-12-31"]


def test_import_skips_totals_and_rejects_duplicate_file(sample_files):
    conn = db.connect(":memory:")
    sales_path, _ = sample_files
    table = importer.load_table(sales_path, "sales")
    mapping = importer.guess_mapping(list(table.columns), "sales")
    result = importer.import_file(conn, sales_path, "sales", mapping)
    assert result.rows_skipped == 1  # the Grand Total row
    count = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    assert count == result.rows_imported
    with pytest.raises(importer.ImportFailed):
        importer.import_file(conn, sales_path, "sales", mapping)
    importer.import_file(conn, sales_path, "sales", mapping, replace=True)
    assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == count


def test_amount_computed_from_unit_price(tmp_path):
    path = write(tmp_path, "s.csv", "Date;Customer;SKU;Qty;Price\n2026-01-05;Acme;A1;3;2,50\n")
    conn = db.connect(":memory:")
    mapping = importer.guess_mapping(list(importer.load_table(path, "sales").columns), "sales")
    assert mapping["unit_price"] == "Price"
    importer.import_file(conn, path, "sales", mapping)
    row = conn.execute("SELECT quantity, amount FROM sales").fetchone()
    assert row["quantity"] == 3 and row["amount"] == 7.5


def test_missing_required_columns_reports_labels(tmp_path):
    path = write(tmp_path, "s.csv", "Date,Customer,Qty\n2026-01-05,Acme,3\n")
    conn = db.connect(":memory:")
    mapping = importer.guess_mapping(list(importer.load_table(path, "sales").columns), "sales")
    with pytest.raises(importer.ImportFailed, match="Item code.*Line amount or Unit price"):
        importer.import_file(conn, path, "sales", mapping)


def test_inventory_import_replaces_snapshot(tmp_path):
    conn = db.connect(":memory:")
    first = write(tmp_path, "a.csv", "Item Code,Stock\nA,5\nB,7\n")
    second = write(tmp_path, "b.csv", "Item Code,Stock\nA,1\nA,2\n")
    for path in (first, second):
        mapping = importer.guess_mapping(list(importer.load_table(path, "inventory").columns), "inventory")
        result = importer.import_file(conn, path, "inventory", mapping)
    assert any("duplicate" in w for w in result.warnings)
    rows = conn.execute("SELECT product_code, on_hand FROM inventory").fetchall()
    assert [tuple(r) for r in rows] == [("A", 2.0)]
