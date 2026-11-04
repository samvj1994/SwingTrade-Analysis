"""Download daily equity data from Groww or Yahoo Finance.

Place ``nifty50_symbols.csv`` beside this script, then run:

    pip install -r requirements.txt
    python download_nifty50_data.py

Each symbol is saved under the selected output directory. Groww supports NSE
and BSE history; Yahoo Finance is used for markets not supported by Groww.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import time
from typing import Protocol

import pandas as pd
import yfinance as yf

from groww_connection import get_groww_client


DEFAULT_START_DATE = "2025-01-01"
MINIMUM_STRATEGY_BARS = 309
GROWW_MAX_CANDLE_DAYS = 180
GROWW_MIN_REQUEST_INTERVAL_SECONDS = 0.25
GROWW_RATE_LIMIT_RETRIES = 4
GROWW_HISTORICAL_SYMBOL_ALIASES = {"ETERNAL": ("ZOMATO",)}
BASE_DIR = Path(__file__).resolve().parent
SYMBOLS_FILE = BASE_DIR / "nifty50_symbols.csv"
DATA_DIR = BASE_DIR / "data"
# Keep yfinance's timezone/cookie SQLite cache in the project. This avoids
# permission problems on machines where the default user cache is unavailable.
YFINANCE_CACHE_DIR = BASE_DIR / "work" / "yfinance_cache"


class GrowwHistoricalClient(Protocol):
    def get_historical_candles(
        self,
        *,
        exchange: str,
        segment: str,
        groww_symbol: str,
        start_time: str,
        end_time: str,
        candle_interval: str,
        timeout: int,
    ) -> dict[str, object]: ...


class GrowwRequestPacer:
    """Keep historical calls below Groww's published per-minute rate limit."""

    def __init__(self) -> None:
        self._last_request_at = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < GROWW_MIN_REQUEST_INTERVAL_SECONDS:
            time.sleep(GROWW_MIN_REQUEST_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()


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
    """Return daily OHLCV data for one symbol in the supplied date range."""
    yahoo_symbol = symbol if symbol.endswith(suffix) else f"{symbol}{suffix}"
    # yfinance's end date is exclusive; use tomorrow when no end date is supplied.
    effective_end = end_date or (
        pd.Timestamp.today().normalize() + pd.Timedelta("1d")
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


def resolve_yahoo_ticker(symbol: str, yahoo_ticker: str, suffix: str) -> str:
    """Use a name-based Yahoo ticker for BSE symbols mapped to numeric scrip codes."""
    if suffix == ".BO" and yahoo_ticker.isdigit():
        return symbol
    return yahoo_ticker


def download_market_symbol(
    symbol: str,
    yahoo_ticker: str,
    start_date: str,
    end_date: str | None,
    suffix: str,
    provider: str = "yahoo",
    groww_client: GrowwHistoricalClient | None = None,
    request_pacer: GrowwRequestPacer | None = None,
) -> tuple[pd.DataFrame, str]:
    """Download a symbol from Groww or Yahoo, with an NSE fallback for Yahoo BSE data."""
    if provider == "groww":
        if suffix not in (".NS", ".BO"):
            raise ValueError("Groww historical data supports NSE/BSE symbols, not US stocks.")
        if groww_client is None:
            raise ValueError("A Groww client is required for Groww historical data.")
        exchange = "NSE" if suffix == ".NS" else "BSE"
        data = download_groww_symbol(
            groww_client, symbol, exchange, start_date, end_date, request_pacer
        )
        price_source = f"Groww API ({exchange})"
        aliases = GROWW_HISTORICAL_SYMBOL_ALIASES.get(symbol, ())
        if aliases:
            historical_data = [
                download_groww_symbol(
                    groww_client, alias, exchange, start_date, end_date, request_pacer
                )
                for alias in aliases
            ]
            data = pd.concat([*historical_data, data], ignore_index=True)
            data = data.drop_duplicates(subset=["Date"], keep="last").sort_values("Date")
            price_source = f"{price_source}; {'/'.join(aliases)} history merged"
        if exchange == "BSE" and len(data) < MINIMUM_STRATEGY_BARS:
            nse_data = download_groww_symbol(
                groww_client, symbol, "NSE", start_date, end_date, request_pacer
            )
            if len(nse_data) >= MINIMUM_STRATEGY_BARS:
                data = nse_data
                price_source = "Groww API (NSE fallback for BSE)"
        return data.reset_index(drop=True), price_source

    resolved_ticker = resolve_yahoo_ticker(symbol, yahoo_ticker, suffix)
    data = download_symbol(resolved_ticker, start_date, end_date, suffix)
    if suffix != ".BO":
        return data, "Yahoo Finance"

    if len(data) < MINIMUM_STRATEGY_BARS:
        nse_data = download_symbol(symbol, start_date, end_date, ".NS")
        if len(nse_data) >= MINIMUM_STRATEGY_BARS:
            return nse_data, "NSE fallback"
    return data, "BSE"


def download_groww_symbol(
    groww_client: GrowwHistoricalClient,
    symbol: str,
    exchange: str,
    start_date: str,
    end_date: str | None,
    request_pacer: GrowwRequestPacer | None = None,
) -> pd.DataFrame:
    """Fetch daily Groww candles in chunks no longer than 180 calendar days."""
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(
        end_date or (pd.Timestamp.today().normalize() + pd.Timedelta("1d"))
    ).normalize()
    if end <= start:
        raise ValueError("The end date must be after the start date.")

    request_pacer = request_pacer or GrowwRequestPacer()
    frames = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(
            chunk_start + pd.Timedelta(f"{GROWW_MAX_CANDLE_DAYS}d"), end
        )
        request_start = chunk_start.strftime("%Y-%m-%d 00:00:00")
        request_end = (chunk_end - pd.Timedelta("1s")).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        response = get_groww_candles(
            groww_client,
            exchange,
            symbol,
            request_start,
            request_end,
            request_pacer,
        )
        candles = response.get("candles", []) if isinstance(response, dict) else []
        if candles:
            valid_candles = [
                candle[:6]
                for candle in candles
                if len(candle) >= 6 and all(pd.notna(value) for value in candle[:5])
            ]
            if valid_candles:
                frames.append(
                    pd.DataFrame(
                        valid_candles,
                        columns=["Date", "Open", "High", "Low", "Close", "Volume"],
                    )
                )
        chunk_start = chunk_end

    if not frames:
        raise ValueError(f"Groww returned no daily candles for {exchange}-{symbol}.")
    data = pd.concat(frames, ignore_index=True)
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce").dt.strftime(
        "%Y-%m-%d"
    )
    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column in data:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["Date", "Open", "High", "Low", "Close"])
    data = data.drop_duplicates(subset=["Date"], keep="last").sort_values("Date")
    return data[["Date", "Open", "High", "Low", "Close", "Volume"]].reset_index(
        drop=True
    )


def get_groww_candles(
    groww_client: GrowwHistoricalClient,
    exchange: str,
    symbol: str,
    start_time: str,
    end_time: str,
    request_pacer: GrowwRequestPacer,
) -> dict[str, object]:
    for attempt in range(GROWW_RATE_LIMIT_RETRIES + 1):
        request_pacer.wait()
        try:
            return groww_client.get_historical_candles(
                exchange=exchange,
                segment="CASH",
                groww_symbol=f"{exchange}-{symbol}",
                start_time=start_time,
                end_time=end_time,
                candle_interval="1day",
                timeout=30,
            )
        except Exception as error:
            if "rate limit" not in str(error).lower() or attempt == GROWW_RATE_LIMIT_RETRIES:
                raise
            delay = 2**attempt
            logging.warning(
                "Groww rate limit for %s-%s; retrying in %d seconds",
                exchange,
                symbol,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError(f"Unable to fetch Groww candles for {exchange}-{symbol}.")


def main(
    start_date: str,
    end_date: str | None,
    symbols_file: Path,
    output_dir: Path,
    suffix: str,
    provider: str = "yahoo",
) -> int:
    if provider == "yahoo":
        YFINANCE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(YFINANCE_CACHE_DIR))
        groww_client = None
    else:
        groww_client = get_groww_client()
    request_pacer = GrowwRequestPacer() if provider == "groww" else None
    symbols = read_symbols(symbols_file)
    output_dir.mkdir(parents=True, exist_ok=True)
    logging.info("Downloading %d symbols from %s", len(symbols), start_date)

    failures: list[str] = []
    for symbol, yahoo_ticker in symbols:
        try:
            data, price_source = download_market_symbol(
                symbol,
                yahoo_ticker,
                start_date,
                end_date,
                suffix,
                provider=provider,
                groww_client=groww_client,
                request_pacer=request_pacer,
            )
            if provider == "groww" or suffix == ".BO":
                data["Price source"] = price_source
            output_path = output_dir / f"{symbol.replace('.NS', '').replace('.BO', '')}.csv"
            data.to_csv(output_path, index=False)
            logging.info(
                "Saved %s (%d daily bars; %s)",
                output_path.name,
                len(data),
                price_source,
            )
        except Exception as error:  # Keep downloading the remaining symbols.
            failures.append(symbol)
            logging.error("Failed to download %s: %s", symbol, error)

    if failures:
        logging.warning("No file was created for: %s", ", ".join(failures))
    else:
        logging.info("Completed successfully. Files saved to %s", output_dir)
    return int(bool(failures))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Download daily Indian equity OHLCV data.")
    parser.add_argument("--start", default=DEFAULT_START_DATE, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="End date, exclusive (YYYY-MM-DD)")
    parser.add_argument("--symbols-file", type=Path, default=SYMBOLS_FILE)
    parser.add_argument("--output-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--suffix", choices=[".NS", ".BO", ""], default=".NS")
    parser.add_argument(
        "--provider",
        choices=["groww", "yahoo"],
        default="yahoo",
        help="Market-data source. Groww supports NSE/BSE; Yahoo is used for Nasdaq-100.",
    )
    args = parser.parse_args()
    raise SystemExit(
        main(
            args.start,
            args.end,
            args.symbols_file,
            args.output_dir,
            args.suffix,
            args.provider,
        )
    )
