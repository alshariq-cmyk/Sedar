"""Sedar web app: upload Exiros exports and see what they say about the business."""

import io
import json
import secrets
import shutil
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import analytics, importer
from .db import DEFAULT_DB_PATH, connect

HERE = Path(__file__).parent


def create_app(db_path: Path | str | None = None, upload_dir: Path | str | None = None) -> FastAPI:
    app = FastAPI(title="Sedar")
    conn = connect(db_path)
    uploads = Path(upload_dir or Path(db_path or DEFAULT_DB_PATH).parent / "uploads")
    uploads.mkdir(parents=True, exist_ok=True)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["money"] = lambda v: "–" if v is None or pd.isna(v) else f"{v:,.0f}"
    templates.env.filters["num"] = lambda v, d=0: "–" if v is None or pd.isna(v) else f"{v:,.{d}f}"
    templates.env.filters["pct"] = lambda v: "–" if v is None or pd.isna(v) else f"{v:+.0f}%"
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def render(request: Request, name: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context)

    def load():
        sales = analytics.sales_frame(conn)
        inventory = analytics.inventory_frame(conn)
        return sales, inventory

    def empty_redirect():
        return RedirectResponse("/import?msg=Import+a+sales+export+to+get+started", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        sales, inventory = load()
        if sales.empty:
            return empty_redirect()
        customers = analytics.customer_table(sales)
        products = analytics.product_table(sales, inventory)
        return render(
            request,
            "dashboard.html",
            page="dashboard",
            kpis=analytics.kpis(sales),
            trend=json.dumps(analytics.monthly_trend(sales)),
            top_customers=analytics.top_customers(sales),
            top_products=products.head(10).to_dict("records"),
            insights=analytics.insights(sales, customers, products, not inventory.empty),
            stock=analytics.inventory_summary(products) if not inventory.empty else None,
        )

    @app.get("/customers", response_class=HTMLResponse)
    def customers_page(request: Request, status: str | None = None):
        sales, _ = load()
        if sales.empty:
            return empty_redirect()
        table = analytics.customer_table(sales)
        counts = table["status"].value_counts().to_dict()
        if status:
            table = table[table["status"] == status]
        return render(request, "customers.html", page="customers", rows=table.to_dict("records"), counts=counts, status=status, as_of=analytics.as_of(sales))

    @app.get("/inventory", response_class=HTMLResponse)
    def inventory_page(request: Request, status: str | None = None):
        sales, inventory = load()
        if sales.empty and inventory.empty:
            return empty_redirect()
        table = analytics.product_table(sales, inventory)
        counts = table["status"].value_counts().to_dict()
        summary = analytics.inventory_summary(table) if not inventory.empty else None
        if status:
            table = table[table["status"] == status]
        return render(
            request,
            "inventory.html",
            page="inventory",
            rows=table.to_dict("records"),
            counts=counts,
            status=status,
            has_inventory=not inventory.empty,
            summary=summary,
        )

    def excel(frames: dict[str, pd.DataFrame], filename: str) -> StreamingResponse:
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer) as writer:
            for sheet, frame in frames.items():
                frame.to_excel(writer, sheet_name=sheet, index=False)
        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/export/reorder.xlsx")
    def export_reorder():
        sales, inventory = load()
        products = analytics.product_table(sales, inventory)
        reorder = products[products["status"].isin(["Out of stock", "Reorder now"])]
        columns = ["supplier", "product_code", "product_name", "abc", "on_hand", "daily_velocity", "days_of_cover", "lead_time_days", "suggested_order", "unit_cost", "status"]
        reorder = reorder[columns].sort_values(["supplier", "abc", "product_code"], na_position="last")
        reorder = reorder.assign(order_value=reorder["suggested_order"] * reorder["unit_cost"])
        return excel({"Reorder": reorder}, "reorder_list.xlsx")

    @app.get("/export/customers.xlsx")
    def export_customers():
        sales, _ = load()
        return excel({"Customers": analytics.customer_table(sales)}, "customers.xlsx")

    @app.get("/export/products.xlsx")
    def export_products():
        sales, inventory = load()
        return excel({"Products": analytics.product_table(sales, inventory)}, "products.xlsx")

    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request, msg: str | None = None, error: str | None = None):
        history = [dict(r) for r in conn.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 20")]
        counts = {
            "sales": conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0],
            "inventory": conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0],
        }
        return render(request, "import.html", page="import", msg=msg, error=error, history=history, counts=counts)

    @app.post("/import/upload", response_class=HTMLResponse)
    def upload(request: Request, dataset: str = Form(...), file: UploadFile = File(...)):
        if dataset not in importer.DATASETS:
            return RedirectResponse("/import?error=Unknown+data+type", status_code=303)
        suffix = Path(file.filename or "").suffix.lower()
        token = secrets.token_hex(8)
        path = uploads / f"{token}{suffix}"
        with path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        try:
            table = importer.load_table(path, dataset)
        except Exception as exc:  # show any parsing failure to the user rather than a 500
            path.unlink(missing_ok=True)
            return RedirectResponse(f"/import?error={quote_plus(f'Could not read {file.filename}: {exc}')}", status_code=303)
        return render(
            request,
            "mapping.html",
            page="import",
            token=token,
            suffix=suffix,
            filename=file.filename,
            dataset=dataset,
            fields=importer.DATASETS[dataset],
            columns=list(table.columns),
            mapping=importer.guess_mapping(list(table.columns), dataset),
            preview=table.head(8).fillna("").to_dict("records"),
            row_count=len(table),
        )

    @app.post("/import/confirm")
    async def confirm(request: Request):
        form = await request.form()
        token, suffix, dataset = form.get("token", ""), form.get("suffix", ""), form.get("dataset", "")
        if not token.isalnum() or suffix not in (".csv", ".txt", ".xlsx", ".xlsm", ".xls") or dataset not in importer.DATASETS:
            return RedirectResponse("/import?error=Invalid+upload", status_code=303)
        path = uploads / f"{token}{suffix}"
        if not path.exists():
            return RedirectResponse("/import?error=Upload+expired,+please+upload+again", status_code=303)
        mapping = {f.name: (form.get(f"map_{f.name}") or None) for f in importer.DATASETS[dataset]}
        try:
            result = importer.import_file(
                conn, path, dataset, mapping,
                filename=form.get("filename") or path.name,
                date_order=form.get("date_order", "auto"),
                replace=form.get("mode") == "replace",
            )
        except importer.ImportFailed as exc:
            return RedirectResponse(f"/import?error={quote_plus(str(exc))}", status_code=303)
        path.unlink(missing_ok=True)
        message = f"Imported {result.rows_imported:,} {dataset} rows. " + " ".join(result.warnings)
        return RedirectResponse(f"/import?msg={quote_plus(message)}", status_code=303)

    @app.post("/import/{import_id}/delete")
    def delete_import(import_id: int):
        with conn:
            conn.execute("DELETE FROM sales WHERE import_id = ?", (import_id,))
            conn.execute("DELETE FROM inventory WHERE import_id = ?", (import_id,))
            conn.execute("DELETE FROM imports WHERE id = ?", (import_id,))
        return RedirectResponse("/import?msg=Import+removed", status_code=303)

    return app


app = create_app()
