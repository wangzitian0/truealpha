"""CLI entrypoint for post-deploy canary oracle checks."""

from data_engine.datahub.canary_oracles import main

if __name__ == "__main__":
    raise SystemExit(main())
