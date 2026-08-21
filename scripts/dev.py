from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="启动 PPTExtract 本地开发环境")
    parser.add_argument(
        "--host",
        default=os.environ.get("PPTEXTRACT_HOST", "0.0.0.0"),
        help="API 监听的主机地址 (默认: 0.0.0.0，亦可指定 10.8.0.5、127.0.0.1 等)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PPTEXTRACT_PORT", "8000")),
        help="API 服务的监听端口 (默认: 8000)",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]

    env = os.environ.copy()
    proxy_target_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    env["PPTEXTRACT_API_HOST"] = proxy_target_host
    env["PPTEXTRACT_API_PORT"] = str(args.port)

    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "pptextract.api:app",
                "--host",
                args.host,
                "--port",
                str(args.port),
                "--reload",
            ],
            cwd=project_root,
            env=env,
            start_new_session=True,
        ),
        subprocess.Popen(
            [sys.executable, "-m", "pptextract.worker"],
            cwd=project_root,
            env=env,
            start_new_session=True,
        ),
        subprocess.Popen(
            ["npm", "run", "dev"],
            cwd=project_root / "web",
            env=env,
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
