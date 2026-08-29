"""Regression coverage for Telegram's typed event delivery boundary."""

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.event_delivery_policy import (
    DeliveryEvent,
    TelegramDeliveryGate,
    event_from_metadata,
    event_metadata,
    should_deliver,
)
from plugins.platforms.telegram.adapter import TelegramAdapter


@dataclass
class FakeAdapter:
    """Deterministic adapter used to model the gateway delivery callback path."""

    sent: list[str]

    async def send(self, chat_id, text, metadata=None):
        self.sent.append(text)


@pytest.mark.parametrize(
    "kind",
    [
        "reasoning",
        "reasoning.summary",
        "tool.started",
        "tool.completed",
        "tool.output_risk",
        "tool.call_json",
        "progress",
        "interim_assistant",
        "background_review",
        "status",
        "lifecycle",
    ],
)
def test_telegram_suppresses_typed_internal_events(kind):
    assert not should_deliver(
        Platform.TELEGRAM,
        DeliveryEvent(kind=kind, channel="internal"),
    )


@pytest.mark.parametrize("kind", ["cron.blocker", "cron.failure", "cron.recovered"])
def test_telegram_allows_only_explicit_cron_alerts(kind):
    assert should_deliver(
        Platform.TELEGRAM,
        DeliveryEvent(kind=kind, channel="alert", alert_kind=kind),
    )
    assert not should_deliver(
        Platform.TELEGRAM,
        DeliveryEvent(kind=kind, channel="internal"),
    )


def test_telegram_preserves_final_and_interactive_events():
    for kind in ("final", "approval", "clarify", "interactive"):
        assert should_deliver(
            Platform.TELEGRAM,
            DeliveryEvent(kind=kind, channel="user"),
        )


@pytest.mark.asyncio
async def test_fake_adapter_path_sends_only_final_and_alert_once():
    adapter = FakeAdapter([])
    gate = TelegramDeliveryGate()

    async def deliver(event, text):
        if gate.accept(event, delivery_key=text):
            await adapter.send("chat-test", text)

    events = [
        (DeliveryEvent("reasoning", "internal"), "SECRET_REASONING"),
        (DeliveryEvent("tool.started", "internal"), '{"tool":"terminal"}'),
        (DeliveryEvent("progress", "internal"), "running"),
        (DeliveryEvent("final", "user"), "final response"),
        (DeliveryEvent("final", "user"), "final response"),
        (
            DeliveryEvent("cron.blocker", "alert", alert_kind="cron.blocker"),
            "cron blocked",
        ),
    ]
    for event, text in events:
        await deliver(event, text)

    assert adapter.sent == ["final response", "cron blocked"]
    assert "SECRET_REASONING" not in adapter.sent
    assert all("tool" not in item for item in adapter.sent)


def test_typed_event_metadata_round_trips_without_text_inspection():
    event = DeliveryEvent("cron.failure", "alert", alert_kind="cron.failure")
    assert event_from_metadata(event_metadata(event)) == event
    assert not should_deliver(
        Platform.TELEGRAM,
        DeliveryEvent("credit.warning", "internal"),
    )


@pytest.mark.asyncio
async def test_telegram_adapter_drops_typed_internal_event_before_transport():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock()

    result = await adapter.send(
        "chat-test",
        "internal progress",
        metadata=event_metadata(DeliveryEvent("progress", "internal")),
    )

    assert result.success is True
    assert result.message_id is None
    adapter._bot.send_message.assert_not_awaited()
