#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

_started_resources = {
    "uvicorn_proc": None,
}

def cleanup(signum=None, frame=None):
    print("\n🧹 Stopping uvicorn...")
    proc = _started_resources.get("uvicorn_proc")
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    sys.exit(0)

def main():
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    os.environ.setdefault(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/truealpha",
    )

    print("🐘 Running database migrations...")
    try:
        subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT / "apps" / "backend",
            check=True,
        )
    except subprocess.CalledProcessError:
        print("\n❌ Migration failed. Is the database running? Run 'make dev' or 'moon run :dev -- --infra'.")
        sys.exit(1)

    print("\n🚀 Starting FastAPI dev server on http://localhost:8000")
    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "python",
            "-m",
            "uvicorn",
            "src.main:app",
            "--reload",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
        ],
        cwd=REPO_ROOT / "apps" / "backend",
    )
    _started_resources["uvicorn_proc"] = proc

    try:
        proc.wait()
    except KeyboardInterrupt:
        cleanup()

if __name__ == "__main__":
    main()
