"""Test support: two real moomoo `get_valuation_detail` answers, byte-pinned (#1791).

Each file is a read-only copy of a staging `raw.fetches` object (source `moomoo`), read with
`raw_store.get_payload` and checked against its stored sha256. The loader fails on a changed byte.

| file                                   | `source_record_id`                          | fetched (UTC)        |
|----------------------------------------|---------------------------------------------|----------------------|
| `XOM_valuation_detail_2026-11-20.json` | `get_valuation_detail:symbol:XOM:2026-11-20` | 2026-11-20 03:52:54 |
| `XOM_valuation_detail_2026-11-21.json` | `get_valuation_detail:symbol:XOM:2026-11-21` | 2026-11-21 03:03:34 |

`last_update_time` is epoch seconds in UTC. `last_update_time_str` is 5 hours behind it, so it is US Eastern time.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

_MOOMOO_SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "moomoo"


@dataclass(frozen=True)
class ValuationSample:
    key_day: date
    sha256: str
    knowable_at: datetime
    trailing_pe: str
    forward_pe: str

    @property
    def path(self) -> Path:
        return _MOOMOO_SAMPLES / f"XOM_valuation_detail_{self.key_day.isoformat()}.json"

    def raw_bytes(self) -> bytes:
        body = self.path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        assert digest == self.sha256, f"samples/moomoo/{self.path.name} changed: {digest}"
        return body

    def answer(self) -> dict[str, Any]:
        return json.loads(self.raw_bytes())


XOM_2026_11_20 = ValuationSample(
    key_day=date(2026, 11, 20),
    sha256="a74738718d88f4db621384c393a5ec02e7066465a5685abcb741c98f32eb7208",
    knowable_at=datetime(2026, 11, 20, 3, 52, 54, tzinfo=UTC),
    trailing_pe="16.35",
    forward_pe="10.789",
)
XOM_2026_11_21 = ValuationSample(
    key_day=date(2026, 11, 21),
    sha256="23abd764446cbd8ddc4dae04df5640f6c7b89875209f925539d6a7c9edc0f196",
    knowable_at=datetime(2026, 11, 21, 3, 3, 35, tzinfo=UTC),
    trailing_pe="16.459",
    forward_pe="10.861",
)
