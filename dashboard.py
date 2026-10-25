"""Streamlit dashboard for NIFTY 50 strategy screening and backtesting."""

from __future__ import annotations

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
    extract_ltp_prices,
    load_historical_stock_prices,
    position_pnl,
    position_value_total,
    statement_performance,
    stock_holding_rows,
    stock_quote_symbols,
)
from strategy import BacktestConfig, load_market_data
from trade_journal import closed_cycles, journal_metrics, load_fills, merge_reviews, open_positions, save_reviews, save_trades


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


def show_groww_table(records: list[dict[str, object]], date_columns: list[str] | None = None) -> None:
    """Render Groww records while formatting dates consistently."""
    if not records:
        st.info("No records returned.")
        return
    frame = pd.DataFrame(records)
    if date_columns:
        for column in date_columns:
            if column in frame.columns:
                frame[column] = pd.to_datetime(frame[column], errors="coerce").dt.strftime("%d-%m-%Y %H:%M")
    st.dataframe(frame, use_container_width=True, hide_index=True)


st.set_page_config(page_title="Swing Trading Dashboard", layout="wide")
st.title("Swing trader workspace")
st.caption("Daily swing setups across NSE, BSE, and Nasdaq-100 · Rule-based signals, trade review, and portfolio monitoring.")

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

with tab_groww:
    st.subheader("Portfolio monitor")
    st.caption("Read-only portfolio data from Groww. No orders are placed by this dashboard.")
    if st.button("Refresh Groww portfolio", type="primary"):
        try:
            with st.spinner("Connecting to Groww..."):
                groww = get_groww_client()
                holdings_response = groww.get_holdings_for_user(timeout=10)
                cash_positions_response = groww.get_positions_for_user(segment="CASH", timeout=10)
                fno_positions_response = groww.get_positions_for_user(segment="FNO", timeout=10)
                orders_response = groww.get_order_list(page=0, page_size=25)
                orders = response_records(orders_response, "order_list")
                trades = []
                for order in orders:
                    order_id = order.get("groww_order_id")
                    if not order_id:
                        continue
                    trade_response = groww.get_trade_list_for_order(
                        groww_order_id=order_id,
                        segment=order.get("segment", "CASH"),
                        page=0,
                        page_size=50,
                        timeout=10,
                    )
                    order_trades = response_records(trade_response, "trade_list")
                    for trade in order_trades:
                        trade.setdefault("segment", order.get("segment", "CASH"))
                        trade.setdefault("groww_order_id", order_id)
                        trade.setdefault("trading_symbol", order.get("trading_symbol") or order.get("trading_symbol_name") or order.get("symbol"))
                    trades.extend(order_trades)
                synced_count, skipped_count = save_trades(DATA_DIR / "trade_journal.sqlite3", trades)
                holdings = response_records(holdings_response, "holdings")
                cash_positions = response_records(cash_positions_response, "positions")
                fno_positions = response_records(fno_positions_response, "positions")
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
                                f"Groww returned quotes for {len(ltp_prices)} of "
                                f"{len(quote_symbols)} held stock symbols."
                            )
                    except Exception as error:
                        quote_error = str(error)
                historical_prices = load_historical_stock_prices(DATA_DIR, holdings)
                st.session_state["groww_portfolio_snapshot"] = {
                    "holdings": holdings,
                    "cash_positions": cash_positions,
                    "fno_positions": fno_positions,
                    "orders": orders,
                    "trades": trades,
                    "ltp_prices": ltp_prices,
                    "historical_prices": historical_prices,
                    "quote_error": quote_error,
                    "synced_count": synced_count,
                    "skipped_count": skipped_count,
                }
            st.success(f"Groww refreshed. {synced_count} new executed trade fill(s) added to the journal.")
        except Exception as error:
            st.error(f"Groww connection failed: {error}")
            st.info("Check .env, Groww API subscription, daily API-key approval, and network access.")

    snapshot = st.session_state.get("groww_portfolio_snapshot")
    if snapshot:
        holdings = snapshot["holdings"]
        cash_positions = snapshot["cash_positions"]
        fno_positions = snapshot["fno_positions"]
        stock_rows = stock_holding_rows(
            holdings,
            snapshot["ltp_prices"],
            snapshot["historical_prices"],
        )
        invested = complete_total(stock_rows, "Invested value")
        current_value = complete_total(stock_rows, "Current value")
        stock_pnl = complete_total(stock_rows, "Unrealized P&L")
        historical_count = sum(row["Price source"] == "Historical close" for row in stock_rows)
        unavailable_count = sum(row["Price source"] == "Unavailable" for row in stock_rows)
        fno_pnl = position_pnl(fno_positions)
        cash_pnl = position_pnl(cash_positions)
        fno_realized_pnl = position_value_total(
            fno_positions,
            ("realized_pnl", "realised_pnl"),
        )

        st.subheader("Portfolio overview")
        overview = st.columns(4)
        overview[0].metric(
            "Stocks · cost basis",
            f"₹{invested:,.2f}" if invested is not None else "Unavailable",
        )
        overview[1].metric(
            "Stocks · value at available prices",
            f"₹{current_value:,.2f}" if current_value is not None else "Unavailable",
        )
        overview[2].metric(
            "Stocks · P&L vs. average cost",
            f"₹{stock_pnl:,.2f}" if stock_pnl is not None else "Unavailable",
        )
        overview[3].metric(
            "F&O · open position P&L",
            f"₹{fno_pnl:,.2f}" if fno_pnl is not None else "Unavailable",
        )

        st.subheader("Stocks · delivery holdings")
        if stock_rows:
            stock_frame = pd.DataFrame(stock_rows)
            st.dataframe(stock_frame, use_container_width=True, hide_index=True)
            if historical_count:
                st.caption(
                    f"{historical_count} holding(s) use the latest downloaded daily close "
                    "because a live Groww quote was unavailable. The price date is shown per row; "
                    "these values are historical, not live."
                )
            if unavailable_count:
                st.warning(
                    f"No live quote or downloaded close was available for {unavailable_count} "
                    "holding(s); those are excluded from the value and P&L totals."
                )
        else:
            st.info("Groww returned no stock holdings.")
        if snapshot["quote_error"]:
            st.warning(
                "Groww live quotes were incomplete: "
                f"{snapshot['quote_error']} Downloaded daily closes are used for those stocks "
                "when available."
            )

        st.subheader("Stocks · open cash positions")
        if cash_positions:
            st.metric(
                "Open cash-position P&L (Groww)",
                f"₹{cash_pnl:,.2f}" if cash_pnl is not None else "Unavailable",
            )
        if cash_positions:
            show_groww_table(cash_positions)
        else:
            st.info("No open cash-segment positions were returned.")

        st.subheader("F&O · open positions")
        if fno_positions:
            st.caption(
                "F&O positions are fetched separately from cash holdings. The P&L metric "
                "uses Groww's position P&L field only when every returned position provides it."
            )
            fno_columns = st.columns(2)
            fno_columns[0].metric(
                "Open F&O position P&L (Groww)",
                f"₹{fno_pnl:,.2f}" if fno_pnl is not None else "Unavailable",
            )
            fno_columns[1].metric(
                "F&O realized P&L on returned positions",
                f"₹{fno_realized_pnl:,.2f}" if fno_realized_pnl is not None else "Unavailable",
            )
            show_groww_table(fno_positions)
        else:
            st.info("No open F&O positions were returned.")

        st.subheader("Mutual funds")
        st.info(
            "Mutual-fund holdings are not exposed by the installed Groww Trade API client. "
            "They are not included in the stock/F&O totals."
        )

        st.subheader("Portfolio XIRR and realized P&L")
        st.metric("Portfolio XIRR", "Unavailable")
        st.caption(
            "A reliable XIRR needs the complete dated portfolio cash-flow ledger "
            "(including deposits and withdrawals) and the complete closing portfolio value. "
            "The Groww Trade API data available here does not provide the full ledger or mutual-fund value."
        )
        st.metric("Lifetime realized P&L", "Unavailable")
        st.caption(
            "Recent order/trade pages are not a complete lifetime transaction history, "
            "so they are not summed as realized portfolio P&L."
        )
        with st.expander("Recent orders and executed trades"):
            st.caption("Recent API records only; these are not used for lifetime P&L or XIRR.")
            st.subheader("Orders")
            show_groww_table(snapshot["orders"], ["created_at", "exchange_time", "trade_date"])
            st.subheader("Executed trades")
            show_groww_table(snapshot["trades"], ["created_at", "trade_date_time"])

        st.subheader("Statement-based XIRR and P&L")
        st.caption(
            "Groww's Trade API does not return a complete transaction history or mutual-fund values. "
            "Upload a normalized CSV to calculate statement-based returns; files are processed in this session only."
        )
        template = "date,asset_class,cash_flow,market_value\n"
        st.download_button(
            "Download normalized CSV template",
            data=template,
            file_name="groww_portfolio_template.csv",
            mime="text/csv",
        )
        st.caption(
            "Use columns date, asset_class, cash_flow, market_value. Purchases are negative cash flows; "
            "sale/redemption proceeds are positive. Put current closing values in market_value on the "
            "valuation date. Allowed sections: Stocks, F&O, Mutual Funds. For an accurate combined XIRR, "
            "include the complete transaction history and value every included section on the same date."
        )
        st.code(
            "date,asset_class,cash_flow,market_value\n"
            "2025-01-15,Stocks,-10000,\n"
            f"{date.today().isoformat()},Stocks,,11500\n"
            "2025-02-01,Mutual Funds,-5000,\n"
            f"{date.today().isoformat()},Mutual Funds,,5600",
            language="csv",
        )
        statement_file = st.file_uploader(
            "Upload normalized portfolio CSV",
            type=["csv"],
            key="groww_statement_csv",
        )
        if statement_file is not None:
            try:
                statement_sections, statement_total = statement_performance(
                    pd.read_csv(statement_file)
                )
            except (ValueError, pd.errors.ParserError, UnicodeDecodeError) as error:
                st.error(f"Could not calculate statement returns: {error}")
            else:
                st.dataframe(
                    statement_sections.style.format({
                        "Invested (buy flows)": "₹{:,.2f}",
                        "Sale / redemption proceeds": "₹{:,.2f}",
                        "Closing value": "₹{:,.2f}",
                        "Total P&L": "₹{:,.2f}",
                        "XIRR %": "{:.2f}%",
                    }),
                    use_container_width=True,
                    hide_index=True,
                )
                if statement_total["valuation_date"] is None:
                    st.warning(
                        "Section returns are shown above. A combined portfolio XIRR/P&L needs "
                        "the same closing valuation date for every included section."
                    )
                else:
                    total_columns = st.columns(3)
                    total_columns[0].metric(
                        "Combined statement P&L",
                        f"₹{statement_total['pnl']:,.2f}",
                    )
                    total_columns[1].metric(
                        "Combined closing value",
                        f"₹{statement_total['closing_value']:,.2f}",
                    )
                    total_columns[2].metric(
                        "Combined XIRR",
                        f"{statement_total['xirr_pct']:.2f}%"
                        if statement_total["xirr_pct"] is not None
                        else "Unavailable",
                    )
                    st.caption(
                        f"Statement valuation date: {indian_date(statement_total['valuation_date'])}. "
                        "XIRR is shown only when the supplied cash flows have a unique solution."
                    )

with tab_journal:
    st.subheader("Trade journal")
    st.caption("Actual executed Groww fills are saved locally in this repository. Sync after a trade to capture it; this dashboard never places orders.")
    if st.button("Sync executed trades from Groww", type="primary", key="journal_sync"):
        st.session_state["journal_sync_requested"] = True
        st.rerun()
    if st.session_state.pop("journal_sync_requested", False):
        try:
            groww = get_groww_client()
            all_orders = []
            for page in range(10):
                page_orders = response_records(groww.get_order_list(page=page, page_size=100), "order_list")
                if not page_orders:
                    break
                all_orders.extend(page_orders)
                if len(page_orders) < 100:
                    break
            fills = []
            for order in all_orders:
                order_id = order.get("groww_order_id")
                if not order_id:
                    continue
                order_trades = response_records(groww.get_trade_list_for_order(
                    groww_order_id=order_id, segment=order.get("segment", "CASH"),
                    page=0, page_size=100, timeout=10), "trade_list")
                for trade in order_trades:
                    trade.setdefault("segment", order.get("segment", "CASH"))
                    trade.setdefault("groww_order_id", order_id)
                    trade.setdefault("trading_symbol", order.get("trading_symbol") or order.get("symbol"))
                fills.extend(order_trades)
            inserted, skipped = save_trades(DATA_DIR / "trade_journal.sqlite3", fills)
            st.success(f"Sync complete: {inserted} new fill(s) saved; {skipped} record(s) lacked usable fill details.")
        except Exception as error:
            st.error(f"Could not sync Groww trades: {error}")
    journal_db = DATA_DIR / "trade_journal.sqlite3"
    all_fills = load_fills(journal_db)
    cycles = merge_reviews(journal_db, closed_cycles(all_fills))
    if all_fills.empty:
        st.info("No executed trades are saved yet. Use **Sync executed trades from Groww** after placing a trade. Historical fills available from Groww will also be imported.")
    else:
        period = st.radio("Analysis period", ["Day", "Month", "Year", "All time"], index=2, horizontal=True, key="journal_period")
        if period == "Year":
            available = sorted(all_fills["trade_at"].dt.year.dropna().unique().tolist(), reverse=True)
            chosen = st.selectbox("Year", available, index=0, key="journal_year")
            start = pd.Timestamp(int(chosen), 1, 1)
            end = pd.Timestamp(int(chosen) + 1, 1, 1)
        elif period == "Month":
            months = sorted(all_fills["trade_at"].dt.to_period("M").dropna().unique(), reverse=True)
            chosen = st.selectbox("Month", months, index=0, key="journal_month")
            start, end = chosen.start_time, chosen.end_time + pd.Timedelta(days=1)
        elif period == "Day":
            days = sorted(all_fills["trade_at"].dt.date.dropna().unique(), reverse=True)
            chosen = st.selectbox("Day", days, index=0, key="journal_day")
            start, end = pd.Timestamp(chosen), pd.Timestamp(chosen) + pd.Timedelta(days=1)
        else:
            start, end = all_fills["trade_at"].min(), all_fills["trade_at"].max() + pd.Timedelta(days=1)
        period_fills = all_fills[(all_fills["trade_at"] >= start) & (all_fills["trade_at"] < end)]
        period_cycles = cycles[(cycles["Exit"] >= start) & (cycles["Exit"] < end)] if not cycles.empty else cycles
        journal_prices = {}
        snapshot = st.session_state.get("groww_portfolio_snapshot")
        if snapshot:
            for row in stock_holding_rows(snapshot["holdings"], snapshot["ltp_prices"], snapshot["historical_prices"]):
                if row["Price"] is not None:
                    journal_prices[str(row["Symbol"]).upper()] = float(row["Price"])
            for position in snapshot["cash_positions"] + snapshot["fno_positions"]:
                symbol = str(position.get("trading_symbol") or position.get("symbol") or "").upper()
                price = pd.to_numeric(position.get("ltp") or position.get("last_price") or position.get("market_price"), errors="coerce")
                if symbol and pd.notna(price) and float(price) > 0:
                    journal_prices[symbol] = float(price)
        metrics = journal_metrics(all_fills, journal_prices)
        closed_count = len(period_cycles)
        gross = float(period_cycles["Gross P&L"].sum()) if closed_count else 0.0
        net = float(period_cycles["Net P&L"].sum()) if closed_count else 0.0
        wins = period_cycles[period_cycles["Net P&L"] > 0] if closed_count else period_cycles
        losses = period_cycles[period_cycles["Net P&L"] < 0] if closed_count else period_cycles
        loss_amount = abs(float(losses["Net P&L"].sum())) if not losses.empty else 0.0
        equity = period_cycles["Net P&L"].cumsum() if closed_count else pd.Series(dtype=float)
        dd = equity - equity.cummax() if not equity.empty else pd.Series(dtype=float)
        reviewed_count = int(period_cycles["Reviewed"].sum()) if closed_count else 0
        adherence = period_cycles["Plan adherence"].dropna() if closed_count else pd.Series(dtype=float)
        xirr_value = metrics["xirr"]
        st.caption(f"Selected period: **{period}** · XIRR is cumulative since the first captured fill.")
        first_row = st.columns(6)
        first_row[0].metric("Closed cycles", f"{closed_count:,}")
        first_row[1].metric("Gross P&L", f"₹{gross:,.2f}")
        first_row[2].metric("Net after recorded costs", f"₹{net:,.2f}")
        first_row[3].metric("Win rate", f"{len(wins) / closed_count:.1%}" if closed_count else "—")
        first_row[4].metric("Profit factor", f"{float(wins['Net P&L'].sum()) / loss_amount:.2f}" if loss_amount else "—")
        first_row[5].metric("Plan adherence", f"{adherence.mean():.0%}" if not adherence.empty else "—")
        second_row = st.columns(4)
        second_row[0].metric("XIRR · since tracking began", f"{xirr_value:.2%}" if xirr_value is not None else "—")
        second_row[1].metric("Expectancy / trade", f"₹{net / closed_count:,.2f}" if closed_count else "—")
        second_row[2].metric("Max drawdown", f"₹{abs(float(dd.min())):,.2f}" if not dd.empty else "₹0.00")
        hold = float(period_cycles["Hold minutes"].mean()) if closed_count else None
        second_row[3].metric("Average hold", f"{hold / 60:.1f} hr" if hold is not None else "—")
        st.metric("Reviews completed", f"{reviewed_count}/{closed_count}")
        st.caption(f"{len(period_fills):,} executed fill(s) in the selected period · {len(all_fills):,} captured in the journal. Costs are included only when Groww provides them in the fill record; open positions are marked using the latest available Groww price when present.")
        st.subheader("Open trades")
        open_frame = open_positions(all_fills, journal_prices)
        if open_frame.empty:
            st.info("No open long positions in the captured fills.")
        else:
            st.dataframe(open_frame, use_container_width=True, hide_index=True)
        st.subheader("Closed trade cycles")
        if not period_cycles.empty:
            editable_cols = ["Cycle ID", "Symbol", "Segment", "Entry", "Exit", "Quantity", "Gross P&L", "Fees", "Net P&L", "Hold minutes", "Plan adherence", "Reviewed", "Notes"]
            edited = st.data_editor(period_cycles[editable_cols], use_container_width=True, hide_index=True,
                disabled=["Cycle ID", "Symbol", "Segment", "Entry", "Exit", "Quantity", "Gross P&L", "Fees", "Net P&L", "Hold minutes"],
                column_config={"Cycle ID": None, "Plan adherence": st.column_config.CheckboxColumn("Plan followed?"), "Reviewed": st.column_config.CheckboxColumn("Review complete"), "Notes": st.column_config.TextColumn("Notes", max_chars=500)})
            if st.button("Save trade reviews", key="save_journal_reviews"):
                save_reviews(journal_db, edited)
                st.success("Trade reviews saved.")
                st.rerun()
        else:
            st.info("No completed buy/sell cycles in this period yet. Open positions will appear after exit fills close a cycle.")
        with st.expander("Captured Groww fills"):
            visible = period_fills[["trade_at", "segment", "symbol", "side", "quantity", "price", "fees", "order_id"]].copy()
            st.dataframe(visible, use_container_width=True, hide_index=True)
