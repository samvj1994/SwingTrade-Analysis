"""Download daily Indian equity data from Yahoo Finance.

Place ``nifty50_symbols.csv`` beside this script, then run:

    pip install -r requirements.txt
    python download_nifty50_data.py

Each symbol is saved under the selected output directory. Yahoo Finance uses
the ``.NS`` suffix for NSE stocks and ``.BO`` for BSE stocks.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd
import yfinance as yf


DEFAULT_START_DATE = "2025-01-01"
BASE_DIR = Path(__file__).resolve().parent
SYMBOLS_FILE = BASE_DIR / "nifty50_symbols.csv"
DATA_DIR = BASE_DIR / "data"
# Keep yfinance's timezone/cookie SQLite cache in the project. This avoids
# permission problems on machines where the default user cache is unavailable.
YFINANCE_CACHE_DIR = BASE_DIR / "work" / "yfinance_cache"


def read_symbols(path: Path) -> list[tuple[str, str]]:
    """Read display symbols and optional Yahoo tickers from a symbols CSV."""
    if not path.exists():
        raise FileNotFoundError(
            f"Symbol file not found: {path}\n"
            "Copy nifty50_symbols.csv into the same folder as this script."
        )

    symbols_df = pd.read_csv(path)
    symbol_column = next(
        (column for column in symbols_df.columns if column.strip().lower() == "symbol"),
        None,
    )
    if symbol_column is None:
        raise ValueError("The symbols CSV must contain a 'Symbol' column.")

    yahoo_column = next(
        (column for column in symbols_df.columns if column.strip().lower() == "yahooticker"),
        None,
    )
    symbols = (
        symbols_df[[symbol_column] + ([yahoo_column] if yahoo_column else [])]
        .dropna()
        .astype(str)
        .apply(lambda column: column.str.strip().str.upper())
        .loc[lambda frame: frame[symbol_column].ne("")]
        .drop_duplicates(subset=[symbol_column])
    )
    return [
        (row[symbol_column], row[yahoo_column] if yahoo_column else row[symbol_column])
        for _, row in symbols.iterrows()
    ]


def download_symbol(symbol: str, start_date: str, end_date: str | None, suffix: str) -> pd.DataFrame:
    """Return daily OHLCV data for one Indian stock in the supplied date range."""
    yahoo_symbol = symbol if symbol.endswith(suffix) else f"{symbol}{suffix}"
    # yfinance's end date is exclusive; use tomorrow when no end date is supplied.
    effective_end = end_date or (
        pd.Timestamp.today().normalize() + pd.Timedelta(days=1)
    ).strftime("%Y-%m-%d")
    data = yf.download(
        yahoo_symbol,
        start=start_date,
        end=effective_end,
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if data.empty:
        raise ValueError("No data returned")

    # yfinance may use MultiIndex columns even for one ticker; flatten them.
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    data = data.reset_index()
    data["Date"] = pd.to_datetime(data["Date"]).dt.strftime("%Y-%m-%d")
    preferred_columns = ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"]
    return data.loc[:, [column for column in preferred_columns if column in data.columns]]


def main(start_date: str, end_date: str | None, symbols_file: Path, output_dir: Path, suffix: str) -> None:
    YFINANCE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(YFINANCE_CACHE_DIR))
    symbols = read_symbols(symbols_file)
    output_dir.mkdir(parents=True, exist_ok=True)
    logging.info("Downloading %d symbols from %s", len(symbols), start_date)

    failures: list[str] = []
    for symbol, yahoo_ticker in symbols:
        try:
            data = download_symbol(yahoo_ticker, start_date, end_date, suffix)
            output_path = output_dir / f"{symbol.replace('.NS', '').replace('.BO', '')}.csv"
            data.to_csv(output_path, index=False)
            logging.info("Saved %s (%d daily bars)", output_path.name, len(data))
        except Exception as error:  # Keep downloading the remaining symbols.
            failures.append(symbol)
            logging.error("Failed to download %s: %s", symbol, error)

    if failures:
        logging.warning("No file was created for: %s", ", ".join(failures))
    else:
        logging.info("Completed successfully. Files saved to %s", output_dir)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Download daily Indian equity OHLCV data.")
    parser.add_argument("--start", default=DEFAULT_START_DATE, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="End date, exclusive (YYYY-MM-DD)")
    parser.add_argument("--symbols-file", type=Path, default=SYMBOLS_FILE)
    parser.add_argument("--output-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--suffix", choices=[".NS", ".BO"], default=".NS")
    args = parser.parse_args()
    main(args.start, args.end, args.symbols_file, args.output_dir, args.suffix)
