"""A variant of the environment contract for tests of the universe record (#1522).

`variant_text()` is the packaged contract with the S&P 500 scheduled in production and one more universe
(`dow` by default). `serve_variant()` makes every `load_environment_contract()` call in the process read it, because
all readers share that one cached loader.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from typing import Any

from truealpha_contracts import environment_contract

#: The key, id prefix and lane name of the added universe.
NEW_KEY = "dow"
NEW_ID_PREFIX = "universe:dow-us-"
NEW_LANE = "universe-list:dow"


def variant_text(*, key: str = NEW_KEY, lane: str = NEW_LANE, id_prefix: str = NEW_ID_PREFIX) -> str:
    """The packaged contract with the S&P 500 scheduled in production and the universe ``key`` added."""
    raw: dict[str, Any] = json.loads(
        resources.files("truealpha_contracts.data")
        .joinpath(environment_contract.ENVIRONMENT_CONTRACT_RESOURCE)
        .read_text()
    )
    raw["universes"][key] = {
        "region": "us",
        "id_prefix": id_prefix,
        "lane_name": lane,
        "label": "Dow Jones Industrial Average constituents",
        "membership": {"kind": "index_api", "url": "https://example.invalid/dow", "mic": "xnys"},
        "roles": [],
        "price_series": False,
        "report_sample": ["listing:xnys:jpm"],
        "cron": {"production": "30 2 * * *"},
    }
    production = raw["environments"]["production"]
    production["universes"] = [
        {"key": "topt", "status": "scheduled"},
        {"key": "qqq", "status": "scheduled"},
        {"key": "sp500", "status": "scheduled"},
        {"key": key, "status": "scheduled"},
    ]
    production["ticks"][key] = {"estimated_listings": 30}
    return json.dumps(raw)


class _Overlay:
    """The contract package, with the contract file replaced."""

    def __init__(self, root: Any, text: str) -> None:
        self._root, self._text = root, text

    def joinpath(self, name: str) -> Any:
        if name != environment_contract.ENVIRONMENT_CONTRACT_RESOURCE:
            return self._root.joinpath(name)
        text = self._text

        class _File:
            def read_text(self, encoding: str = "utf-8") -> str:
                return text

        return _File()


@contextmanager
def serve_variant(text: str | None = None) -> Iterator[None]:
    """Every reader of the environment contract sees ``text`` (default: `variant_text()`) inside the block."""
    served = variant_text() if text is None else text
    real_files = environment_contract.resources.files

    def files(package: str) -> Any:
        root = real_files(package)
        return _Overlay(root, served) if package == "truealpha_contracts.data" else root

    environment_contract.load_environment_contract.cache_clear()
    environment_contract.resources.files = files
    try:
        yield
    finally:
        environment_contract.resources.files = real_files
        environment_contract.load_environment_contract.cache_clear()
