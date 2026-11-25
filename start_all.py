"""Launches FastAPI (127.0.0.1:8000) and Streamlit (0.0.0.0:$PORT) in one process group."""
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"


def wait_for_api(url: str = "http://127.0.0.1:8000/api/health", timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    print("[start_all] FastAPI backend is ready.", flush=True)
                    return
        except Exception:
            time.sleep(0.5)
    print("[start_all] Warning: FastAPI did not respond within timeout; starting Streamlit anyway.", flush=True)


def main() -> int:
    port = os.getenv("PORT", "8501")
    env = os.environ.copy()
    env["CHRONOS_API"] = "http://127.0.0.1:8000"

    api_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=str(BACKEND_DIR),
        env=env,
    )

    wait_for_api()

    ui_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "app.py",
            "--server.port",
            str(port),
            "--server.address",
            "0.0.0.0",
            "--server.headless",
            "true",
        ],
        cwd=str(FRONTEND_DIR),
        env=env,
    )

    def shutdown(signum=None, frame=None):
        for proc in (ui_proc, api_proc):
            if proc.poll() is None:
                proc.terminate()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while True:
        api_rc = api_proc.poll()
        ui_rc = ui_proc.poll()
        if api_rc is not None:
            print(f"[start_all] uvicorn exited with code {api_rc}", flush=True)
            shutdown()
            return api_rc
        if ui_rc is not None:
            print(f"[start_all] streamlit exited with code {ui_rc}", flush=True)
            shutdown()
            return ui_rc
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
