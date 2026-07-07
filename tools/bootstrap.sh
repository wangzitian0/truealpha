#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== Bootstrapping TrueAlpha ==="

# Check Python/uv
if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: 'uv' package manager is not installed."
  echo "Please install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

# Check Node.js
if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: Node.js is not installed."
  echo "Please install Node.js (version 20+ recommended)."
  exit 1
fi

# Sync Backend dependencies
echo "-> Syncing backend dependencies..."
(cd apps/backend && uv sync)

# Install Frontend dependencies
echo "-> Installing frontend dependencies..."
(cd apps/frontend && npm install)

# Initialize pre-commit if available
if command -v uvx >/dev/null 2>&1; then
  echo "-> Setting up pre-commit hooks..."
  uvx pre-commit install || true
fi

echo "=== Bootstrap Complete ==="
echo "You can now run:"
echo "  moon run :dev -- --infra    # to start postgres"
echo "  moon run :dev -- --migrate  # to run migrations"
echo "  moon run :dev               # to start dev servers"
