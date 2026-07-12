"""Run the exact #27/#51 TOPT capture graph outside Dagster for diagnostics."""

import argparse
import json

from data_engine import db
from data_engine.capture.topt_run import ToptRunOptions, execute


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-digest", required=True, help="sha256 digest of the tested data-engine artifact")
    parser.add_argument("--run-id")
    parser.add_argument("--reuse-openfigi-raw", action="store_true")
    parser.add_argument("--skip-identity", action="store_true")
    parser.add_argument("--skip-sec-financials", action="store_true")
    parser.add_argument("--skip-sec-filings", action="store_true")
    parser.add_argument("--skip-yahoo-prices", action="store_true")
    parser.add_argument("--skip-moomoo", action="store_true")
    args = parser.parse_args()
    connection = db.connect()
    try:
        manifest = execute(
            connection,
            ToptRunOptions(
                image_digest=args.image_digest,
                run_id=args.run_id,
                reuse_openfigi_raw=args.reuse_openfigi_raw,
                identity=not args.skip_identity,
                sec_financials=not args.skip_sec_financials,
                sec_filings=not args.skip_sec_filings,
                yahoo_prices=not args.skip_yahoo_prices,
                moomoo_domains=not args.skip_moomoo,
            ),
        )
    finally:
        connection.close()
    print(
        json.dumps(
            {
                "run_id": manifest.run_id,
                "capture_scope_id": manifest.scope.capture_scope_id,
                "capture_manifest_id": manifest.capture_manifest_id,
                "status": manifest.status.value,
                "cell_count": len(manifest.cells),
                "complete_cell_count": sum(cell.status.value == "complete" for cell in manifest.cells),
                "blockers": manifest.blockers,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
