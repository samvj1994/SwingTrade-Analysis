"""Serve the dashboard from 9:00 to 11:00 AM IST on weekdays only."""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")


def main() -> int:
    now = datetime.now(IST)
    if now.weekday() >= 5 or not (9 <= now.hour < 11):
        print("Outside weekday dashboard window; not starting.")
        return 0
    proc = subprocess.Popen([
        sys.executable, "-m", "streamlit", "run", str(ROOT / "dashboard.py"),
        "--server.address", "127.0.0.1", "--server.port", "8501",
        "--server.headless", "true", "--browser.gatherUsageStats", "false",
    ], cwd=ROOT)
    try:
        while datetime.now(IST).hour < 11:
            if proc.poll() is not None:
                return proc.returncode or 0
            time.sleep(15)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
