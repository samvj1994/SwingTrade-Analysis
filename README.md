# SwingTrade Analysis

## Run locally on macOS

Install Python 3.11 or 3.12. In Terminal, go to the project folder and create
an isolated environment:

```bash
cd /path/to/SwingTrade-Analysis
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
streamlit run dashboard.py
```

Open `http://localhost:8501` in your browser. Stop the dashboard with
Control-C in Terminal.

The dashboard uses a dark financial-workstation theme configured in
`.streamlit/config.toml`.

On first run, use the sidebar date range and refresh NIFTY 50, SENSEX, and
Nasdaq-100 to download price history. NSE/NIFTY 50 files are stored in `data/`,
BSE/SENSEX files in `data/BSE/`, and Nasdaq-100 files in `data/NASDAQ/`. Nasdaq
prices are in USD; India-market prices are in INR. These
downloaded files are local and excluded from Git, so download them on each
computer. The signal scan needs sufficient warm-up history for its 52-week
range and 220-session EMA.

The Nasdaq-100 ticker universe is in `nasdaq100_symbols.csv`. It follows
Nasdaq's published constituent list and is adjusted for Nasdaq's announced
2026 additions and replacements. Constituent membership can change separately
from the daily price refresh, so update this file after future index changes.

## Groww API credentials

Groww is optional; market screening works without it. To enable the Groww
portfolio and trading-journal tabs, copy the example file and edit it locally:

```bash
cp .env.example .env
```

Set either `GROWW_ACCESS_TOKEN`, or both `GROWW_API_KEY` and
`GROWW_API_SECRET` in `.env`. Use only one authentication flow. Access tokens
expire according to the date shown in Groww; replace an expired token and
restart Streamlit. Never put real credentials in Python files or commit `.env`.

## Current positions and portfolio

Open **Trade journal** and choose **Fetch current positions from Groww**. The
journal shows only current positive-quantity NSE/BSE stock holdings and US
stock holdings when Groww returns them with an identifiable US exchange or USD
currency. It does not fetch orders, F&O positions, completed trades, or
transaction history. Current Indian stock prices use Groww quotes when
available, then the latest downloaded daily close; US prices are shown only
when included in Groww's holding data.

Use the **Include in portfolio** checkbox to select positions for the
**Portfolio** tab. Choices are saved locally in
`data/trade_journal.sqlite3` and retained when positions are fetched again.
Portfolio values are grouped by currency so INR and USD amounts are not added
together. The dashboard does not place orders.

## Strategy implemented

At each close, a stock must satisfy all filters: SMA 150 > EMA 220, close > SMA
50, SMA 50 > SMA 150, close > 125% of the rolling 52-week low, and its low has
fallen below the EMA 220 at least once in the preceding 90 sessions. An entry
signal additionally requires a close above the previous 252-session closing
high. Buy orders execute at the next session open.

The dashboard applies the same signal rules to NIFTY 50, SENSEX, and Nasdaq-100 constituents. It does not place or simulate orders.

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
  download_nifty50_data.py groww_connection.py groww_portfolio.py trade_journal.py \
  nifty50_symbols.csv sensex_symbols.csv nasdaq100_symbols.csv .env.example
git status --short
git commit -m "Prepare dashboard for cross-platform local use"
git push -u origin main
```

Clone the repository on the Mac, then follow the macOS setup commands above.
