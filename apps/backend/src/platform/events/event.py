from dataclasses import dataclass
from datetime import datetime

@dataclass(frozen=True)
class DomainEvent:
    event_type: str
    occurred_at: datetime

    def payload(self) -> dict:
        return {}
