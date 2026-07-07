from collections import defaultdict
from collections.abc import Callable
from typing import Protocol, runtime_checkable
from src.platform.events.event import DomainEvent
from src.platform.store.outbox import OutboxRepository

EventHandler = Callable[[DomainEvent], None]

class SubscriberRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)

    def handlers_for(self, event_type: str) -> list[EventHandler]:
        return list(self._handlers.get(event_type, ()))

@runtime_checkable
class EventBus(Protocol):
    def publish(self, event: DomainEvent) -> None:
        ...
    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        ...

class OutboxEventBus:
    def __init__(self, session, *, source_pkg: str, registry: SubscriberRegistry | None = None) -> None:
        self._repo = OutboxRepository(session)
        self._source_pkg = source_pkg
        self._registry = registry or SubscriberRegistry()

    def publish(self, event: DomainEvent) -> None:
        payload = event.payload()
        self._repo.enqueue(
            occurred_at=event.occurred_at,
            event_type=event.event_type,
            source_pkg=self._source_pkg,
            payload=payload,
            aggregate_id=payload.get("aggregate_id"),
        )

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._registry.subscribe(event_type, handler)

class RecordingEventBus:
    def __init__(self) -> None:
        self.published: list[DomainEvent] = []

    def publish(self, event: DomainEvent) -> None:
        self.published.append(event)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        pass
