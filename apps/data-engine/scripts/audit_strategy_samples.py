#!/usr/bin/env python3
"""Report whether checked-in samples are sufficient for strategy work."""

from __future__ import annotations

import argparse
from pathlib import Path

from data_engine.quality.strategy_samples import audit_strategy_samples, render_markdown


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-root", type=Path, default=Path(__file__).parents[1] / "samples")
    parser.add_argument("--json", action="store_true", help="Emit the stable JSON report instead of Markdown")
    args = parser.parse_args()

    report = audit_strategy_samples(args.sample_root)
    print(report.model_dump_json(indent=2) if args.json else render_markdown(report))


if __name__ == "__main__":
    main()
