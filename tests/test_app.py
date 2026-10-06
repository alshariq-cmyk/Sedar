from fastapi.testclient import TestClient

from sedar.main import create_app


def upload(client, path, dataset):
    with open(path, "rb") as f:
        res = client.post("/import/upload", data={"dataset": dataset}, files={"file": (path.name, f)})
    assert res.status_code == 200 and "Check the columns" in res.text
    token = res.text.split('name="token" value="')[1].split('"')[0]
    suffix = res.text.split('name="suffix" value="')[1].split('"')[0]
    return token, suffix


def test_full_flow(tmp_path, sample_files):
    client = TestClient(create_app(tmp_path / "test.db"))
    assert client.get("/", follow_redirects=False).status_code == 303

    sales_path, stock_path = sample_files
    form = {"dataset": "sales", "filename": sales_path.name, "date_order": "auto", "mode": "append",
            "map_date": "Inv Date", "map_invoice": "Invoice No", "map_customer": "Customer Name",
            "map_product_code": "Item Code", "map_product_name": "Description", "map_quantity": "Qty",
            "map_unit_price": "Rate", "map_amount": "Net Amount", "map_unit_cost": "Cost Price", "map_salesperson": "Salesman"}
    token, suffix = upload(client, sales_path, "sales")
    res = client.post("/import/confirm", data={**form, "token": token, "suffix": suffix})
    assert "Imported" in res.text

    token, suffix = upload(client, stock_path, "inventory")
    res = client.post("/import/confirm", data={"dataset": "inventory", "token": token, "suffix": suffix,
                                               "map_product_code": "Item Code", "map_on_hand": "Closing Stock"})
    assert "Imported 17 inventory rows" in res.text

    for url in ("/", "/customers", "/customers?status=At+risk", "/inventory", "/inventory?status=Reorder+now"):
        res = client.get(url)
        assert res.status_code == 200, url
    assert "What needs your attention" in client.get("/").text
    assert "Blue Bay Hotel" in client.get("/customers?status=At+risk").text

    res = client.get("/export/reorder.xlsx")
    assert res.status_code == 200 and res.content[:2] == b"PK"


def test_bad_upload_shows_error(tmp_path):
    client = TestClient(create_app(tmp_path / "test.db"))
    res = client.post("/import/upload", data={"dataset": "sales"}, files={"file": ("x.pdf", b"%PDF")})
    assert "Unsupported file type" in res.text
