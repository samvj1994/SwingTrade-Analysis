"""Signal generation and no-look-ahead portfolio backtest for the NIFTY 50 strategy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd


TRADING_DAYS_YEAR = 252


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 100_000
    max_position_value: float = 10_000
    stop_loss_pct: float = 0.08
    fee_rate: float = 0.001


def prepare_data(frame: pd.DataFrame) -> pd.DataFrame:
    """Add strategy indicators and close-based entry/exit signals to OHLCV data."""
    required = {"Date", "Open", "High", "Low", "Close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")

    data = frame.copy()
    data["Date"] = pd.to_datetime(data["Date"])
    data = data.sort_values("Date").drop_duplicates("Date").set_index("Date")
    for column in ("Open", "High", "Low", "Close"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["Open", "High", "Low", "Close"])

    data["SMA_50"] = data["Close"].rolling(50, min_periods=50).mean()
    data["SMA_150"] = data["Close"].rolling(150, min_periods=150).mean()
    data["EMA_220"] = data["Close"].ewm(span=220, adjust=False, min_periods=220).mean()
    data["Low_52W"] = data["Low"].rolling(TRADING_DAYS_YEAR, min_periods=TRADING_DAYS_YEAR).min()
    # A breakout is judged at the close against the highest *previous* 252 closes.
    data["Prior_High_52W"] = data["Close"].shift(1).rolling(
        TRADING_DAYS_YEAR, min_periods=TRADING_DAYS_YEAR
    ).max()
    data["Dipped_Below_EMA_90D"] = (
        (data["Low"] < data["EMA_220"]).rolling(90, min_periods=90).max().fillna(0).astype(bool)
    )

    data["Trend_Above_EMA"] = data["SMA_150"] > data["EMA_220"]
    data["Close_Above_SMA_50"] = data["Close"] > data["SMA_50"]
    data["SMA_50_Above_SMA_150"] = data["SMA_50"] > data["SMA_150"]
    data["Above_125pct_52W_Low"] = data["Close"] > 1.25 * data["Low_52W"]
    data["Breakout_Above_52W_High"] = data["Close"] > data["Prior_High_52W"]
    criteria = [
        "Trend_Above_EMA",
        "Close_Above_SMA_50",
        "SMA_50_Above_SMA_150",
        "Above_125pct_52W_Low",
        "Dipped_Below_EMA_90D",
        "Breakout_Above_52W_High",
    ]
    data["Criteria_Met"] = data[criteria].sum(axis=1)
    data["Entry_Signal"] = data["Criteria_Met"] == len(criteria)
    data["EMA_Exit_Signal"] = data["Close"] < data["EMA_220"]
    return data.reset_index()


def load_market_data(data_dir: str | Path) -> dict[str, pd.DataFrame]:
    """Load and prepare NSE/NIFTY and BSE/SENSEX CSVs from ``data_dir``."""
    root = Path(data_dir)
    files = sorted(root.glob("*.csv")) + sorted((root / "BSE").glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"No CSV files found in {root.resolve()}")

    market_data = {}
    for file in files:
        exchange = "BSE" if file.parent.name.upper() == "BSE" else "NSE"
        index_name = "SENSEX" if exchange == "BSE" else "NIFTY 50"
        key = f"{exchange}/{index_name}/{file.stem}"
        market_data[key] = prepare_data(pd.read_csv(file))
    return market_data


def run_backtest(
    market_data: dict[str, pd.DataFrame], start_date: str | pd.Timestamp,
    end_date: str | pd.Timestamp, config: BacktestConfig = BacktestConfig(),
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Simulate entries/exits at the following session's open across all stocks.

    Signals are evaluated after a day's close. Orders are queued and filled at the
    next available daily open, ensuring there is no same-bar look-ahead.
    """
    start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)
    indexed = {
        symbol: data.set_index("Date").sort_index()
        for symbol, data in market_data.items()
    }
    calendar = sorted({day for data in indexed.values() for day in data.index if start <= day <= end})
    cash = config.initial_capital
    positions: dict[str, dict[str, float]] = {}
    pending: list[dict[str, str]] = []
    trades: list[dict[str, object]] = []
    equity: list[dict[str, object]] = []

    for day in calendar:
        # Execute orders generated at the prior close.
        for order in pending:
            symbol = order["symbol"]
            data = indexed[symbol]
            if day not in data.index:
                continue
            open_price = float(data.at[day, "Open"])
            if open_price <= 0:
                continue
            if order["side"] == "SELL" and symbol in positions:
                position = positions.pop(symbol)
                proceeds = position["shares"] * open_price
                fee = proceeds * config.fee_rate
                cash += proceeds - fee
                trades.append({"Symbol": symbol, "Entry Date": position["entry_date"], "Entry Price": position["entry_price"], "Exit Date": day, "Exit Price": open_price, "Shares": position["shares"], "Reason": order["reason"], "PnL": (open_price - position["entry_price"]) * position["shares"] - position["entry_fee"] - fee})
            elif order["side"] == "BUY" and symbol not in positions:
                budget = min(config.max_position_value, cash / max(1, len(order["candidates"])))
                shares = int(budget / (open_price * (1 + config.fee_rate)))
                if shares:
                    cost = shares * open_price
                    fee = cost * config.fee_rate
                    cash -= cost + fee
                    positions[symbol] = {"shares": shares, "entry_price": open_price, "entry_date": day, "entry_fee": fee}
        pending = []

        # Mark portfolio to closing prices, then create orders for tomorrow's open.
        marked_value = cash
        for symbol, position in positions.items():
            data = indexed[symbol]
            historical = data.loc[:day, "Close"]
            if not historical.empty:
                marked_value += position["shares"] * float(historical.iloc[-1])
        equity.append({"Date": day, "Equity": marked_value, "Cash": cash, "Open Positions": len(positions)})

        candidates = [symbol for symbol, data in indexed.items() if day in data.index and bool(data.at[day, "Entry_Signal"])]
        for symbol in positions:
            row = indexed[symbol].loc[day] if day in indexed[symbol].index else None
            if row is not None and (bool(row["EMA_Exit_Signal"]) or float(row["Close"]) <= positions[symbol]["entry_price"] * (1 - config.stop_loss_pct)):
                pending.append({"side": "SELL", "symbol": symbol, "reason": "EMA 220 exit" if bool(row["EMA_Exit_Signal"]) else f"{config.stop_loss_pct:.0%} stop loss"})
        for symbol in candidates:
            if symbol not in positions and not any(order["symbol"] == symbol for order in pending):
                pending.append({"side": "BUY", "symbol": symbol, "candidates": candidates})

    equity_df = pd.DataFrame(equity)
    trades_df = pd.DataFrame(trades)
    metrics = pd.DataFrame([{
        "Initial Capital": config.initial_capital,
        "Final Equity": equity_df["Equity"].iloc[-1] if not equity_df.empty else config.initial_capital,
        "Return %": ((equity_df["Equity"].iloc[-1] / config.initial_capital - 1) * 100) if not equity_df.empty else 0.0,
        "Closed Trades": len(trades_df),
        "Win Rate %": ((trades_df["PnL"] > 0).mean() * 100) if not trades_df.empty else 0.0,
    }])
    return equity_df, trades_df, metrics
