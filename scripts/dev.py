from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "pptextract.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--reload",
            ],
            cwd=project_root,
            start_new_session=True,
        ),
        subprocess.Popen(
            [sys.executable, "-m", "pptextract.worker"],
            cwd=project_root,
            start_new_session=True,
        ),
        subprocess.Popen(
            ["npm", "run", "dev"],
            cwd=project_root / "web",
            start_new_session=True,
        ),
    ]

    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        while not stopping:
            for process in processes:
                exit_code = process.poll()
                if exit_code is not None:
                    return exit_code
            time.sleep(0.25)
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
