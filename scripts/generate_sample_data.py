"""Generate fake Exiros-style exports for a distribution business.

    python scripts/generate_sample_data.py [output_dir]

Writes sales_export.csv and stock_on_hand.xlsx, laid out the way ERP exports
usually are (title rows above the header, formatted numbers, a total row).
"""

import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

CUSTOMERS = [
    "Al Noor Trading", "Gulf Star Supermarket", "Blue Bay Hotel", "City Mart", "Desert Rose Cafe",
    "Falcon Catering", "Green Valley Grocers", "Harbor Foods", "Lulu Corner Store", "Marina Restaurants",
    "Oasis Hypermarket", "Palm Grove Bakery", "Royal Kitchen", "Sunrise Minimarket", "Union Coop",
]
PRODUCTS = [
    ("RC-5KG", "Basmati Rice 5kg", "Rice & Grains", 38.0, 52.0),
    ("RC-25KG", "Basmati Rice 25kg", "Rice & Grains", 170.0, 215.0),
    ("OIL-1L", "Sunflower Oil 1L", "Oils", 6.5, 9.0),
    ("OIL-5L", "Sunflower Oil 5L", "Oils", 29.0, 38.5),
    ("SUG-2KG", "White Sugar 2kg", "Baking", 7.0, 9.5),
    ("FLR-10KG", "All Purpose Flour 10kg", "Baking", 21.0, 27.0),
    ("TEA-100", "Black Tea 100 bags", "Beverages", 9.0, 14.0),
    ("COF-500", "Ground Coffee 500g", "Beverages", 22.0, 31.0),
    ("MLK-1L", "UHT Milk 1L (12 pack)", "Dairy", 30.0, 36.0),
    ("CHS-2KG", "Cheddar Block 2kg", "Dairy", 48.0, 62.0),
    ("TOM-400", "Tomato Paste 400g (24)", "Canned", 26.0, 33.0),
    ("CHK-1KG", "Chickpeas 1kg", "Rice & Grains", 5.5, 7.9),
    ("PST-500", "Spaghetti 500g (20)", "Pasta", 24.0, 31.0),
    ("WTR-24", "Water 500ml (24)", "Beverages", 7.5, 10.0),
    ("SPC-MIX", "Mixed Spices 1kg", "Spices", 35.0, 49.0),
    ("SAF-10G", "Saffron 10g", "Spices", 60.0, 69.0),
    ("OLV-750", "Olive Oil 750ml", "Oils", 18.0, 26.0),
]


def generate(out_dir: Path, seed: int = 7) -> tuple[Path, Path]:
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    end = date(2026, 9, 30)
    start = end - timedelta(days=540)
    weights = {c: rng.uniform(0.3, 3.0) for c in CUSTOMERS}
    weights["Oasis Hypermarket"] = 6.0
    weights["Union Coop"] = 4.5
    popularity = [rng.uniform(0.2, 3.0) for _ in PRODUCTS]
    popularity[-2] = 0.0  # saffron stops selling
    churned = {"Blue Bay Hotel": end - timedelta(days=75), "Harbor Foods": end - timedelta(days=50)}
    declining = {"City Mart"}

    rows = []
    invoice = 10000
    day = start
    while day <= end:
        if day.weekday() != 4:  # closed Fridays
            for customer in CUSTOMERS:
                if customer in churned and day > churned[customer]:
                    continue
                chance = 0.06 * weights[customer]
                if customer in declining and day > end - timedelta(days=90):
                    chance *= 0.35
                if rng.random() > chance:
                    continue
                invoice += 1
                for idx in rng.sample(range(len(PRODUCTS)), rng.randint(1, 5)):
                    if popularity[idx] == 0.0 and day > end - timedelta(days=120):
                        continue
                    code, name, _, cost, price = PRODUCTS[idx]
                    qty = max(1, int(rng.expovariate(1 / (4 * popularity[idx] + 1))))
                    unit_price = round(price * rng.uniform(0.95, 1.03), 2)
                    rows.append({
                        "Inv Date": day.strftime("%d/%m/%Y"),
                        "Invoice No": f"INV-{invoice}",
                        "Customer Name": customer,
                        "Item Code": code,
                        "Description": name,
                        "Qty": qty,
                        "Rate": f"{unit_price:,.2f}",
                        "Net Amount": f"{qty * unit_price:,.2f}",
                        "Cost Price": f"{cost:.2f}",
                        "Salesman": rng.choice(["Ahmed", "Priya", "Omar"]),
                    })
        day += timedelta(days=1)

    sales = pd.DataFrame(rows)
    sales_path = out_dir / "sales_export.csv"
    with sales_path.open("w", newline="") as f:
        f.write("Exiros - Sales Detail Report\n")
        f.write(f"Period: {start:%d/%m/%Y} to {end:%d/%m/%Y}\n")
        f.write("\n")
        sales.to_csv(f, index=False)
        total = sum(float(r["Net Amount"].replace(",", "")) for r in rows)
        f.write(f",,Grand Total,,,,,\"{total:,.2f}\",,\n")

    stock_rows = []
    for i, (code, name, category, cost, _) in enumerate(PRODUCTS):
        on_hand = int(rng.uniform(0, 400) * popularity[i] / 2)
        if code in ("OIL-5L", "COF-500"):
            on_hand = 0
        if code == "SAF-10G":
            on_hand = 120
        if code == "WTR-24":
            on_hand = 4000
        stock_rows.append({
            "Item Code": code, "Item Name": name, "Item Group": category, "Closing Stock": on_hand,
            "Avg Cost": cost, "Supplier": "Spice Route LLC" if category == "Spices" else rng.choice(["Global Foods Co", "Prime Distributors"]),
            "Lead Time": rng.choice([7, 14, 21]),
        })
    stock_path = out_dir / "stock_on_hand.xlsx"
    with pd.ExcelWriter(stock_path) as writer:
        pd.DataFrame([["Exiros - Stock on Hand"], [f"As of {end:%d/%m/%Y}"]]).to_excel(writer, index=False, header=False)
        pd.DataFrame(stock_rows).to_excel(writer, index=False, startrow=3)
    return sales_path, stock_path


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("sample_data")
    for p in generate(target):
        print(f"wrote {p}")
