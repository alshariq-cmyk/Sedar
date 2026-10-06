import pytest

from sedar import analytics, db, importer


@pytest.fixture(scope="module")
def loaded(sample_files):
    conn = db.connect(":memory:")
    for path, dataset in zip(sample_files, ("sales", "inventory")):
        mapping = importer.guess_mapping(list(importer.load_table(path, dataset).columns), dataset)
        importer.import_file(conn, path, dataset, mapping)
    sales = analytics.sales_frame(conn)
    inventory = analytics.inventory_frame(conn)
    return sales, inventory


def test_kpis_use_latest_data_date(loaded):
    sales, _ = loaded
    k = analytics.kpis(sales)
    assert k["as_of"].strftime("%Y-%m-%d") == "2026-09-30"
    assert k["current"]["revenue"] > 0
    assert 0 < k["current"]["margin"] < 50


def test_churned_customers_flagged_at_risk(loaded):
    sales, _ = loaded
    customers = analytics.customer_table(sales).set_index("customer")
    assert customers.loc["Blue Bay Hotel", "status"] == "At risk"
    assert customers.loc["Harbor Foods", "status"] == "At risk"
    assert customers.loc["Oasis Hypermarket", "status"] == "Active"


def test_inventory_statuses(loaded):
    sales, inventory = loaded
    products = analytics.product_table(sales, inventory).set_index("product_code")
    assert products.loc["OIL-5L", "status"] == "Out of stock"
    assert products.loc["OIL-5L", "suggested_order"] > 0
    assert products.loc["SAF-10G", "status"] == "Dead stock"
    assert products.loc["WTR-24", "status"] == "Overstock"
    assert products.loc["WTR-24", "suggested_order"] == 0
    assert set(products["abc"]) <= {"A", "B", "C"}


def test_works_without_inventory_or_cost(tmp_path):
    path = tmp_path / "s.csv"
    path.write_text("Date,Customer,Item,Qty,Amount\n2026-01-05,Acme,A1,3,30\n2026-02-05,Acme,A1,1,10\n2026-03-05,Beta,B1,2,50\n")
    conn = db.connect(":memory:")
    mapping = importer.guess_mapping(list(importer.load_table(path, "sales").columns), "sales")
    importer.import_file(conn, path, "sales", mapping)
    sales = analytics.sales_frame(conn)
    inventory = analytics.inventory_frame(conn)
    assert analytics.kpis(sales)["current"]["gross_profit"] is None
    products = analytics.product_table(sales, inventory)
    assert set(products["status"]) == {"Not in stock file"}
    items = analytics.insights(sales, analytics.customer_table(sales), products, has_inventory=False)
    assert any("stock-on-hand" in i["text"] for i in items)
