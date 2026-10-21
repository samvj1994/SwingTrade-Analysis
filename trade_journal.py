"""Persistent local journal for executed Groww trades and performance metrics."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd

from groww_portfolio import as_number, calculate_xirr


def _pick(row: dict[str, object], *keys: str) -> object | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _trade_key(row: dict[str, object]) -> str:
    explicit = _pick(row, "groww_trade_id", "trade_id", "tradeId", "id")
    if explicit:
        return str(explicit)
    identity = "|".join(str(_pick(row, *keys) or "") for keys in (
        ("groww_order_id", "order_id", "orderId"),
        ("trade_date_time", "trade_date", "timestamp", "exchange_time"),
        ("trading_symbol", "symbol"), ("transaction_type", "trade_type", "side"),
        ("quantity", "filled_quantity"), ("price", "trade_price"),
    ))
    return hashlib.sha256(identity.encode()).hexdigest()


def _normalize(row: dict[str, object], segment: str = "CASH") -> dict[str, object] | None:
    symbol = str(_pick(row, "trading_symbol", "symbol", "tradingSymbol") or "").strip().upper()
    side_raw = str(_pick(row, "transaction_type", "trade_type", "side", "action") or "").upper()
    if "BUY" in side_raw:
        side = "BUY"
    elif "SELL" in side_raw:
        side = "SELL"
    else:
        return None
    quantity = as_number(_pick(row, "quantity", "filled_quantity", "trade_quantity", "qty"))
    price = as_number(_pick(row, "price", "trade_price", "average_price", "average_trade_price"))
    when = pd.to_datetime(_pick(row, "trade_date_time", "trade_date", "timestamp", "exchange_time", "created_at"), errors="coerce")
    if not symbol or quantity is None or price is None or quantity <= 0 or price <= 0 or pd.isna(when):
        return None
    fee = as_number(_pick(row, "brokerage", "charges", "total_charges", "fees")) or 0.0
    order_id = str(_pick(row, "groww_order_id", "order_id", "orderId") or "")
    return {
        "trade_id": _trade_key(row), "order_id": order_id, "segment": segment.upper(),
        "symbol": symbol, "side": side, "quantity": float(quantity), "price": float(price),
        "fees": max(0.0, float(fee)), "trade_at": pd.Timestamp(when).isoformat(),
        "raw": json.dumps(row, default=str, sort_keys=True),
    }


def save_trades(db_path: str | Path, rows: list[dict[str, object]]) -> tuple[int, int]:
    """Insert new normalized fills; existing trade IDs are left unchanged."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    inserted = skipped = 0
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS fills (
          trade_id TEXT PRIMARY KEY, order_id TEXT, segment TEXT NOT NULL, symbol TEXT NOT NULL,
          side TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL, fees REAL NOT NULL,
          trade_at TEXT NOT NULL, raw TEXT NOT NULL)""")
        for row in rows:
            normalized = _normalize(row, str(row.get("segment", "CASH")))
            if normalized is None:
                skipped += 1
                continue
            cursor = connection.execute(
                "INSERT OR IGNORE INTO fills VALUES (:trade_id,:order_id,:segment,:symbol,:side,:quantity,:price,:fees,:trade_at,:raw)",
                normalized,
            )
            inserted += cursor.rowcount
    return inserted, skipped


def load_fills(db_path: str | Path) -> pd.DataFrame:
    path = Path(db_path)
    if not path.exists():
        return pd.DataFrame(columns=["trade_id", "order_id", "segment", "symbol", "side", "quantity", "price", "fees", "trade_at", "raw"])
    with sqlite3.connect(path) as connection:
        try:
            frame = pd.read_sql_query("SELECT * FROM fills ORDER BY trade_at, trade_id", connection)
        except sqlite3.OperationalError:
            return pd.DataFrame()
    if not frame.empty:
        frame["trade_at"] = pd.to_datetime(frame["trade_at"], errors="coerce")
    return frame


def closed_cycles(fills: pd.DataFrame) -> pd.DataFrame:
    """Build long-only closed trade cycles using weighted-average entry cost."""
    cycles: list[dict[str, object]] = []
    if fills.empty:
        return pd.DataFrame(columns=["Symbol", "Segment", "Entry", "Exit", "Quantity", "Gross P&L", "Fees", "Net P&L", "Hold minutes", "Plan adherence", "Notes"])
    for (segment, symbol), group in fills.groupby(["segment", "symbol"], sort=False):
        qty = average = gross = fees = 0.0
        entry_at = None
        entry_qty = 0.0
        for row in group.sort_values("trade_at").itertuples(index=False):
            amount = float(row.quantity)
            price = float(row.price)
            fee = float(row.fees)
            if row.side == "BUY":
                if qty <= 1e-10:
                    entry_at, entry_qty, gross, fees = row.trade_at, 0.0, 0.0, 0.0
                    average = 0.0
                new_qty = qty + amount
                average = ((average * qty) + (price * amount)) / new_qty if new_qty else 0.0
                qty = new_qty
                entry_qty += amount
                fees += fee
            elif qty > 1e-10:
                closed = min(qty, amount)
                gross += (price - average) * closed
                fees += fee * (closed / amount)
                qty -= closed
                if qty <= 1e-10:
                    entry_key = pd.Timestamp(entry_at).isoformat() if entry_at is not None else ""
                    exit_key = pd.Timestamp(row.trade_at).isoformat()
                    cycle_id = hashlib.sha256(f"{segment}|{symbol}|{entry_key}|{exit_key}".encode()).hexdigest()
                    cycles.append({"Cycle ID": cycle_id, "Symbol": symbol, "Segment": segment, "Entry": entry_at, "Exit": row.trade_at,
                        "Quantity": entry_qty, "Gross P&L": gross, "Fees": fees, "Net P&L": gross - fees,
                        "Hold minutes": max(0.0, (row.trade_at - entry_at).total_seconds() / 60) if entry_at is not None else 0.0,
                        "Plan adherence": None, "Notes": "", "Reviewed": False})
                    qty = average = 0.0
    return pd.DataFrame(cycles)


def open_positions(fills: pd.DataFrame, prices: dict[str, float] | None = None) -> pd.DataFrame:
    """Show remaining long exposure after replaying captured fills."""
    prices = prices or {}
    rows = []
    if fills.empty:
        return pd.DataFrame(columns=["Symbol", "Segment", "Quantity", "Average entry", "Last price", "Unrealized P&L"])
    for (segment, symbol), group in fills.groupby(["segment", "symbol"], sort=False):
        quantity = average = 0.0
        for row in group.sort_values(["trade_at", "trade_id"], kind="stable").itertuples(index=False):
            shares = float(row.quantity)
            price = float(row.price)
            if row.side == "BUY":
                next_quantity = quantity + shares
                average = ((quantity * average) + (shares * price)) / next_quantity if next_quantity else 0.0
                quantity = next_quantity
            elif quantity > 0:
                quantity = max(0.0, quantity - shares)
                if quantity == 0:
                    average = 0.0
        if quantity > 1e-10:
            last = prices.get(str(symbol).upper())
            rows.append({"Symbol": symbol, "Segment": segment, "Quantity": quantity,
                "Average entry": average, "Last price": last,
                "Unrealized P&L": (float(last) - average) * quantity if last is not None else None})
    return pd.DataFrame(rows)


def save_reviews(db_path: str | Path, reviews: pd.DataFrame) -> None:
    """Persist review fields keyed to a closed trade cycle."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS reviews (cycle_id TEXT PRIMARY KEY, plan_adherence INTEGER, notes TEXT NOT NULL DEFAULT '', reviewed INTEGER NOT NULL DEFAULT 0)")
        for row in reviews.to_dict(orient="records"):
            if not row.get("Cycle ID"):
                continue
            adherence = row.get("Plan adherence")
            if pd.isna(adherence):
                adherence = None
            else:
                adherence = int(bool(adherence))
            reviewed = row.get("Reviewed")
            reviewed = int(bool(reviewed)) if reviewed is not None and not pd.isna(reviewed) else 0
            note = row.get("Notes")
            note = "" if note is None or pd.isna(note) else str(note)
            connection.execute("INSERT INTO reviews VALUES (?,?,?,?) ON CONFLICT(cycle_id) DO UPDATE SET plan_adherence=excluded.plan_adherence, notes=excluded.notes, reviewed=excluded.reviewed",
                (str(row["Cycle ID"]), adherence, note, reviewed))


def merge_reviews(db_path: str | Path, cycles: pd.DataFrame) -> pd.DataFrame:
    if cycles.empty:
        return cycles
    path = Path(db_path)
    if not path.exists():
        return cycles
    with sqlite3.connect(path) as connection:
        try:
            reviews = pd.read_sql_query("SELECT * FROM reviews", connection)
        except sqlite3.OperationalError:
            return cycles
    if reviews.empty:
        return cycles
    reviews = reviews.rename(columns={"cycle_id": "Cycle ID", "plan_adherence": "Plan adherence", "notes": "Notes", "reviewed": "Reviewed"})
    reviews["Reviewed"] = reviews["Reviewed"].astype(bool)
    return cycles.drop(columns=["Plan adherence", "Notes", "Reviewed"], errors="ignore").merge(reviews, on="Cycle ID", how="left").assign(**{"Reviewed": lambda frame: frame["Reviewed"].fillna(False).astype(bool)})


def journal_metrics(fills: pd.DataFrame, prices: dict[str, float] | None = None, as_of: date | None = None) -> dict[str, object]:
    """Summarize fills and calculate XIRR from recorded cash flows and open value."""
    prices = prices or {}
    cycles = merge_reviews(Path(__file__).resolve().parent / "data" / "trade_journal.sqlite3", closed_cycles(fills))
    xirr = None
    if not fills.empty:
        flows = []
        running: dict[tuple[str, str], float] = {}
        for row in fills.sort_values("trade_at").itertuples(index=False):
            amount = float(row.quantity) * float(row.price)
            fee = float(row.fees)
            key = (str(row.segment), str(row.symbol))
            running[key] = running.get(key, 0.0) + (float(row.quantity) if row.side == "BUY" else -float(row.quantity))
            flows.append((row.trade_at, -(amount + fee) if row.side == "BUY" else amount - fee))
        if as_of is None:
            as_of = date.today()
        cutoff = pd.Timestamp(as_of).normalize()
        for (segment, symbol), quantity in running.items():
            if quantity > 1e-10 and symbol in prices:
                flows.append((cutoff, quantity * float(prices[symbol])))
        xirr = calculate_xirr(flows)
    gross = float(cycles["Gross P&L"].sum()) if not cycles.empty else 0.0
    net = float(cycles["Net P&L"].sum()) if not cycles.empty else 0.0
    wins = cycles[cycles["Net P&L"] > 0] if not cycles.empty else cycles
    losses = cycles[cycles["Net P&L"] < 0] if not cycles.empty else cycles
    loss_total = abs(float(losses["Net P&L"].sum())) if not losses.empty else 0.0
    equity = cycles["Net P&L"].cumsum() if not cycles.empty else pd.Series(dtype=float)
    drawdown = equity - equity.cummax() if not equity.empty else pd.Series(dtype=float)
    return {
        "cycles": cycles, "xirr": xirr, "closed_cycles": len(cycles), "gross_pnl": gross,
        "net_pnl": net, "win_rate": len(wins) / len(cycles) if len(cycles) else None,
        "profit_factor": float(wins["Net P&L"].sum()) / loss_total if loss_total else None,
        "expectancy": net / len(cycles) if len(cycles) else None,
        "max_drawdown": abs(float(drawdown.min())) if not drawdown.empty else 0.0,
        "average_hold_minutes": float(cycles["Hold minutes"].mean()) if not cycles.empty else None,
        "reviews_completed": int(cycles["Reviewed"].sum()) if not cycles.empty else 0,
        "plan_adherence": float(cycles["Plan adherence"].dropna().mean()) if not cycles.empty and cycles["Plan adherence"].notna().any() else None,
    }
