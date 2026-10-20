"""Streamlit dashboard for NIFTY 50 strategy screening and backtesting."""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

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
from strategy import BacktestConfig, load_market_data, run_backtest


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SENSEX_DIR = DATA_DIR / "BSE"


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


st.set_page_config(page_title="NSE & SENSEX Swing Trading", layout="wide")
st.title("NSE & SENSEX Swing Trading Dashboard")
st.caption("Rule-based swing signals for NIFTY 50 and SENSEX stocks. Signals use each session's close; trade entries and exits execute at the next session's open.")

with st.sidebar:
    st.header("Data refresh")
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

    st.divider()
    st.header("Backtest settings")
    initial_capital = st.number_input("Initial capital", min_value=1_000.0, value=100_000.0, step=1_000.0)
    allocation_pct = st.number_input("Target allocation per stock (%)", min_value=0.1, max_value=100.0, value=10.0, step=0.5)
    max_position = initial_capital * allocation_pct / 100
    stop_loss_pct = st.number_input("Stop-loss (%)", min_value=0.1, max_value=100.0, value=8.0, step=0.5) / 100
    fee_pct = st.number_input("Fee / slippage per side (%)", min_value=0.0, value=0.1, step=0.01) / 100

try:
    market_data = load_market_data(DATA_DIR)
except FileNotFoundError:
    st.info("No local price data yet. Select a date range and click **Refresh all NIFTY 50 data**.")
    st.stop()

latest_rows = []
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
        "Data status": "Strategy-ready" if len(data) >= 252 else "Needs more history",
        "Criteria met": int(latest["Criteria_Met"]),
        "Status": "Qualified" if bool(latest["Entry_Signal"]) else "Near qualifying",
        "Readiness": "Ready to trade" if bool(latest["Entry_Signal"]) else ("Watch closely" if int(latest["Criteria_Met"]) >= 4 else "Needs more confirmation"),
        "Signal date": indian_date(latest["Date"]),
        "Close": latest["Close"],
        "Portfolio %": allocation_pct,
        "Suggested amount": initial_capital * allocation_pct / 100,
        "Suggested shares": int((initial_capital * allocation_pct / 100) / latest["Close"])
        if pd.notna(latest["Close"]) and latest["Close"] > 0 else 0,
        "SMA 50": latest["SMA_50"],
        "SMA 150": latest["SMA_150"],
        "EMA 220": latest["EMA_220"],
        "Stop-loss exit": latest["Close"] * (1 - stop_loss_pct),
        "220 EMA exit": latest["EMA_220"],
        "Exit status": "EMA exit active" if latest["Close"] < latest["EMA_220"] else "Hold above exit levels",
        "Exit rule": f"Close < {stop_loss_pct:.0%} stop or 220 EMA",
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
file_refresh = max(path.stat().st_mtime for path in list(DATA_DIR.glob("*.csv")) + list(SENSEX_DIR.glob("*.csv")))
st.caption(f"Last price bar: **{indian_date(last_bar)}** · Last refresh: **{pd.Timestamp(file_refresh, unit='s').strftime('%d-%m-%Y %H:%M:%S')}**")

tab_screen, tab_backtest, tab_groww = st.tabs(["NSE & BSE screening", "Portfolio backtest", "Groww portfolio"])
with tab_screen:
    if latest_rows:
        all_ranked = pd.DataFrame(latest_rows).sort_values(
            ["Criteria met", "Status", "Symbol"],
            ascending=[False, True, True],
        )
        selected_exchange = st.selectbox("Exchange", ["All", "NSE", "BSE"])
        ranked = all_ranked if selected_exchange == "All" else all_ranked[all_ranked["Exchange"] == selected_exchange]
        qualified = ranked[ranked["Status"] == "Qualified"]
        near_qualifying = ranked[ranked["Status"] == "Near qualifying"]
        st.subheader("Stocks closest to meeting every entry criterion")
        summary_columns = st.columns(4)
        summary_columns[0].metric("Ready to trade", len(qualified))
        summary_columns[1].metric("Watch closely (4-5/6)", len(near_qualifying[near_qualifying["Criteria met"] >= 4]))
        summary_columns[2].metric("Average criteria met", f"{ranked['Criteria met'].mean():.1f}/6")
        summary_columns[3].metric("Allocation per stock", f"{allocation_pct:.1f}%")
        incomplete = ranked[ranked["Data bars"] < 252]
        if not incomplete.empty:
            st.warning(f"{len(incomplete)} stock(s) have fewer than 252 trading bars and cannot fully evaluate the 52-week strategy conditions yet.")

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
                .format("{:.2f}", subset=["Close", "Portfolio %", "Suggested amount", "SMA 50", "SMA 150", "EMA 220", "Stop-loss exit", "220 EMA exit", "52-week low"])
            )

        st.subheader("Stocks meeting all criteria")
        if qualified.empty:
            st.info("No stocks currently meet all criteria on their latest available close.")
        else:
            st.dataframe(
                style_screen(qualified.set_index(["Exchange", "Index", "Symbol"])),
                use_container_width=True,
            )
        st.subheader("Near-qualifying stocks")
        st.dataframe(
            style_screen(near_qualifying.set_index(["Exchange", "Index", "Symbol"])),
            use_container_width=True,
        )

with tab_groww:
    st.subheader("Live Groww account")
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
                    trades.extend(response_records(trade_response, "trade_list"))
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
                }
            st.success("Groww portfolio data refreshed.")
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

with tab_backtest:
    all_dates = pd.concat([data["Date"] for data in market_data.values()])
    min_date, max_date = all_dates.min().date(), all_dates.max().date()
    default_start = max(min_date, date(2025, 1, 1))
    backtest_dates = st.date_input(
        "Backtest period",
        value=(default_start, max_date),
        min_value=min_date,
        max_value=max_date,
        format="DD-MM-YYYY",
        key="portfolio_backtest_period",
        help="Both dates are inclusive. The strategy indicators use all downloaded history before the selected start date.",
    )
    st.caption(f"Downloaded data available from **{indian_date(min_date)}** to **{indian_date(max_date)}**.")
    if len(backtest_dates) == 2:
        backtest_start, backtest_end = backtest_dates
        config = BacktestConfig(initial_capital=initial_capital, max_position_value=max_position, stop_loss_pct=stop_loss_pct, fee_rate=fee_pct)
        equity, trades, metrics = run_backtest(market_data, backtest_start, backtest_end, config)
        st.caption(f"Selected period: **{indian_date(backtest_start)}** to **{indian_date(backtest_end)}**")
        st.dataframe(metrics.style.format({"Initial Capital": "{:.2f}", "Final Equity": "{:.2f}", "Return %": "{:.2f}", "Win Rate %": "{:.2f}"}), use_container_width=True, hide_index=True)
        if not equity.empty:
            chart_data = equity.assign(Date=equity["Date"].map(indian_date)).set_index("Date")
            st.line_chart(chart_data["Equity"])
        st.subheader("Closed trades")
        if trades.empty:
            st.info("No positions were closed in this period.")
        else:
            st.dataframe(trades.style.format({"Entry Date": indian_date, "Exit Date": indian_date, "Entry Price": "{:.2f}", "Exit Price": "{:.2f}", "PnL": "{:.2f}"}), use_container_width=True, hide_index=True)
