# Sedar

A small web app that turns Exiros exports into a view of how your distribution business is doing: sales trends, which customers are slipping away, and what stock to reorder.

## What it does

- **Dashboard**: revenue, gross profit, margin, orders, average order and active customers for the last 30 days compared with the 30 days before. Also a 12-month chart, top customers and products, and a list of things that need attention.
- **Customers**: each customer's usual ordering gap. It flags regular customers who have gone quiet (**At risk**) and those spending much less than before (**Declining**).
- **Products & stock**: ABC classification, sales per day, days of cover, a reorder point based on supplier lead time, a suggested order quantity, and dead stock and overstock alerts.
- **Excel downloads**: reorder list grouped by supplier, customer list and product list.

All numbers are measured from the latest date in your data, not today, so an export that is a few days old still gives the right picture.

## Getting data out of Exiros

Export two reports as CSV or Excel:

| Report | Needs | Nice to have |
| --- | --- | --- |
| Sales detail (one row per invoice line) | date, customer, item code, quantity, and either line amount or unit price | invoice no., item description, unit cost (for profit), salesperson |
| Stock on hand (one row per item) | item code, quantity on hand | description, category, unit cost, supplier, lead time in days |

Column names don't need to match anything exact. Sedar guesses the mapping and lets you correct it before importing. It also copes with title rows above the header, numbers like `1,234.50`, `2,50` or `(12.00)`, day-first or month-first dates, and "Grand Total" rows. Sales uploads add to existing data (an identical file is rejected), and each stock upload replaces the previous one.

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn sedar.main:app --reload
```

Open http://localhost:8000 and go to **Import data**. Data is stored in `data/sedar.db`. Set `SEDAR_DB` to use a different path.

To try it with made-up data first:

```bash
python scripts/generate_sample_data.py   # writes sample_data/sales_export.csv and stock_on_hand.xlsx
```

## Tuning

Reorder settings are at the top of `sedar/analytics.py`:

- `DEFAULT_LEAD_TIME_DAYS`: used when the stock file has no lead time
- `SAFETY_DAYS`: extra days of safety stock in the reorder point
- `REORDER_TARGET_DAYS`: how many days of extra cover a suggested order adds
- `OVERSTOCK_DAYS`: cover above this counts as overstock
- `VELOCITY_WINDOW_DAYS`: how many recent days of sales define sales speed

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

Code layout: `sedar/importer.py` (reading and mapping exports), `sedar/analytics.py` (metrics), `sedar/main.py` (web routes), `sedar/templates/` (pages). Chart.js is bundled in `sedar/static/vendor/` so the app works offline.

## RFQ Platform (`exiros_tool/`)

A local dashboard of the RFQs that Exiros (a customer) sends through its supplier portal, with deadlines and our submitted quotes. It runs on the user's own Windows computer (`setup_windows.bat`, then `start_platform.bat`); see `exiros_tool/HOW_TO_USE.txt`.

- **RFQs**: every RFQ, quoted and not quoted, sorted by submission deadline. Red means it closes within 48 hours, orange within 7 days. New RFQs are flagged until opened. Search and filter by status or client. Totals are in SAR.
- **RFQ detail**: every line with our unit price, quoted quantity, line total, delivery days, brand and notes. The deadline can be set by hand if the portal doesn't give one.
- **Price history**: what we quoted for an item before.
- **Excel**: all RFQs, or one RFQ, as a formatted workbook.
- **Update from Exiros**: runs only when clicked. It opens a fresh browser on the Exiros login page, waits for the user to log in, reads the RFQ list (numbers and deadlines) across pages, downloads each quotations report, saves it, then logs out and closes the browser. No login is kept. RFQs that are already closed and stored aren't downloaded again.

Portal-specific selectors live in `exiros_tool/exiros_config.json`. They need filling in from a one-time look at the portal pages (`python exiros_fetch.py capture` saves them). Until then the Update button says so, and quotation report files can be added by hand on the **Add files** page.

```bash
cd exiros_tool && pip install -r requirements.txt && python app.py   # http://127.0.0.1:8765
python exiros_tool/rfq_to_excel.py out.xlsx reports/               # convert report files without the platform
```
