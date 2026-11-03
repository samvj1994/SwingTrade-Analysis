"""Streamlit dashboard for NIFTY 50 strategy screening and backtesting."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from datetime import date
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from groww_connection import get_groww_client
from groww_portfolio import (
    complete_total,
    current_stock_position_rows,
    extract_ltp_prices,
    load_historical_stock_prices,
    stock_quote_symbols,
)
from strategy import BacktestConfig, load_market_data
from trade_journal import (
    load_portfolio_inclusions,
    save_portfolio_inclusions,
)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SENSEX_DIR = DATA_DIR / "BSE"
NASDAQ_DIR = DATA_DIR / "NASDAQ"


def indian_date(value: object) -> str:
    """Format dashboard dates using the Indian day-month-year convention."""
    return pd.Timestamp(value).strftime("%d-%m-%Y")


def response_records(response: object, key: str) -> list[dict[str, object]]:
    """Extract a Groww list payload into records for a Streamlit table."""
    if isinstance(response, dict):
        records = response.get(key, [])
    else:
        records = response
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def fetch_current_stock_positions() -> dict[str, object]:
    """Fetch current stock holdings and available prices, without order history."""
    groww = get_groww_client()
    holdings = response_records(groww.get_holdings_for_user(timeout=10), "holdings")
    quote_symbols = stock_quote_symbols(holdings)
    ltp_prices: dict[str, float] = {}
    quote_error = None
    if quote_symbols:
        try:
            quote_response = groww.get_ltp(
                exchange_trading_symbols=tuple(sorted(quote_symbols)),
                segment="CASH",
                timeout=10,
            )
            ltp_prices = extract_ltp_prices(quote_response, quote_symbols)
            if len(ltp_prices) < len(quote_symbols):
                quote_error = (
                    f"Groww returned prices for {len(ltp_prices)} of "
                    f"{len(quote_symbols)} Indian stock holdings."
                )
        except Exception as error:
            quote_error = str(error)
    return {
        "holdings": holdings,
        "ltp_prices": ltp_prices,
        "historical_prices": load_historical_stock_prices(DATA_DIR, holdings),
        "quote_error": quote_error,
    }


st.set_page_config(page_title="Swing Trading Dashboard", layout="wide")
st.title("Swing trader workspace")
st.caption("Daily swing setups across NSE, BSE, and Nasdaq-100 · Rule-based signals, position selection, and portfolio monitoring.")

with st.sidebar:
    st.header("Market universe")
    selected_exchange = st.selectbox("Scan market", ["All", "NSE", "BSE", "NASDAQ"])
    st.caption("India-market prices are in INR; Nasdaq prices are in USD.")
    st.divider()
    st.header("Market data")
    fetch_start = st.date_input("Data start date", value=date(2024, 1, 1), format="DD-MM-YYYY", help="Use at least one year of warm-up data before the backtest start date for the 220-day EMA and 52-week conditions.")
    fetch_end = st.date_input("Data end date (inclusive)", value=date.today(), format="DD-MM-YYYY")
    if st.button("Refresh all NIFTY 50 data", type="primary", use_container_width=True):
        if fetch_end < fetch_start:
            st.error("The end date must be on or after the start date.")
        else:
            with st.spinner("Downloading daily OHLCV data for all symbols..."):
                # Downloader accepts an exclusive end date.
                end_exclusive = pd.Timestamp(fetch_end) + pd.Timedelta(days=1)
                result = subprocess.run([sys.executable, str(BASE_DIR / "download_nifty50_data.py"), "--start", str(fetch_start), "--end", end_exclusive.strftime("%Y-%m-%d")], capture_output=True, text=True)
            if result.returncode:
                st.error(result.stderr or result.stdout)
            else:
                st.success("Refresh complete.")
                st.rerun()
    if st.button("Refresh all SENSEX data", use_container_width=True):
        if fetch_end < fetch_start:
            st.error("The end date must be on or after the start date.")
        else:
            with st.spinner("Downloading BSE SENSEX data..."):
                end_exclusive = pd.Timestamp(fetch_end) + pd.Timedelta(days=1)
                result = subprocess.run([
                    sys.executable, str(BASE_DIR / "download_nifty50_data.py"),
                    "--start", str(fetch_start), "--end", end_exclusive.strftime("%Y-%m-%d"),
                    "--symbols-file", str(BASE_DIR / "sensex_symbols.csv"),
                    "--output-dir", str(SENSEX_DIR), "--suffix", ".BO",
                ], capture_output=True, text=True)
            if result.returncode:
                st.error(result.stderr or result.stdout)
            else:
                st.success("SENSEX refresh complete.")
                st.rerun()
    if st.button("Refresh all Nasdaq-100 data", use_container_width=True):
        if fetch_end < fetch_start:
            st.error("The end date must be on or after the start date.")
        else:
            with st.spinner("Downloading daily OHLCV data for Nasdaq-100 constituents..."):
                end_exclusive = pd.Timestamp(fetch_end) + pd.Timedelta(days=1)
                result = subprocess.run([
                    sys.executable, str(BASE_DIR / "download_nifty50_data.py"),
                    "--start", str(fetch_start), "--end", end_exclusive.strftime("%Y-%m-%d"),
                    "--symbols-file", str(BASE_DIR / "nasdaq100_symbols.csv"),
                    "--output-dir", str(NASDAQ_DIR), "--suffix", "",
                ], capture_output=True, text=True)
            if result.returncode:
                st.error(result.stderr or result.stdout or "Some Nasdaq-100 symbols failed to refresh.")
            else:
                st.success("Nasdaq-100 refresh complete.")
                st.rerun()


try:
    market_data = load_market_data(DATA_DIR)
except FileNotFoundError:
    st.info("No local price data yet. Select a date range and refresh one of the market universes.")
    st.stop()

latest_rows = []
stop_loss_pct = BacktestConfig().stop_loss_pct
criteria_labels = {
    "Trend_Above_EMA": "150 SMA > 220 EMA",
    "Close_Above_SMA_50": "Close > 50 SMA",
    "SMA_50_Above_SMA_150": "50 SMA > 150 SMA",
    "Above_125pct_52W_Low": "Close > 125% of 52W low",
    "Dipped_Below_EMA_90D": "Dipped below 220 EMA",
    "Breakout_Above_52W_High": "Breakout above 52W high",
}
for symbol, data in market_data.items():
    market_parts = symbol.split("/", 2)
    if len(market_parts) == 3:
        exchange, index_name, display_symbol = market_parts
    else:
        exchange, index_name, display_symbol = "NSE", "NIFTY 50", symbol
    latest = data.iloc[-1]
    missing = [label for column, label in criteria_labels.items() if not bool(latest[column])]
    latest_rows.append({
        "Exchange": exchange,
        "Index": index_name,
        "Symbol": display_symbol,
        "Data bars": len(data),
        "Data status": "Strategy-ready" if len(data) >= 309 else "Needs more history",
        "Criteria met": int(latest["Criteria_Met"]),
        "Status": "Qualified" if bool(latest["Entry_Signal"]) else "Near qualifying",
        "Readiness": "Ready to trade" if bool(latest["Entry_Signal"]) else ("Watch closely" if int(latest["Criteria_Met"]) >= 4 else "Needs more confirmation"),
        "Signal date": indian_date(latest["Date"]),
        "Close": latest["Close"],
        "Reference entry": latest["Close"],
        "Stop reference": latest["Close"] * (1 - stop_loss_pct),
        "Currency": "USD" if exchange == "NASDAQ" else "INR",
        "SMA 50": latest["SMA_50"],
        "SMA 150": latest["SMA_150"],
        "EMA 220": latest["EMA_220"],
        "220 EMA exit": latest["EMA_220"],
        "Exit status": "EMA exit active" if latest["Close"] < latest["EMA_220"] else "Hold above exit levels",
        "52-week low": latest["Low_52W"],
        "Missing criteria": ", ".join(missing) if missing else "None",
        "Trend": "PASS" if bool(latest["Trend_Above_EMA"]) else "WAIT",
        "Close > 50 SMA": "PASS" if bool(latest["Close_Above_SMA_50"]) else "WAIT",
        "50 SMA > 150 SMA": "PASS" if bool(latest["SMA_50_Above_SMA_150"]) else "WAIT",
        ">125% of 52W low": "PASS" if bool(latest["Above_125pct_52W_Low"]) else "WAIT",
        "EMA dip": "PASS" if bool(latest["Dipped_Below_EMA_90D"]) else "WAIT",
        "52W breakout": "PASS" if bool(latest["Breakout_Above_52W_High"]) else "WAIT",
    })

last_bar = max(data["Date"].max() for data in market_data.values())
price_files = list(DATA_DIR.glob("*.csv")) + list(SENSEX_DIR.glob("*.csv")) + list(NASDAQ_DIR.glob("*.csv"))
file_refresh = max(path.stat().st_mtime for path in price_files)
st.caption(f"Last price bar: **{indian_date(last_bar)}** · Last refresh: **{pd.Timestamp(file_refresh, unit='s').strftime('%d-%m-%Y %H:%M:%S')}**")

tab_screen, tab_journal, tab_groww = st.tabs([
    ":material/radar: Market scan",
    ":material/edit_note: Trade journal",
    ":material/account_balance_wallet: Portfolio",
])
with tab_screen:
    if latest_rows:
        all_ranked = pd.DataFrame(latest_rows).sort_values(
            ["Criteria met", "Status", "Symbol"],
            ascending=[False, True, True],
        )
        ranked = all_ranked if selected_exchange == "All" else all_ranked[all_ranked["Exchange"] == selected_exchange]
        qualified = ranked[ranked["Status"] == "Qualified"]
        near_qualifying = ranked[ranked["Status"] == "Near qualifying"]
        st.subheader("Setup overview")
        st.caption(f"{selected_exchange} universe · Daily timeframe · Six-point trend and breakout checklist")
        summary_columns = st.columns(4)
        summary_columns[0].metric("Entry signals", len(qualified), help="All six strategy conditions pass on the latest available close.")
        summary_columns[1].metric("Watchlist 4+", len(near_qualifying[near_qualifying["Criteria met"] >= 4]))
        summary_columns[2].metric("Avg. checklist", f"{ranked['Criteria met'].mean():.1f}/6")
        summary_columns[3].metric("Symbols scanned", f"{len(ranked):,}")
        incomplete = ranked[ranked["Data bars"] < 309]
        if not incomplete.empty:
            st.warning(f"{len(incomplete)} stock(s) have fewer than 309 trading bars and cannot fully evaluate the 220-session EMA and 90-session pullback conditions yet.")

        st.subheader("Active entry signals")
        if qualified.empty:
            st.info("No stocks currently meet all six entry conditions.")
        else:
            signal_details = qualified[
                [
                    "Exchange",
                    "Index",
                    "Symbol",
                    "Signal date",
                    "Reference entry",
                    "Stop reference",
                    "220 EMA exit",
                    "Currency",
                ]
            ].rename(
                columns={
                    "Exchange": "Market",
                    "Symbol": "Ticker",
                    "Reference entry": "Signal-close reference",
                    "Stop reference": f"Illustrative stop ({stop_loss_pct:.0%} below close)",
                    "220 EMA exit": "220 EMA trigger at signal close",
                }
            )
            st.dataframe(
                signal_details.style.format(
                    {
                        "Signal-close reference": "{:,.2f}",
                        f"Illustrative stop ({stop_loss_pct:.0%} below close)": "{:,.2f}",
                        "220 EMA trigger at signal close": "{:,.2f}",
                    }
                ),
                hide_index=True,
                width="stretch",
                alt="Qualified swing-trading signals with reference entry and exit levels",
            )
            st.caption(
                f"Signals are confirmed at the daily close. The signal-close price is a reference, not a guaranteed entry; "
                f"the backtest enters at the next session's open. The illustrative stop shown is {stop_loss_pct:.0%} below that close; "
                "recalculate it from the actual entry fill. The 220 EMA is a moving exit trigger: a close below it queues a sell for "
                "the next session's open. The actual exit fill is not known in advance, and there is no fixed profit target."
            )

        chart_columns = st.columns(2)
        with chart_columns[0]:
            st.subheader("How many checks are passing?")
            score_distribution = (
                ranked["Criteria met"]
                .value_counts()
                .reindex(range(7), fill_value=0)
                .rename_axis("Checks passed")
                .reset_index(name="Stocks")
            )
            score_distribution["Checks passed"] = score_distribution["Checks passed"].map(
                lambda score: f"{score}/6"
            )
            st.bar_chart(
                score_distribution,
                x="Checks passed",
                y="Stocks",
                alt="Number of stocks passing zero to six swing-trading checks",
            )
            st.caption("A stock must pass all 6 checks to show an entry signal. Four or five checks means it is worth reviewing, not an automatic buy.")

        with chart_columns[1]:
            st.subheader("Checklist health")
            checklist_columns = {
                "Long trend is rising": "Trend",
                "Price above 50-day average": "Close > 50 SMA",
                "50-day trend above 150-day": "50 SMA > 150 SMA",
                "At least 25% above 52-week low": ">125% of 52W low",
                "Pullback within 90 days": "EMA dip",
                "New 52-week closing high": "52W breakout",
            }
            checklist_health = pd.DataFrame(
                [
                    {
                        "Checklist item": label,
                        "Passing (%)": round((ranked[column] == "PASS").mean() * 100, 1),
                    }
                    for label, column in checklist_columns.items()
                ]
            )
            checklist_chart = (
                alt.Chart(checklist_health)
                .mark_bar()
                .encode(
                    x=alt.X("Passing (%)", scale=alt.Scale(domain=[0, 100])),
                    y=alt.Y("Checklist item", sort="-x", axis=alt.Axis(labelLimit=220)),
                    tooltip=["Checklist item", "Passing (%)"],
                )
                .properties(height=250)
            )
            st.altair_chart(
                checklist_chart,
                width="stretch",
                alt="Share of scanned stocks passing each swing-trading checklist item",
            )
            st.caption("Higher bars mean more stocks in this market currently pass that individual rule.")

        st.subheader("Price and trend lines")
        st.caption("Choose a symbol to compare its closing price with the strategy's moving averages. These are historical daily prices, not live quotes.")
        chart_symbols: dict[str, pd.DataFrame] = {}
        for symbol_key, symbol_data in market_data.items():
            exchange, _, display_symbol = symbol_key.split("/", 2)
            if selected_exchange == "All" or exchange == selected_exchange:
                chart_symbols[f"{exchange} · {display_symbol}"] = symbol_data
        if chart_symbols:
            chart_controls = st.columns([2, 1])
            selected_chart_symbol = chart_controls[0].selectbox(
                "Stock to chart",
                list(chart_symbols),
                key="price_chart_symbol",
            )
            chart_period = chart_controls[1].selectbox(
                "Chart period",
                ["3 months", "6 months", "1 year"],
                index=1,
                key="price_chart_period",
            )
            period_days = {"3 months": 92, "6 months": 183, "1 year": 365}
            symbol_history = chart_symbols[selected_chart_symbol]
            chart_cutoff = symbol_history["Date"].max() - pd.Timedelta(
                days=period_days[chart_period]
            )
            price_chart = (
                symbol_history.loc[
                    symbol_history["Date"] >= chart_cutoff,
                    ["Date", "Close", "SMA_50", "SMA_150", "EMA_220"],
                ]
                .rename(
                    columns={
                        "Close": "Closing price",
                        "SMA_50": "50-day average",
                        "SMA_150": "150-day average",
                        "EMA_220": "220-day trend line",
                    }
                )
                .dropna(subset=["Closing price"])
            )
            price_latest = price_chart.iloc[-1]
            chart_currency = "USD" if selected_chart_symbol.startswith("NASDAQ") else "INR"
            trend_value = price_latest["220-day trend line"]
            latest_close = float(price_latest["Closing price"])
            trend_caption = "A positive percentage means the close is above the 220-day trend; a negative percentage means it is below."
            if pd.notna(trend_value) and float(trend_value) != 0:
                trend_delta = (latest_close / float(trend_value) - 1) * 100
                st.metric(
                    f"Latest close · {chart_currency}",
                    f"{chart_currency} {latest_close:,.2f}",
                    f"{trend_delta:+.1f}% vs. 220-day trend",
                )
            else:
                st.metric(f"Latest close · {chart_currency}", f"{chart_currency} {latest_close:,.2f}")
            st.line_chart(
                price_chart,
                x="Date",
                y=[
                    "Closing price",
                    "50-day average",
                    "150-day average",
                    "220-day trend line",
                ],
                height=360,
                alt=f"{selected_chart_symbol} closing price and strategy trend averages",
            )
            st.caption(trend_caption)
            st.caption("Moving averages smooth daily price changes: the 50-day line shows the shorter trend; the 150-day and 220-day lines show longer-term direction.")

        def color_status(value: str) -> str:
            if value == "Ready to trade":
                return "background-color: #dcfce7; color: #166534; font-weight: 700"
            if value == "Watch closely":
                return "background-color: #fef3c7; color: #92400e; font-weight: 700"
            return "background-color: #fee2e2; color: #991b1b"

        def color_pass(value: str) -> str:
            return "background-color: #dcfce7; color: #166534; font-weight: 700" if value == "PASS" else "background-color: #fee2e2; color: #991b1b"

        def color_score(value: int) -> str:
            score_colors = {
                0: ("#fee2e2", "#991b1b"),
                1: ("#fee2e2", "#991b1b"),
                2: ("#ffedd5", "#9a3412"),
                3: ("#fef3c7", "#92400e"),
                4: ("#fef3c7", "#92400e"),
                5: ("#dcfce7", "#166534"),
                6: ("#bbf7d0", "#14532d"),
            }
            background, text = score_colors.get(int(value), ("#f3f4f6", "#374151"))
            return f"background-color: {background}; color: {text}; font-weight: 700"

        def style_screen(frame: pd.DataFrame):
            return (
                frame.style
                .map(color_status, subset=["Readiness"])
                .map(color_pass, subset=["Trend", "Close > 50 SMA", "50 SMA > 150 SMA", ">125% of 52W low", "EMA dip", "52W breakout"])
                .map(color_score, subset=["Criteria met"])
                .format("{:.2f}", subset=["Close", "SMA 50", "SMA 150", "EMA 220", "220 EMA exit", "52-week low"])
            )

        st.subheader("Top setups to review")
        top_setups = ranked.head(10)[
            ["Exchange", "Index", "Symbol", "Signal date", "Close", "Currency", "Criteria met", "Readiness", "Missing criteria"]
        ].rename(
            columns={
                "Criteria met": "Checks passed",
                "Readiness": "Setup status",
                "Close": "Latest close",
                "Missing criteria": "Checks still needed",
            }
        )
        top_setups["Checks passed"] = top_setups["Checks passed"].map(lambda score: f"{score}/6")
        st.dataframe(
            top_setups,
            hide_index=True,
            width="stretch",
            alt="Top ten stocks ranked by swing strategy checks passed",
        )
        st.caption("This is a research shortlist, not a buy recommendation. Review the missing checks and your own risk plan before trading.")

        with st.expander("Detailed strategy checklist", expanded=False):
            st.caption("PASS means a rule is met on the latest available daily close; WAIT means it is not yet met.")
            st.subheader("Stocks meeting all criteria")
            if qualified.empty:
                st.info("No stocks currently meet all criteria on their latest available close.")
            else:
                st.dataframe(
                    style_screen(qualified.set_index(["Exchange", "Index", "Symbol"])),
                    width="stretch",
                    alt="Stocks passing all six swing strategy checks",
                )
            st.subheader("Near-qualifying stocks")
            st.dataframe(
                style_screen(near_qualifying.set_index(["Exchange", "Index", "Symbol"])),
                width="stretch",
                alt="Stocks near qualification, with individual strategy checks",
            )

with tab_journal:
    st.subheader("Current stock positions")
    st.caption("Only current stock holdings are shown. F&O, orders, completed trades, and transaction history are excluded.")
    journal_db = DATA_DIR / "trade_journal.sqlite3"
    if st.button("Fetch current positions from Groww", type="primary", key="journal_fetch_positions"):
        try:
            with st.spinner("Fetching current stock holdings..."):
                st.session_state["current_stock_positions_snapshot"] = (
                    fetch_current_stock_positions()
                )
            st.session_state["current_positions_message"] = (
                "Current stock positions fetched from Groww."
            )
            st.rerun()
        except Exception as error:
            st.error(f"Could not fetch current Groww stock positions: {error}")

    message = st.session_state.pop("current_positions_message", None)
    if message:
        st.success(message)
    snapshot = st.session_state.get("current_stock_positions_snapshot")
    if not snapshot:
        st.info("Fetch current positions from Groww to see your stock holdings.")
    else:
        position_rows = current_stock_position_rows(
            snapshot["holdings"],
            snapshot["ltp_prices"],
            snapshot["historical_prices"],
        )
        if not position_rows:
            st.info("Groww returned no current NSE/BSE or identifiable US stock holdings.")
        else:
            inclusions = load_portfolio_inclusions(journal_db)
            editor_frame = pd.DataFrame(
                [
                    {
                        "Market": row["Market"],
                        "Symbol": row["Symbol"],
                        "Currency": row["Currency"],
                        "Quantity": row["Quantity"],
                        "Average cost": row["Average cost"],
                        "Current price": row["Current price"],
                        "Include in portfolio": inclusions.get(
                            str(row["Position key"]), False
                        ),
                    }
                    for row in position_rows
                ]
            )
            editor_key = hashlib.sha256(
                "|".join(str(row["Position key"]) for row in position_rows).encode()
            ).hexdigest()[:12]
            edited_positions = st.data_editor(
                editor_frame,
                key=f"current_positions_{editor_key}",
                hide_index=True,
                width="stretch",
                disabled=[
                    "Market",
                    "Symbol",
                    "Currency",
                    "Quantity",
                    "Average cost",
                    "Current price",
                ],
                column_config={
                    "Quantity": st.column_config.NumberColumn(format="%.4f"),
                    "Average cost": st.column_config.NumberColumn(format="%.2f"),
                    "Current price": st.column_config.NumberColumn(format="%.2f"),
                    "Include in portfolio": st.column_config.CheckboxColumn(
                        "Include in portfolio"
                    ),
                },
                alt="Current stock positions with portfolio inclusion checkboxes",
            )
            updated_inclusions = {
                str(row["Position key"]): bool(included)
                for row, included in zip(
                    position_rows,
                    edited_positions["Include in portfolio"],
                )
            }
            saved_inclusions = {
                str(row["Position key"]): inclusions.get(
                    str(row["Position key"]), False
                )
                for row in position_rows
            }
            if updated_inclusions != saved_inclusions:
                save_portfolio_inclusions(journal_db, updated_inclusions)
                st.rerun()
            st.caption("Checkbox changes save automatically and update Portfolio.")
            if not any(row["Currency"] == "USD" for row in position_rows):
                st.caption(
                    "US stocks appear only when Groww returns them as current holdings "
                    "with an identifiable US exchange or USD currency."
                )
            if snapshot.get("quote_error"):
                st.warning(
                    f"Groww live quotes were incomplete: {snapshot['quote_error']} "
                    "Available prices from the holding data or downloaded daily closes are used."
                )

with tab_groww:
    st.subheader("Selected stock portfolio")
    st.caption("Only the current stock positions selected in Trade journal are included here.")
    snapshot = st.session_state.get("current_stock_positions_snapshot")
    if not snapshot:
        st.info("Fetch current positions in Trade journal to build your selected portfolio.")
    else:
        inclusions = load_portfolio_inclusions(DATA_DIR / "trade_journal.sqlite3")
        position_rows = current_stock_position_rows(
            snapshot["holdings"],
            snapshot["ltp_prices"],
            snapshot["historical_prices"],
        )
        selected_rows = [
            row for row in position_rows if inclusions.get(row["Position key"], False)
        ]
        if selected_rows:
            positions_frame = pd.DataFrame(selected_rows)
            st.dataframe(
                positions_frame[
                    [
                        "Market",
                        "Symbol",
                        "Currency",
                        "Quantity",
                        "Average cost",
                        "Invested value",
                        "Current price",
                        "Market value",
                        "Unrealized P&L",
                    ]
                ].style.format(
                    {
                        "Quantity": "{:,.4f}",
                        "Average cost": "{:,.2f}",
                        "Invested value": "{:,.2f}",
                        "Current price": "{:,.2f}",
                        "Market value": "{:,.2f}",
                        "Unrealized P&L": "{:,.2f}",
                    },
                    na_rep="—",
                ),
                hide_index=True,
                width="stretch",
                alt="Current stock positions selected for the portfolio",
            )
            for currency, currency_rows in positions_frame.groupby("Currency"):
                symbol = "$" if currency == "USD" else "₹"
                currency_columns = st.columns(3)
                for column, field, label in zip(
                    currency_columns,
                    ("Invested value", "Market value", "Unrealized P&L"),
                    ("Invested value", "Current value", "Unrealized P&L"),
                ):
                    total = complete_total(currency_rows.to_dict("records"), field)
                    column.metric(
                        f"{label} · {currency}",
                        f"{symbol}{total:,.2f}" if total is not None else "Unavailable",
                    )
        else:
            st.info("No positions are selected. Tick positions in Trade journal to include them.")
