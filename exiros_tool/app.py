"""RFQ platform: a local dashboard of Exiros RFQs, deadlines and our quotes.

    python app.py            # starts the dashboard and opens it in the browser

The dashboard only reads its local database. It goes into Exiros only when the
user clicks "Update from Exiros" and logs in.
"""

import io
import os
import tempfile
import threading
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook

import exiros_fetch
import rfq_to_excel
import store

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("RFQ_DATA_DIR", HERE / "data"))
SOON_HOURS = 48
WEEK_DAYS = 7


def parse_deadline(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def fmt_when(value: str | None, with_time: bool = True) -> str:
    when = parse_deadline(value)
    if when is None:
        return "–"
    return when.strftime("%d %b %Y, %H:%M" if with_time and len(value) > 10 else "%d %b %Y")


def closes_label(value: str | None) -> str:
    when = parse_deadline(value)
    if when is None:
        return ""
    return when.strftime("%a %d %b, %H:%M") if len(value) > 10 else when.strftime("%a %d %b")


def urgency(deadline: str | None, now: datetime) -> str:
    """closed / soon (≤48h) / week (≤7 days) / later / none"""
    when = parse_deadline(deadline)
    if when is None:
        return "none"
    if when < now:
        return "closed"
    if when - now <= timedelta(hours=SOON_HOURS):
        return "soon"
    if when - now <= timedelta(days=WEEK_DAYS):
        return "week"
    return "later"


def time_left(deadline: str | None, now: datetime) -> str:
    when = parse_deadline(deadline)
    if when is None:
        return "No closing date"
    plural = lambda n, w: f"{n} {w}{'s' if n != 1 else ''}"  # noqa: E731
    delta = when - now
    if delta.total_seconds() < 0:
        days = (-delta).days
        return "Closed today" if days == 0 else f"Closed {plural(days, 'day')} ago"
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return "Closes in less than 1 hour"
    if hours < 24:
        return f"Closes in {plural(hours, 'hour')}"
    return f"Closes in {plural(delta.days, 'day')}"


def ago(stamp: str | None, now: datetime) -> str:
    when = parse_deadline(stamp)
    if when is None:
        return "never"
    minutes = int((now - when).total_seconds() // 60)
    if minutes < 60:
        return "just now" if minutes < 2 else f"{minutes} minutes ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} hour{'s' if minutes >= 120 else ''} ago"
    return f"{minutes // 1440} days ago"


class Updater:
    """Runs one user-started update at a time in a background thread."""

    def __init__(self, conn, config_path: Path, download_dir: Path):
        self.conn, self.config_path, self.download_dir = conn, config_path, download_dir
        self.lock = threading.Lock()
        self.state = {"running": False, "step": "", "log": [], "result": None}

    def progress(self, message: str) -> None:
        self.state["step"] = message
        self.state["log"] = (self.state["log"] + [message])[-200:]

    def start(self) -> str | None:
        """Start an update; returns an error message if it can't start."""
        config = exiros_fetch.load_config(self.config_path)
        problem = exiros_fetch.config_problem(config)
        if problem:
            return problem
        with self.lock:
            if self.state["running"]:
                return "An update is already running."
            self.state = {"running": True, "step": "Opening Exiros…", "log": [], "result": None}
        threading.Thread(target=self._run, args=(config,), daemon=True).start()
        return None

    def _run(self, config: dict) -> None:
        update_id = store.start_update(self.conn)
        try:
            found, new, failed = exiros_fetch.update_from_exiros(self.conn, config, self.download_dir, self.progress)
            message = f"Found {found} RFQs, {new} new." + (f" Couldn't read: {', '.join(failed)}." if failed else "")
            store.finish_update(self.conn, update_id, "ok", found, new, message)
            self.state["result"] = {"ok": True, "message": message}
        except Exception as exc:  # report anything to the user rather than dying silently
            store.finish_update(self.conn, update_id, "failed", message=str(exc))
            self.state["result"] = {"ok": False, "message": str(exc).splitlines()[0]}
        finally:
            self.state["running"] = False


def create_app(data_dir: Path | str = DATA_DIR, config_path: Path | str | None = None) -> FastAPI:
    data_dir = Path(data_dir)
    conn = store.connect(data_dir / "rfqs.db")
    updater = Updater(conn, Path(config_path or exiros_fetch.CONFIG_PATH), data_dir / "downloads")
    app = FastAPI(title="RFQ Platform")
    app.state.conn, app.state.updater = conn, updater
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["sar"] = lambda v: "–" if v is None or pd.isna(v) else f"{v:,.2f}"
    templates.env.filters["num"] = lambda v: "–" if v is None or pd.isna(v) else f"{v:,.10g}"
    templates.env.filters["date"] = lambda v: fmt_when(v, with_time=False)
    templates.env.filters["datetime"] = fmt_when
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def render(request: Request, name: str, **context):
        now = datetime.now()
        last = store.last_update(conn)
        context.setdefault("msg", request.query_params.get("msg"))
        context.setdefault("error", request.query_params.get("error"))
        return templates.TemplateResponse(request, name, {
            "now": now, "last_update": last, "last_update_ago": ago(last["finished"] if last else None, now),
            "updater": updater.state, **context,
        })

    def enrich(rfqs: pd.DataFrame, now: datetime) -> list[dict]:
        rows = rfqs.to_dict("records")
        for r in rows:
            r["urgency"] = urgency(r["deadline"], now)
            r["time_left"] = time_left(r["deadline"], now)
            r["closes"] = closes_label(r["deadline"])
            r["is_new"] = r["seen_at"] is None
        return rows

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, show: str = "open", q: str = ""):
        now = datetime.now()
        rows = enrich(store.rfqs_frame(conn), now)
        open_rows = [r for r in rows if r["urgency"] != "closed"]
        needs_quote = [r for r in open_rows if r["status"] != "Quoted"]
        groups = {
            "open": open_rows,
            "need": needs_quote,
            "soon": [r for r in open_rows if r["urgency"] == "soon"],
            "new": [r for r in rows if r["is_new"]],
            "closed": [r for r in rows if r["urgency"] == "closed"],
        }
        counts = {k: len(v) for k, v in groups.items()}
        shown = rows if q else groups.get(show, open_rows)
        if q:
            needle = q.lower()
            matches = {row[0] for row in conn.execute(
                "SELECT DISTINCT rfq FROM lines WHERE lower(description) LIKE ? OR lower(long_description) LIKE ? OR lower(brand) LIKE ?",
                (f"%{needle}%",) * 3)}
            shown = [r for r in shown if needle in r["rfq"].lower() or needle in (r["client"] or "").lower()
                     or needle in (r["title"] or "").lower() or r["rfq"] in matches]
        order = {"soon": 0, "week": 1, "later": 2, "none": 3, "closed": 4}
        shown.sort(key=lambda r: (order[r["urgency"]], parse_deadline(r["deadline"]) or datetime.max, r["rfq"]))
        if show == "closed" and not q:
            shown.reverse()
        return render(request, "dashboard.html", page="dashboard", rows=shown, counts=counts, show=show, q=q,
                      total_count=len(rows))

    @app.get("/rfq/{rfq}", response_class=HTMLResponse)
    def rfq_detail(request: Request, rfq: str):
        row = conn.execute("SELECT * FROM rfqs WHERE rfq = ?", (rfq,)).fetchone()
        if not row:
            return RedirectResponse(f"/?error={quote_plus(f'RFQ {rfq} not found')}", status_code=303)
        was_new = row["seen_at"] is None
        store.mark_seen(conn, rfq)
        now = datetime.now()
        info = enrich(pd.DataFrame([dict(row)]), now)[0]
        info["is_new"] = was_new
        lines = store.lines_frame(conn, rfq).to_dict("records")
        return render(request, "rfq.html", page="dashboard", r=info, lines=lines)

    @app.post("/rfq/{rfq}/deadline")
    def update_deadline(rfq: str, date: str = Form(""), time: str = Form("")):
        value = f"{date} {time or '23:59'}" if date else None
        store.set_deadline(conn, rfq, value)
        return RedirectResponse(f"/rfq/{rfq}?msg=Deadline+saved", status_code=303)

    @app.post("/seen")
    def seen_all():
        store.mark_all_seen(conn)
        return RedirectResponse("/", status_code=303)

    @app.get("/items", response_class=HTMLResponse)
    def item_history(request: Request, q: str = ""):
        rows = []
        if q.strip():
            needle = f"%{q.strip().lower()}%"
            rows = [dict(r) for r in conn.execute(
                """SELECT l.*, r.deadline, r.first_seen FROM lines l JOIN rfqs r USING (rfq)
                   WHERE lower(l.description) LIKE ? OR lower(l.long_description) LIKE ? OR lower(l.brand) LIKE ? OR lower(l.notes) LIKE ?
                   ORDER BY COALESCE(r.deadline, r.first_seen) DESC LIMIT 500""", (needle,) * 4)]
        priced = [r["unit_price"] for r in rows if r["unit_price"]]
        stats = {"count": len(priced), "min": min(priced), "max": max(priced), "last": priced[0]} if priced else None
        return render(request, "items.html", page="items", q=q, rows=rows, stats=stats)

    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request):
        return render(request, "import.html", page="import")

    @app.post("/import")
    def import_files(files: list[UploadFile] = File(...)):
        done, new, failed = [], 0, []
        with tempfile.TemporaryDirectory() as tmp:
            for upload in files:
                path = Path(tmp) / Path(upload.filename or "report.xls").name
                path.write_bytes(upload.file.read())
                try:
                    rfq, is_new = store.import_report(conn, path)
                    done.append(rfq)
                    new += is_new
                except Exception as exc:
                    failed.append(f"{upload.filename} ({str(exc).splitlines()[0]})")
        if not done:
            return RedirectResponse(f"/import?error={quote_plus('Nothing imported: ' + '; '.join(failed))}", status_code=303)
        msg = f"Imported {len(done)} RFQ(s), {new} new." + (f" Skipped: {'; '.join(failed)}" if failed else "")
        return RedirectResponse(f"/?msg={quote_plus(msg)}", status_code=303)

    @app.post("/update")
    def start_update():
        problem = updater.start()
        if problem:
            return RedirectResponse(f"/?error={quote_plus(problem)}", status_code=303)
        return RedirectResponse("/?updating=1", status_code=303)

    @app.get("/update/status")
    def update_status():
        return JSONResponse(updater.state)

    @app.get("/export.xlsx")
    def export(rfq: str | None = None):
        rfqs = store.rfqs_frame(conn)
        lines = store.lines_frame(conn, rfq) if rfq else store.lines_frame(conn)
        if rfq:
            rfqs = rfqs[rfqs["rfq"] == rfq]
            lines["deadline"] = rfqs["deadline"].iloc[0] if len(rfqs) else None
        now = datetime.now()
        rfqs = rfqs.assign(time_left=[time_left(d, now) for d in rfqs["deadline"]], currency="SAR")
        rfqs = rfqs.sort_values("deadline", na_position="last")
        for frame, cols in ((rfqs, ["deadline"]), (lines, ["deadline", "date_needed"])):
            for c in cols:
                frame[c] = pd.to_datetime(frame[c], errors="coerce")
        wb = Workbook()
        rfq_to_excel.write_sheet(wb.active, rfqs, [
            ("rfq", "RFQ No.", 12, None), ("deadline", "Deadline", 18, "dd-mmm-yyyy hh:mm"), ("time_left", "Time left", 16, None),
            ("status", "Status", 15, None), ("client", "Client", 45, None), ("lines", "Lines", 8, "0"),
            ("quoted_lines", "Lines quoted", 10, "0"), ("total", "Total quoted (SAR)", 18, rfq_to_excel.MONEY),
            ("earliest_needed", "Earliest date needed", 16, None), ("first_seen", "First seen", 16, None),
        ], total_cols=("lines", "quoted_lines", "total"))
        wb.active.title = "RFQs"
        rfq_to_excel.write_sheet(wb.create_sheet("Lines"), lines, [
            ("rfq", "RFQ No.", 12, None), ("deadline", "Deadline", 18, "dd-mmm-yyyy hh:mm"), ("line", "#", 5, "0"),
            ("description", "Description", 32, None), ("long_description", "Long description", 55, None),
            ("client", "Client", 36, None), ("requested_qty", "Requested qty", 11, rfq_to_excel.QTY), ("uom", "UoM", 12, None),
            ("date_needed", "Date needed", 13, rfq_to_excel.DATE), ("unit_price", "Our unit price (SAR)", 14, rfq_to_excel.MONEY),
            ("quoted_qty", "Quoted qty", 10, rfq_to_excel.QTY), ("line_total", "Line total (SAR)", 15, rfq_to_excel.MONEY),
            ("delivery_days", "Delivery (days)", 10, "0"), ("brand", "Brand", 18, None), ("notes", "Notes", 30, None),
            ("status", "Status", 12, None),
        ], total_cols=("line_total",))
        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        name = f"RFQ_{rfq}.xlsx" if rfq else f"Exiros_RFQs_{now:%Y-%m-%d}.xlsx"
        return StreamingResponse(buffer, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    return app


def main() -> None:
    import uvicorn

    port = int(os.environ.get("RFQ_PORT", "8765"))
    threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
