"""Keep the swing-trading dashboard running on its fixed local port."""

from __future__ import annotations

import subprocess
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PID_FILE = ROOT / "work" / "scheduled_dashboard.pid"


def stop_dashboard() -> int:
    """Stop the dashboard process started by this scheduler."""
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        print("No scheduled Swing dashboard process was found.")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        print(f"Unable to stop scheduled Swing dashboard process {pid}.")
        return 1
    print("Scheduled Swing dashboard stopped.")
    return 0


def main() -> int:
    if "--stop" in sys.argv[1:]:
        return stop_dashboard()
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    proc = subprocess.Popen([
        sys.executable, "-m", "streamlit", "run", str(ROOT / "dashboard.py"),
        "--server.address", "127.0.0.1", "--server.port", "9000",
        "--server.headless", "true", "--browser.gatherUsageStats", "false",
    ], cwd=ROOT)
    try:
        while proc.poll() is None:
            time.sleep(15)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
