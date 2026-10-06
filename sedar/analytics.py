"""Business metrics computed from imported sales and inventory data.

Everything is measured relative to the latest sale date in the data rather than
today, so an export that is a few days old still gives sensible numbers.
"""

import math
import sqlite3
from dataclasses import dataclass

import pandas as pd

DEFAULT_LEAD_TIME_DAYS = 14
SAFETY_DAYS = 7
REORDER_TARGET_DAYS = 30
OVERSTOCK_DAYS = 180
VELOCITY_WINDOW_DAYS = 90


def sales_frame(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT * FROM sales", conn)
    df["date"] = pd.to_datetime(df["date"])
    df["cost"] = df["unit_cost"] * df["quantity"]
    # Without an invoice number, treat one customer's purchases on one day as one order.
    df["order_key"] = df["invoice"].fillna(df["customer"] + "|" + df["date"].dt.strftime("%Y-%m-%d"))
    return df


def inventory_frame(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM inventory", conn)


def as_of(sales: pd.DataFrame) -> pd.Timestamp | None:
    return sales["date"].max() if not sales.empty else None


def _window(sales: pd.DataFrame, end: pd.Timestamp, days: int, offset: int = 0) -> pd.DataFrame:
    stop = end - pd.Timedelta(days=offset)
    start = stop - pd.Timedelta(days=days)
    return sales[(sales["date"] > start) & (sales["date"] <= stop)]


def _change(current: float, previous: float) -> float | None:
    if not previous:
        return None
    return (current - previous) / abs(previous) * 100


def _has_cost(df: pd.DataFrame) -> bool:
    return not df.empty and df["cost"].notna().mean() >= 0.5


def _period_stats(df: pd.DataFrame) -> dict:
    revenue = float(df["amount"].sum())
    orders = int(df["order_key"].nunique())
    stats = {
        "revenue": revenue,
        "orders": orders,
        "customers": int(df["customer"].nunique()),
        "avg_order": revenue / orders if orders else 0.0,
        "gross_profit": None,
        "margin": None,
    }
    if _has_cost(df):
        costed = df[df["cost"].notna()]
        gp = float(costed["amount"].sum() - costed["cost"].sum())
        stats["gross_profit"] = gp
        stats["margin"] = gp / float(costed["amount"].sum()) * 100 if costed["amount"].sum() else None
    return stats


def kpis(sales: pd.DataFrame, days: int = 30) -> dict:
    end = as_of(sales)
    current = _period_stats(_window(sales, end, days))
    previous = _period_stats(_window(sales, end, days, offset=days))
    return {
        "days": days,
        "as_of": end,
        "current": current,
        "change": {k: _change(current[k], previous[k]) if current[k] is not None and previous[k] is not None else None for k in current}
        | {"margin": current["margin"] - previous["margin"] if current["margin"] is not None and previous["margin"] is not None else None},
    }


def monthly_trend(sales: pd.DataFrame, months: int = 12) -> list[dict]:
    end = as_of(sales)
    start = (end - pd.DateOffset(months=months - 1)).to_period("M")
    df = sales[sales["date"].dt.to_period("M") >= start]
    grouped = df.groupby(df["date"].dt.to_period("M"))
    has_cost = _has_cost(df)
    rows = []
    for period, g in grouped:
        rows.append({
            "month": period.strftime("%b %Y"),
            "revenue": round(float(g["amount"].sum()), 2),
            "gross_profit": round(float(g["amount"].sum() - g["cost"].sum()), 2) if has_cost else None,
        })
    return rows


def top_customers(sales: pd.DataFrame, days: int = 90, limit: int = 10) -> list[dict]:
    end = as_of(sales)
    cur = _window(sales, end, days).groupby("customer")["amount"].sum()
    prev = _window(sales, end, days, offset=days).groupby("customer")["amount"].sum()
    total = cur.sum()
    out = []
    for customer, revenue in cur.sort_values(ascending=False).head(limit).items():
        out.append({
            "customer": customer,
            "revenue": float(revenue),
            "share": float(revenue / total * 100) if total else 0.0,
            "change": _change(float(revenue), float(prev.get(customer, 0.0))),
        })
    return out


def customer_table(sales: pd.DataFrame) -> pd.DataFrame:
    """One row per customer with ordering pattern and churn-risk status."""
    end = as_of(sales)
    orders = sales.groupby(["customer", "order_key"]).agg(date=("date", "min"), amount=("amount", "sum")).reset_index()
    rows = []
    recent_cut = end - pd.Timedelta(days=90)
    prior_cut = end - pd.Timedelta(days=180)
    for customer, g in orders.groupby("customer"):
        dates = g["date"].sort_values()
        order_days = dates.drop_duplicates()
        gaps = order_days.diff().dt.days.dropna()
        avg_gap = float(gaps.mean()) if len(gaps) else None
        days_since = int((end - dates.iloc[-1]).days)
        recent = float(g.loc[g["date"] > recent_cut, "amount"].sum())
        prior = float(g.loc[(g["date"] > prior_cut) & (g["date"] <= recent_cut), "amount"].sum())
        if len(order_days) < 3:
            status = "New" if dates.iloc[0] > recent_cut else ("Lapsed" if days_since > 90 else "Occasional")
        elif days_since > max(2 * avg_gap, 30):
            status = "At risk"
        elif prior and recent < prior * 0.6:
            status = "Declining"
        else:
            status = "Active"
        rows.append({
            "customer": customer,
            "lifetime_revenue": float(g["amount"].sum()),
            "orders": int(len(g)),
            "first_order": dates.iloc[0].strftime("%Y-%m-%d"),
            "last_order": dates.iloc[-1].strftime("%Y-%m-%d"),
            "days_since_last": days_since,
            "avg_days_between": round(avg_gap, 1) if avg_gap is not None else None,
            "revenue_90d": recent,
            "revenue_prev_90d": prior,
            "status": status,
        })
    columns = ["customer", "lifetime_revenue", "orders", "first_order", "last_order", "days_since_last", "avg_days_between", "revenue_90d", "revenue_prev_90d", "status"]
    return pd.DataFrame(rows, columns=columns).sort_values("lifetime_revenue", ascending=False).reset_index(drop=True)


def product_table(sales: pd.DataFrame, inventory: pd.DataFrame) -> pd.DataFrame:
    """One row per product: sales velocity, ABC class, stock cover and reorder suggestion."""
    end = as_of(sales)
    if end is not None:
        year = _window(sales, end, 365)
        recent = _window(sales, end, VELOCITY_WINDOW_DAYS)
    else:
        year = recent = sales
    names = pd.concat([
        sales.dropna(subset=["product_name"]).drop_duplicates("product_code", keep="last").set_index("product_code")["product_name"],
        inventory.dropna(subset=["product_name"]).set_index("product_code")["product_name"],
    ])
    names = names[~names.index.duplicated(keep="last")]

    revenue_12m = year.groupby("product_code")["amount"].sum()
    cost_12m = year.groupby("product_code")["cost"].sum(min_count=1)
    qty_90 = recent.groupby("product_code")["quantity"].sum()
    last_sold = sales.groupby("product_code")["date"].max()

    codes = sorted(set(sales["product_code"]) | set(inventory["product_code"]))
    df = pd.DataFrame(index=pd.Index(codes, name="product_code"))
    df["product_name"] = names.reindex(df.index)
    df["revenue_12m"] = revenue_12m.reindex(df.index).fillna(0.0)
    df["margin_12m"] = ((revenue_12m - cost_12m) / revenue_12m * 100).reindex(df.index)
    df["qty_90d"] = qty_90.reindex(df.index).fillna(0.0)
    df["daily_velocity"] = df["qty_90d"] / VELOCITY_WINDOW_DAYS
    df["last_sold"] = last_sold.reindex(df.index).dt.strftime("%Y-%m-%d")

    # ABC: A items make up the first 80% of revenue, B the next 15%, C the rest.
    ranked = df["revenue_12m"].sort_values(ascending=False)
    total = ranked.sum()
    share_before = (ranked.cumsum() - ranked) / total * 100 if total else ranked * 0
    df["abc"] = share_before.map(lambda s: "A" if s < 80 else ("B" if s < 95 else "C")).reindex(df.index)
    df.loc[df["revenue_12m"] <= 0, "abc"] = "C"

    inv = inventory.set_index("product_code")
    has_inventory = not inv.empty
    df["category"] = inv["category"].reindex(df.index) if has_inventory else None
    df["supplier"] = inv["supplier"].reindex(df.index) if has_inventory else None
    df["on_hand"] = inv["on_hand"].reindex(df.index) if has_inventory else float("nan")
    df["unit_cost"] = inv["unit_cost"].reindex(df.index) if has_inventory else float("nan")
    lead = inv["lead_time_days"].reindex(df.index) if has_inventory else pd.Series(float("nan"), index=df.index)
    df["lead_time_days"] = lead.fillna(DEFAULT_LEAD_TIME_DAYS)
    df["stock_value"] = df["on_hand"] * df["unit_cost"]
    df["days_of_cover"] = (df["on_hand"] / df["daily_velocity"]).where(df["daily_velocity"] > 0)
    df["reorder_point"] = df["daily_velocity"] * (df["lead_time_days"] + SAFETY_DAYS)
    target = df["daily_velocity"] * (df["lead_time_days"] + SAFETY_DAYS + REORDER_TARGET_DAYS)
    df["suggested_order"] = (target - df["on_hand"].fillna(0)).clip(lower=0).map(math.ceil)

    def status(r) -> str:
        if pd.isna(r["on_hand"]):
            return "Not in stock file"
        if r["on_hand"] <= 0:
            return "Out of stock" if r["daily_velocity"] > 0 else "No stock, no sales"
        if r["daily_velocity"] == 0:
            return "Dead stock"
        if r["on_hand"] <= r["reorder_point"]:
            return "Reorder now"
        if r["days_of_cover"] > OVERSTOCK_DAYS:
            return "Overstock"
        return "OK"

    df["status"] = df.apply(status, axis=1)
    df.loc[~df["status"].isin(["Out of stock", "Reorder now"]), "suggested_order"] = 0
    return df.reset_index().sort_values("revenue_12m", ascending=False).reset_index(drop=True)


@dataclass
class InventorySummary:
    stock_value: float | None
    reorder_count: int
    out_of_stock_count: int
    dead_stock_value: float | None
    overstock_value: float | None


def inventory_summary(products: pd.DataFrame) -> InventorySummary:
    def value(mask) -> float | None:
        vals = products.loc[mask, "stock_value"]
        return float(vals.sum()) if products["stock_value"].notna().any() else None

    return InventorySummary(
        stock_value=value(products["on_hand"].notna()),
        reorder_count=int((products["status"] == "Reorder now").sum()),
        out_of_stock_count=int((products["status"] == "Out of stock").sum()),
        dead_stock_value=value(products["status"] == "Dead stock"),
        overstock_value=value(products["status"] == "Overstock"),
    )


def insights(sales: pd.DataFrame, customers: pd.DataFrame, products: pd.DataFrame, has_inventory: bool) -> list[dict]:
    """Plain-language action items, most important first."""
    items: list[dict] = []
    k = kpis(sales)
    rev_change = k["change"]["revenue"]
    if rev_change is not None and abs(rev_change) >= 5:
        direction = "up" if rev_change > 0 else "down"
        items.append({"level": "good" if rev_change > 0 else "warn", "text": f"Revenue for the last 30 days is {direction} {abs(rev_change):.0f}% on the 30 days before."})

    at_risk = customers[customers["status"] == "At risk"]
    if not at_risk.empty:
        value = at_risk["lifetime_revenue"].sum()
        names = ", ".join(at_risk["customer"].head(3))
        items.append({"level": "warn", "text": f"{len(at_risk)} regular customer(s) have gone quiet for longer than usual ({names}…). Together they've bought {value:,.0f} to date. Worth a call.", "link": "/customers?status=At+risk"})
    declining = customers[customers["status"] == "Declining"]
    if not declining.empty:
        items.append({"level": "warn", "text": f"{len(declining)} customer(s) spent at least 40% less in the last 90 days than in the 90 days before.", "link": "/customers?status=Declining"})

    top = customers.head(max(1, len(customers) // 5))
    if len(customers) >= 5 and customers["lifetime_revenue"].sum():
        share = top["lifetime_revenue"].sum() / customers["lifetime_revenue"].sum() * 100
        if share >= 60:
            items.append({"level": "info", "text": f"Your top {len(top)} customers account for {share:.0f}% of revenue. Losing any one of them would hurt."})

    if has_inventory:
        oos = products[(products["status"] == "Out of stock") & (products["abc"] == "A")]
        if not oos.empty:
            items.append({"level": "bad", "text": f"{len(oos)} of your best-selling (A-class) items are out of stock: {', '.join(oos['product_code'].head(5))}.", "link": "/inventory?status=Out+of+stock"})
        reorder = products[products["status"] == "Reorder now"]
        if not reorder.empty:
            items.append({"level": "warn", "text": f"{len(reorder)} item(s) are at or below their reorder point.", "link": "/inventory?status=Reorder+now"})
        dead = products[products["status"] == "Dead stock"]
        if not dead.empty and dead["stock_value"].notna().any():
            items.append({"level": "info", "text": f"{len(dead)} item(s) haven't sold in {VELOCITY_WINDOW_DAYS} days, tying up {dead['stock_value'].sum():,.0f} in stock. Consider a promotion or a return to the supplier.", "link": "/inventory?status=Dead+stock"})
    else:
        items.append({"level": "info", "text": "Import a stock-on-hand export from Exiros to get reorder suggestions and dead-stock alerts.", "link": "/import"})

    low_margin = products[(products["abc"] == "A") & (products["margin_12m"] < 10)]
    if not low_margin.empty:
        items.append({"level": "info", "text": f"{len(low_margin)} top-selling item(s) earn under 10% margin: {', '.join(low_margin['product_code'].head(5))}. Review pricing."})
    return items
