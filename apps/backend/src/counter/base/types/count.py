from dataclasses import dataclass
from src.counter.base.types.errors import NegativeCountError

@dataclass(frozen=True, order=True)
class Count:
    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise NegativeCountError(f"count must be an int, got {type(self.value).__name__}")
        if self.value < 0:
            raise NegativeCountError(f"count must be non-negative, got {self.value}")

    def __int__(self) -> int:
        return self.value

    def __str__(self) -> str:
        return str(self.value)
