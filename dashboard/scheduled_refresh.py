"""Refresh NIFTY 50, SENSEX, and Nasdaq-100 history for the scheduled dashboard."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
IST = ZoneInfo("Asia/Kolkata")


def main() -> int:
    now = datetime.now(IST)
    if now.weekday() >= 5 or not (9 <= now.hour < 11):
        print("Outside weekday refresh window 09:00–11:00 IST; skipping.")
        return 0

    DATA.mkdir(parents=True, exist_ok=True)
    status_path = DATA / "auto_refresh_status.json"
    status_path.write_text(json.dumps({"date": now.date().isoformat(), "state": "running"}))
    end = (now.date() + timedelta(days=1)).isoformat()
    jobs = [
        ("NIFTY 50", ROOT / "nifty50_symbols.csv", DATA, ".NS", "groww"),
        ("SENSEX", ROOT / "sensex_symbols.csv", DATA / "BSE", ".BO", "groww"),
        ("Nasdaq-100", ROOT / "nasdaq100_symbols.csv", DATA / "NASDAQ", "", "yahoo"),
    ]
    failures = []
    for label, symbols, output, suffix, provider in jobs:
        print(f"Refreshing {label}…", flush=True)
        result = subprocess.run([
            sys.executable, str(ROOT / "download_nifty50_data.py"),
            "--start", "2024-01-01", "--end", end,
            "--symbols-file", str(symbols), "--output-dir", str(output), "--suffix", suffix,
            "--provider", provider,
        ], cwd=ROOT, check=False)
        if result.returncode:
            failures.append(label)

    status_path.write_text(json.dumps({
        "date": now.date().isoformat(),
        "state": "failed" if failures else "complete",
        "finished_at": datetime.now(IST).isoformat(),
        "message": f"Failed: {', '.join(failures)}" if failures else "NIFTY 50, SENSEX, and Nasdaq-100 refreshed.",
    }))
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
