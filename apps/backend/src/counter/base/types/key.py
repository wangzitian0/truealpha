import re
from dataclasses import dataclass
from src.counter.base.types.errors import InvalidCounterKeyError

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")

@dataclass(frozen=True)
class CounterKey:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise InvalidCounterKeyError(f"counter key must be a str, got {type(self.value).__name__}")
        if not _KEY_PATTERN.match(self.value):
            raise InvalidCounterKeyError(
                f"counter key must be lowercase dotted 'domain.action' (e.g. 'report.generated'), got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value
