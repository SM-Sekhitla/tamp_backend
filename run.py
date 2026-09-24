#!/usr/bin/env python3
"""Set up and run the TAMP FastAPI backend for local development."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIR = PROJECT_ROOT / ".venv"


def environment_value(name: str, default: str) -> str:
    if value := os.environ.get(name):
        return value
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for raw_line in env_file.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                return value.strip().strip("\"'") or default
    return default


def run_command(command: list[str]) -> None:
    print(f"Running: {subprocess.list2cmdline(command)}", flush=True)
    result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main() -> None:
    os.chdir(PROJECT_ROOT)
    print(f"Working directory: {PROJECT_ROOT}", flush=True)

    if not VENV_DIR.exists():
        print("Creating virtual environment...", flush=True)
        run_command([sys.executable, "-m", "venv", str(VENV_DIR)])
    else:
        print("Virtual environment already exists.", flush=True)

    if platform.system() == "Windows":
        executable_dir = VENV_DIR / "Scripts"
        python_executable = executable_dir / "python.exe"
    else:
        executable_dir = VENV_DIR / "bin"
        python_executable = executable_dir / "python"

    requirements = PROJECT_ROOT / "requirements.txt"
    if requirements.exists():
        print("Installing dependencies...", flush=True)
        run_command(
            [
                str(python_executable),
                "-m",
                "pip",
                "install",
                "-r",
                str(requirements),
            ]
        )

    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        print(
            "Warning: .env does not exist. Copy .env.example to .env and set "
            "DATABASE_URL, POSTGRES_PASSWORD, and SECRET_KEY.",
            flush=True,
        )

    print("Applying database migrations...", flush=True)
    run_command([str(python_executable), "-m", "alembic", "upgrade", "head"])

    port = environment_value("BACKEND_PORT", "5000")
    print(f"Starting FastAPI server at http://127.0.0.1:{port}...", flush=True)
    run_command(
        [
            str(python_executable),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            port,
            "--reload",
            "--no-proxy-headers",
        ]
    )


if __name__ == "__main__":
    main()
