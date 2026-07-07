#!/usr/bin/env python3
import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_ROOT / "apps" / "backend"
FRONTEND_DIR = REPO_ROOT / "apps" / "frontend"

def run(cmd: list[str], cwd: Path = REPO_ROOT, check: bool = True):
    print(f"▶ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd)
    if check and result.returncode != 0:
        sys.exit(result.returncode)
    return result

def cmd_setup(args):
    """Install dependencies."""
    if args.backend or not args.frontend:
        run(["uv", "sync"], cwd=BACKEND_DIR)
    if args.frontend or not args.backend:
        run(["npm", "install"], cwd=FRONTEND_DIR)

def cmd_dev(args):
    """Start development environment."""
    if args.infra:
        run(["docker", "compose", "--profile", "infra", "up", "-d"])
    elif args.migrate:
        run(["uv", "run", "alembic", "upgrade", "head"], cwd=BACKEND_DIR)
    elif args.backend:
        run([sys.executable, str(REPO_ROOT / "tools" / "dev_backend.py")], cwd=BACKEND_DIR)
    elif args.frontend:
        run([sys.executable, str(REPO_ROOT / "tools" / "dev_frontend.py")], cwd=FRONTEND_DIR)
    else:
        # Start both using docker compose up for infra, then inform the user
        run(["docker", "compose", "--profile", "infra", "up", "-d"])
        print("\n🚀 Infrastructure started. Now run in separate terminals:")
        print("   moon run :dev -- --backend")
        print("   moon run :dev -- --frontend")

def cmd_test(args):
    """Run tests."""
    if args.frontend:
        run(["npm", "run", "test"], cwd=FRONTEND_DIR)
    else:
        run(["uv", "run", "pytest"], cwd=BACKEND_DIR)

def cmd_lint(args):
    """Run linting and formatting."""
    if args.backend or not args.frontend:
        if args.fix:
            run(["uv", "run", "ruff", "format", "src"], cwd=BACKEND_DIR)
            run(["uv", "run", "ruff", "check", "src", "--fix"], cwd=BACKEND_DIR)
        else:
            run(["uv", "run", "ruff", "check", "src"], cwd=BACKEND_DIR)
            run(["uv", "run", "ruff", "format", "src", "--check"], cwd=BACKEND_DIR)

    if args.frontend or not args.backend:
        if args.fix:
            run(["npm", "run", "lint", "--", "--fix"], cwd=FRONTEND_DIR, check=False)
        else:
            run(["npm", "run", "lint"], cwd=FRONTEND_DIR)

def cmd_build(args):
    """Build projects."""
    run(["npm", "run", "build"], cwd=FRONTEND_DIR)

def cmd_clean(args):
    """Clean up resources."""
    if args.containers:
        run(["docker", "compose", "--profile", "infra", "down"])
    else:
        subprocess.run(["make", "clean"], cwd=REPO_ROOT)

def main():
    parser = argparse.ArgumentParser(description="Unified CLI for TrueAlpha")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # setup
    p_setup = subparsers.add_parser("setup", help="Install dependencies")
    p_setup.add_argument("--backend", action="store_true")
    p_setup.add_argument("--frontend", action="store_true")

    # dev
    p_dev = subparsers.add_parser("dev", help="Start development environment")
    p_dev.add_argument("--infra", action="store_true", help="Start infrastructure")
    p_dev.add_argument("--migrate", action="store_true", help="Run database migrations")
    p_dev.add_argument("--backend", action="store_true", help="Start backend server")
    p_dev.add_argument("--frontend", action="store_true", help="Start frontend server")

    # test
    p_test = subparsers.add_parser("test", help="Run tests")
    p_test.add_argument("--frontend", action="store_true", help="Frontend only")

    # lint
    p_lint = subparsers.add_parser("lint", help="Code quality checks")
    p_lint.add_argument("--fix", action="store_true", help="Auto-fix issues")
    p_lint.add_argument("--backend", action="store_true")
    p_lint.add_argument("--frontend", action="store_true")

    # build
    p_build = subparsers.add_parser("build", help="Build projects")

    # clean
    p_clean = subparsers.add_parser("clean", help="Clean up resources")
    p_clean.add_argument("--containers", action="store_true", help="Stop containers")

    args = parser.parse_args()

    commands = {
        "setup": cmd_setup,
        "dev": cmd_dev,
        "test": cmd_test,
        "lint": cmd_lint,
        "build": cmd_build,
        "clean": cmd_clean,
    }
    commands[args.command](args)

if __name__ == "__main__":
    main()
