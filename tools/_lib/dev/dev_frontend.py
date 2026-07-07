#!/usr/bin/env python3
import signal
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

_started_resources = {
    "next_proc": None,
}

def cleanup(signum=None, frame=None):
    print("\n🧹 Stopping Next.js...")
    proc = _started_resources.get("next_proc")
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

    print("🚀 Starting Next.js dev server on http://localhost:3000")
    proc = subprocess.Popen(
        ["npm", "run", "dev"],
        cwd=REPO_ROOT / "apps" / "frontend",
    )
    _started_resources["next_proc"] = proc

    try:
        proc.wait()
    except KeyboardInterrupt:
        cleanup()

if __name__ == "__main__":
    main()
