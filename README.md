# NIFTY 50 breakout strategy

## Run locally on macOS

Install Python 3.11 or 3.12. In Terminal, go to the project folder and create
an isolated environment:

```bash
cd /path/to/Algo-Nifty-50
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
streamlit run dashboard.py
```

Open `http://localhost:8501` in your browser. Stop the dashboard with
Control-C in Terminal.

On first run, use the sidebar date range and click **Refresh all NIFTY 50 data**
and/or **Refresh all SENSEX data** to download price history. NSE/NIFTY 50 files
are stored in `data/`; BSE/SENSEX files are stored in `data/BSE/`. These
downloaded files are local and excluded from Git, so download them on each
computer. A 2024 start date is recommended when backtesting from 2025 because
the rules need 252 trading days for the 52-week range and 220 sessions for the
EMA.

## Groww API credentials

Groww is optional; screening and backtesting work without it. To enable the
Groww portfolio tab, copy the example file and edit it locally:

```bash
cp .env.example .env
```

Set either `GROWW_ACCESS_TOKEN`, or both `GROWW_API_KEY` and
`GROWW_API_SECRET` in `.env`. Use only one authentication flow. Access tokens
expire according to the date shown in Groww; replace an expired token and
restart Streamlit. Never put real credentials in Python files or commit `.env`.

The Groww portfolio tab separates delivery-stock holdings, cash positions, and
F&O positions. Stock cost basis is derived from Groww's quantity and average
price; when a live quote is unavailable, the latest downloaded daily close is
used as a clearly dated historical fallback. The installed Groww Trade API
does not provide mutual-fund holdings or a complete transaction/cash-flow
ledger. The dashboard marks those API-only totals unavailable instead of
estimating them. A normalized CSV template in the Groww portfolio tab can be
used to calculate statement-based section and combined XIRR/P&L. Include
complete signed investment cash flows and closing values, with the same
valuation date for every section included in combined results. The CSV columns
are `date`, `asset_class`, `cash_flow`, and `market_value`; purchases are
negative and sale/redemption proceeds are positive.

## Strategy implemented

At each close, a stock must satisfy all filters: SMA 150 > EMA 220, close > SMA
50, SMA 50 > SMA 150, close > 125% of the rolling 52-week low, and its low has
fallen below the EMA 220 at least once in the preceding 90 sessions. An entry
signal additionally requires a close above the previous 252-session closing
high. Buy orders execute at the next session open.

Positions are exited at the next session open after the close is below the EMA
220 or 8% below the entry price. The portfolio starts with $100,000 and limits
each position to $10,000 by default. Both values can be adjusted in the
dashboard. Fees/slippage may be supplied as a per-side percentage.

## Push to GitHub

The `.gitignore` excludes `.env`, downloaded `data/`, generated `outputs/`,
local `work/` caches, and virtual environments. For an empty GitHub repository,
initialize this project folder and add the repository as `origin`. If the
GitHub repository already contains commits/files, clone it first and copy the
project files into that clone instead of initializing a separate history.
Review the staged file list before pushing; do not stage `.env` or personal
data:

```bash
git init -b main
git remote add origin https://github.com/USERNAME/REPOSITORY.git
git status --short
git add .gitignore README.md requirements.txt dashboard.py strategy.py \
  download_nifty50_data.py groww_connection.py groww_portfolio.py \
  nifty50_symbols.csv sensex_symbols.csv .env.example
git status --short
git commit -m "Prepare dashboard for cross-platform local use"
git push -u origin main
```

Clone the repository on the Mac, then follow the macOS setup commands above.
