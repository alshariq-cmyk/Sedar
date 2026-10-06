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

## Exiros RFQ downloader (`exiros_tool/`)

A standalone tool the user runs on their own computer: they log in to the Exiros supplier portal in a normal browser window, and `exiros_fetch.py` walks the RFQ list, downloads each quotations report and builds the combined Excel. Portal selectors live in `exiros_tool/exiros_config.json`; `exiros_fetch.py capture` saves portal pages so they can be filled in. See `exiros_tool/HOW_TO_USE.txt`.

### Exiros RFQ quotations to Excel

`exiros_tool/rfq_to_excel.py` combines the `quotationsReport.xls` files exported from the Exiros supplier portal into one workbook with a **Summary** sheet (one row per RFQ: client, line counts, total quoted value, earliest date needed, longest delivery) and a **Lines** sheet (every line with our unit price, quoted quantity, line total, delivery days, brand, notes and a status: Quoted / Qty differs / Not quoted). The RFQ number is taken from the file name.

```bash
python exiros_tool/rfq_to_excel.py output/rfqs.xlsx path/to/exports/   # a folder, or list files
```
