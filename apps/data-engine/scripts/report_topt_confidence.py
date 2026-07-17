from __future__ import annotations

from data_engine.quality import build_topt_confidence_sensitivity_report


def main() -> None:
    print(build_topt_confidence_sensitivity_report().model_dump_json(indent=2))


if __name__ == "__main__":
    main()
