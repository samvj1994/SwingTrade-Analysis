"""Accurate portfolio calculations from Groww Trade API responses."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import pandas as pd


ASSET_CLASS_ALIASES = {
    "stock": "Stocks",
    "stocks": "Stocks",
    "equity": "Stocks",
    "equities": "Stocks",
    "f&o": "F&O",
    "fno": "F&O",
    "derivatives": "F&O",
    "mutual fund": "Mutual Funds",
    "mutual funds": "Mutual Funds",
    "mutual_funds": "Mutual Funds",
    "mf": "Mutual Funds",
}


def as_number(value: object) -> float | None:
    """Convert a scalar API value to a finite float, or return None."""
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number):
        return None
    result = float(number)
    return result if math.isfinite(result) else None


def _first_number(record: Mapping[str, object], fields: tuple[str, ...]) -> float | None:
    for field in fields:
        number = as_number(record.get(field))
        if number is not None:
            return number
    return None


def extract_ltp_prices(response: object, symbols: set[str]) -> dict[str, float]:
    """Extract quote values from Groww's symbol-keyed or nested LTP response."""
    prices: dict[str, float] = {}

    def visit(value: object) -> None:
        if not isinstance(value, Mapping):
            return
        for key, item in value.items():
            symbol = str(key)
            if symbol in symbols:
                price = as_number(item)
                if price is None and isinstance(item, Mapping):
                    for price_key in ("ltp", "last_price", "close"):
                        price = as_number(item.get(price_key))
                        if price is not None:
                            break
                if price is not None and price > 0:
                    prices[symbol] = price
            elif isinstance(item, Mapping):
                visit(item)

    visit(response)
    return prices


def stock_quote_symbol(holding: dict[str, object]) -> str | None:
    """Choose one supported exchange symbol for a stock holding."""
    symbol = str(
        holding.get("trading_symbol")
        or holding.get("symbol")
        or holding.get("ticker")
        or ""
    )
    exchanges = holding.get("tradable_exchanges")
    exchange = next(
        (
            candidate
            for candidate in ("NSE", "BSE")
            if isinstance(exchanges, list) and candidate in exchanges
        ),
        None,
    )
    if exchange is None:
        exchange = next(
            (
                candidate
                for candidate in ("NSE", "BSE")
                if str(
                    holding.get("exchange")
                    or holding.get("exchange_name")
                    or holding.get("listing_exchange")
                    or holding.get("market")
                    or ""
                ).strip().upper()
                == candidate
            ),
            None,
        )
    return f"{exchange}_{symbol}" if exchange and symbol else None


def stock_quote_symbols(holdings: list[dict[str, object]]) -> set[str]:
    """Return one quote symbol per open stock holding."""
    return {
        quote_symbol
        for holding in holdings
        if (quantity := as_number(holding.get("quantity"))) is not None
        and quantity > 0
        if (quote_symbol := stock_quote_symbol(holding)) is not None
    }


def load_historical_stock_prices(
    data_dir: str | Path,
    holdings: list[dict[str, object]],
    as_of: date | pd.Timestamp | None = None,
) -> dict[str, tuple[float, pd.Timestamp]]:
    """Find each held stock's latest downloaded close on or before ``as_of``."""
    root = Path(data_dir)
    cutoff = pd.Timestamp(as_of or date.today()).normalize()
    prices: dict[str, tuple[float, pd.Timestamp]] = {}
    for holding in holdings:
        quote_symbol = stock_quote_symbol(holding)
        if not quote_symbol:
            continue
        exchange, symbol = quote_symbol.split("_", 1)
        file = (root / "BSE" if exchange == "BSE" else root) / f"{symbol}.csv"
        if not file.is_file():
            continue
        frame = pd.read_csv(file, usecols=["Date", "Close"])
        frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
        frame["Close"] = pd.to_numeric(frame["Close"], errors="coerce")
        valid = frame.dropna(subset=["Date", "Close"])
        valid = valid[(valid["Date"] <= cutoff) & (valid["Close"] > 0)]
        if valid.empty:
            continue
        latest = valid.loc[valid["Date"].idxmax()]
        prices[quote_symbol] = (float(latest["Close"]), pd.Timestamp(latest["Date"]))
    return prices


def stock_holding_rows(
    holdings: list[dict[str, object]],
    ltp_prices: dict[str, float],
    historical_prices: dict[str, tuple[float, pd.Timestamp]] | None = None,
) -> list[dict[str, object]]:
    """Build stock values using live quotes first, then dated downloaded closes."""
    historical_prices = historical_prices or {}
    rows = []
    for holding in holdings:
        symbol = str(holding.get("trading_symbol", ""))
        quote_symbol = stock_quote_symbol(holding)
        exchange = quote_symbol.split("_", 1)[0] if quote_symbol else None
        quantity = as_number(holding.get("quantity"))
        average_price = as_number(holding.get("average_price"))
        invested = quantity * average_price if quantity is not None and average_price is not None else None
        price = ltp_prices.get(quote_symbol) if quote_symbol else None
        price_source = "Live Groww quote" if price is not None else None
        price_date = None
        if price is None and quote_symbol in historical_prices:
            price, price_date = historical_prices[quote_symbol]
            price_source = "Historical close"
        current_value = quantity * price if quantity is not None and price is not None else None
        rows.append({
            "Symbol": symbol or "Unknown",
            "Exchange": exchange or "Unknown",
            "Quantity": quantity,
            "Average cost": average_price,
            "Invested value": invested,
            "Price": price,
            "Price source": price_source or "Unavailable",
            "Price date": price_date,
            "Current value": current_value,
            "Unrealized P&L": current_value - invested if current_value is not None and invested is not None else None,
        })
    return rows


def current_stock_position_rows(
    holdings: list[dict[str, object]],
    ltp_prices: dict[str, float],
    historical_prices: dict[str, tuple[float, pd.Timestamp]] | None = None,
) -> list[dict[str, object]]:
    """Build current NSE/BSE and explicitly identified US stock positions."""
    historical_prices = historical_prices or {}
    rows = []
    for holding in holdings:
        quantity = as_number(holding.get("quantity"))
        if quantity is None or quantity <= 0:
            continue

        symbol = str(
            holding.get("trading_symbol")
            or holding.get("symbol")
            or holding.get("ticker")
            or ""
        ).strip().upper()
        market_fields = (
            "exchange",
            "exchange_name",
            "listing_exchange",
            "market",
            "market_name",
            "country",
            "currency",
            "currency_code",
            "trading_currency",
        )
        market_values = [
            str(holding.get(field, "")).upper()
            for field in market_fields
            if holding.get(field) is not None
        ]
        tradable_exchanges = holding.get("tradable_exchanges")
        if isinstance(tradable_exchanges, list):
            market_values.extend(str(value).upper() for value in tradable_exchanges)
        market_text = " ".join(market_values)
        quote_symbol = stock_quote_symbol(holding)
        if quote_symbol:
            exchange, quote_ticker = quote_symbol.split("_", 1)
            market, currency = exchange, "INR"
            symbol = quote_ticker
        else:
            domestic_exchange = next(
                (
                    exchange
                    for exchange in ("NSE", "BSE")
                    if exchange in {value.strip() for value in market_values}
                ),
                None,
            )
            is_us_listing = any(
                value.strip() in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA", "USD"}
                or "NASDAQ" in value
                or "NYSE" in value
                for value in market_values
            )
            if domestic_exchange:
                market, currency = domestic_exchange, "INR"
                quote_symbol = f"{market}_{symbol}"
            elif is_us_listing:
                market = next(
                    (name for name in ("NASDAQ", "NYSE") if name in market_text),
                    "US",
                )
                currency = "USD"
            else:
                continue
        if not symbol:
            continue

        average_price = _first_number(
            holding, ("average_price", "avg_price", "average_cost", "cost_price")
        )
        invested_value = (
            quantity * average_price if average_price is not None else None
        )
        if invested_value is None and market == "INR":
            invested_value = _first_number(
                holding, ("invested_value", "cost_basis", "total_investment")
            )
        price = ltp_prices.get(quote_symbol) if quote_symbol else None
        price_source = "Live Groww quote" if price is not None else None
        price_date = None
        if quote_symbol and price is None and quote_symbol in historical_prices:
            price, price_date = historical_prices[quote_symbol]
            price_source = "Historical close"
        if price is None:
            for field in ("ltp", "last_price", "market_price", "current_price"):
                price = as_number(holding.get(field))
                if price is not None and price > 0:
                    price_source = "Groww holding data"
                    break
                price = None
        market_value = quantity * price if price is not None else _first_number(
            holding, ("current_value", "market_value", "holding_value")
        )
        if price is None and market_value is not None:
            price = market_value / quantity
        unrealized_pnl = (
            market_value - invested_value
            if market_value is not None and invested_value is not None
            else _first_number(
                holding, ("unrealized_pnl", "unrealised_pnl")
            )
        )
        rows.append(
            {
                "Position key": f"{market}:{symbol}",
                "Market": market,
                "Symbol": symbol,
                "Currency": currency,
                "Quantity": quantity,
                "Average cost": average_price,
                "Invested value": invested_value,
                "Current price": price,
                "Market value": market_value,
                "Unrealized P&L": unrealized_pnl,
                "Price source": price_source or "Unavailable",
                "Price date": price_date,
            }
        )
    return rows


def complete_total(rows: list[dict[str, object]], field: str) -> float | None:
    """Return a portfolio total only when every row has a valid value."""
    values = [as_number(row.get(field)) for row in rows]
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def position_value_total(
    positions: list[dict[str, object]],
    fields: tuple[str, ...],
) -> float | None:
    """Sum a Groww position field only when every position reports it."""
    if not positions:
        return None
    values = []
    for position in positions:
        value = None
        for field in fields:
            value = as_number(position.get(field))
            if value is not None:
                break
        if value is None:
            return None
        values.append(value)
    return sum(values)


def position_pnl(positions: list[dict[str, object]]) -> float | None:
    """Sum Groww's position P&L field across all positions when available."""
    return position_value_total(
        positions,
        ("unrealized_pnl", "unrealised_pnl", "pnl", "profit_loss"),
    )


def calculate_xirr(cash_flows: list[tuple[pd.Timestamp, float]]) -> float | None:
    """Return the unique annualized money-weighted return, if one exists."""
    flows_by_date: dict[pd.Timestamp, float] = {}
    for day, amount in cash_flows:
        normalized_day = pd.Timestamp(day).normalize()
        flows_by_date[normalized_day] = flows_by_date.get(normalized_day, 0.0) + amount
    flows = sorted(
        ((day, amount) for day, amount in flows_by_date.items() if amount != 0),
        key=lambda item: item[0],
    )
    if len(flows) < 2:
        return None
    signs = {1 if amount > 0 else -1 for _, amount in flows}
    if len(signs) != 2:
        return None
    if sum(
        1
        for (_, previous), (_, current) in zip(flows, flows[1:])
        if previous * current < 0
    ) != 1:
        return None

    start = flows[0][0]

    def npv(rate: float) -> float:
        log_base = math.log1p(rate)
        values = []
        for day, amount in flows:
            years = (day - start).days / 365.0
            exponent = -years * log_base
            if exponent > 700:
                return math.copysign(math.inf, amount)
            values.append(amount * math.exp(exponent))
        return math.fsum(values)

    low, high = -0.999999, 0.1
    low_value = npv(low)
    high_value = npv(high)
    while low_value * high_value > 0 and high < 1_000_000:
        high = high * 2 + 0.1
        high_value = npv(high)
    if low_value * high_value > 0:
        return None

    for _ in range(120):
        middle = (low + high) / 2
        middle_value = npv(middle)
        if abs(middle_value) < 1e-9:
            return middle
        if (middle_value > 0) == (low_value > 0):
            low, low_value = middle, middle_value
        else:
            high = middle
    return (low + high) / 2


def statement_performance(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, object]]:
    """Calculate section and portfolio performance from normalized statement rows.

    Required CSV columns: date, asset_class, cash_flow, market_value. Purchases
    are negative cash flows; sale/redemption proceeds are positive. Add closing
    market values as positive values on the valuation date.
    """
    if frame.empty:
        raise ValueError("CSV contains no data rows.")
    required = {"date", "asset_class", "cash_flow", "market_value"}
    normalized_columns = {str(column).strip().lower(): column for column in frame.columns}
    missing = required - normalized_columns.keys()
    if missing:
        raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}.")

    data = frame.rename(
        columns={original: normalized for normalized, original in normalized_columns.items()}
    ).copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
    if data["date"].isna().any():
        raise ValueError("Every CSV row must have a valid date.")

    data["asset_class"] = data["asset_class"].astype(str).str.strip().str.lower().map(ASSET_CLASS_ALIASES)
    if data["asset_class"].isna().any():
        raise ValueError("Asset class must be Stocks, F&O, or Mutual Funds.")

    for field in ("cash_flow", "market_value"):
        raw_values = data[field]
        parsed_values = raw_values.map(as_number)
        invalid = (
            raw_values.notna()
            & raw_values.astype(str).str.strip().ne("")
            & parsed_values.isna()
        )
        if invalid.any():
            raise ValueError(f"{field} must contain plain numeric rupee amounts.")
        data[field] = parsed_values
    if (data["market_value"].dropna() < 0).any():
        raise ValueError("Closing market values cannot be negative.")
    if (data["cash_flow"].isna() & data["market_value"].isna()).any():
        raise ValueError("Every row must contain a cash flow, a market value, or both.")

    section_rows: list[dict[str, object]] = []
    section_flows: dict[str, list[tuple[pd.Timestamp, float]]] = {}
    valuation_dates: dict[str, pd.Timestamp] = {}
    for asset_class, group in data.groupby("asset_class", sort=False):
        valuation_rows = group[group["market_value"].notna()]
        if valuation_rows.empty:
            raise ValueError(f"{asset_class} needs a closing market value for XIRR.")
        valuation_date = valuation_rows["date"].max()
        closing_value = float(
            valuation_rows.loc[valuation_rows["date"] == valuation_date, "market_value"].sum()
        )
        transaction_rows = group[group["cash_flow"].notna()]
        if transaction_rows.empty:
            raise ValueError(f"{asset_class} needs at least one dated investment cash flow.")
        if (transaction_rows["date"] > valuation_date).any():
            raise ValueError(f"{asset_class} has a transaction dated after its closing valuation.")

        flows = [
            (row.date, float(row.cash_flow))
            for row in transaction_rows.itertuples(index=False)
        ]
        flows.append((valuation_date, closing_value))
        section_flows[asset_class] = flows
        valuation_dates[asset_class] = valuation_date
        net_flows = sum(amount for _, amount in flows[:-1])
        xirr = calculate_xirr(flows)
        section_rows.append({
            "Section": asset_class,
            "Invested (buy flows)": -sum(amount for _, amount in flows[:-1] if amount < 0),
            "Sale / redemption proceeds": sum(amount for _, amount in flows[:-1] if amount > 0),
            "Closing value": closing_value,
            "Total P&L": net_flows + closing_value,
            "XIRR %": xirr * 100 if xirr is not None else None,
            "Valuation date": valuation_date,
        })

    dates = set(valuation_dates.values())
    if len(dates) != 1:
        total = {
            "xirr_pct": None,
            "pnl": None,
            "closing_value": None,
            "valuation_date": None,
        }
    else:
        all_flows = [flow for flows in section_flows.values() for flow in flows]
        xirr = calculate_xirr(all_flows)
        total = {
            "xirr_pct": xirr * 100 if xirr is not None else None,
            "pnl": sum(float(row["Total P&L"]) for row in section_rows),
            "closing_value": sum(float(row["Closing value"]) for row in section_rows),
            "valuation_date": next(iter(dates)),
        }
    return pd.DataFrame(section_rows), total
