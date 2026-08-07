"""Typed event policy for gateway-to-platform delivery.

The policy is deliberately about event identity and channel, never message
wording.  Telegram is a final-answer-first surface: internal events are
fail-closed, while the small operational-alert allowlist is explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


TELEGRAM_ALERT_KINDS = frozenset({
    "cron.blocker",
    "cron.failure",
    "cron.recovered",
})

TELEGRAM_USER_KINDS = frozenset({"final", "approval", "clarify", "interactive"})


@dataclass(frozen=True)
class DeliveryEvent:
    """A presentation event before platform delivery."""

    kind: str
    channel: str
    alert_kind: str | None = None


def event_metadata(event: DeliveryEvent) -> dict[str, str]:
    """Encode a typed event for adapter/router boundaries.

    The metadata is machine-readable routing context; message text is never
    inspected to decide whether Telegram may receive it.
    """
    result = {"delivery_event_kind": event.kind, "delivery_event_channel": event.channel}
    if event.alert_kind:
        result["delivery_alert_kind"] = event.alert_kind
    return result


def event_from_metadata(metadata: Any) -> DeliveryEvent | None:
    """Decode an explicitly typed event, returning None for legacy sends."""
    if not isinstance(metadata, dict) or "delivery_event_kind" not in metadata:
        return None
    kind = str(metadata.get("delivery_event_kind") or "")
    channel = str(metadata.get("delivery_event_channel") or "")
    if not kind or not channel:
        return None
    alert_kind = metadata.get("delivery_alert_kind")
    return DeliveryEvent(kind, channel, str(alert_kind) if alert_kind else None)


def should_deliver(platform: Any, event: DeliveryEvent) -> bool:
    """Return whether a typed event is eligible for the platform surface."""

    platform_name = str(getattr(platform, "value", platform) or "").lower()
    if platform_name != "telegram":
        return True
    if event.channel == "user":
        return event.kind in TELEGRAM_USER_KINDS
    if event.channel == "alert":
        return event.alert_kind in TELEGRAM_ALERT_KINDS
    return False


class TelegramDeliveryGate:
    """Per-turn idempotency gate for final and approved alert events."""

    def __init__(self) -> None:
        self._delivered: set[tuple[str, str]] = set()

    def accept(self, event: DeliveryEvent, *, delivery_key: str = "") -> bool:
        if not should_deliver("telegram", event):
            return False
        if event.kind == "final" or event.channel == "alert":
            key = (event.kind, delivery_key)
            if key in self._delivered:
                return False
            self._delivered.add(key)
        return True


__all__ = [
    "DeliveryEvent", "TelegramDeliveryGate", "TELEGRAM_ALERT_KINDS",
    "TELEGRAM_USER_KINDS", "event_metadata", "event_from_metadata", "should_deliver",
]
