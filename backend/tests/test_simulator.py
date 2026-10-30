"""ClinicSimulator with stream_chat mocked. Only the live test hits the network."""

from collections.abc import AsyncIterator

import pytest

from app.clinics import simulator
from app.clinics.simulator import (
    FALLBACK_LINE,
    HOLD_LINE,
    ClinicSimulator,
    dollar_amounts,
    is_hold_request,
)
from app.config import get_settings
from app.data.loader import load_demo_clinics


def _parkway() -> ClinicSimulator:
    clinic, sheet = next((c, s) for c, s in load_demo_clinics() if c.id == "parkway")
    return ClinicSimulator(clinic, sheet)


class FakeChat:
    """Stands in for stream_chat: returns scripted replies and records each request."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def __call__(
        self, messages: list[dict], model: str, reasoning: str | None = None
    ) -> AsyncIterator[str]:
        self.calls.append(messages)
        reply = self.replies.pop(0)
        for i in range(0, len(reply), 5):  # several chunks, like a real stream
            yield reply[i : i + 5]


@pytest.fixture
def chat(monkeypatch: pytest.MonkeyPatch) -> FakeChat:
    fake = FakeChat()
    monkeypatch.setattr(simulator, "stream_chat", fake)
    return fake


async def test_below_floor_regenerates_then_falls_back(chat: FakeChat) -> None:
    chat.replies = ["Sure, we can do $3,000 total.", "Okay, how about $3,500?"]
    sim = _parkway()

    reply = await sim.next_line("Could you do the treatment for $3,000?")

    assert reply == FALLBACK_LINE
    assert len(chat.calls) == 2
    first_system, retry_system = chat.calls[0][0]["content"], chat.calls[1][0]["content"]
    assert "Correction" not in first_system
    assert "$3,000" in retry_system.split("Correction", 1)[1]
    assert sim.history[-1] == {"role": "assistant", "content": FALLBACK_LINE}


async def test_below_floor_then_valid_uses_regenerated_reply(chat: FakeChat) -> None:
    chat.replies = ["We could do $3,200.", "I can't go that low, but I can check on $4,500."]
    sim = _parkway()

    reply = await sim.next_line("Could you do $3,000?")

    assert reply == "I can't go that low, but I can check on $4,500."
    assert len(chat.calls) == 2


async def test_spelled_out_thousands_regenerates_then_falls_back(chat: FakeChat) -> None:
    chat.replies = ["We could do three thousand even.", "Fine, four thousand eight hundred."]
    sim = _parkway()

    reply = await sim.next_line("Could you do $3,000?")

    assert reply == FALLBACK_LINE
    assert len(chat.calls) == 2
    assert '"three thousand"' in chat.calls[1][0]["content"].split("Correction", 1)[1]


async def test_fees_and_monthly_below_floor_are_allowed(chat: FakeChat) -> None:
    line = "It's $180 a month with $1,000 down, plus a $250 records fee."
    chat.replies = [line]

    assert await _parkway().next_line("What's the monthly plan?") == line
    assert len(chat.calls) == 1


async def test_history_kept_across_turns(chat: FakeChat) -> None:
    chat.replies = ["Treatment is $4,800.", "Yes, the consult is free."]
    sim = _parkway()

    await sim.next_line("How much are braces?")
    await sim.next_line("Is the consult free?")

    second = chat.calls[1]
    assert second[0]["role"] == "system"
    assert "Parkway Orthodontics" in second[0]["content"]
    assert second[1:] == [
        {"role": "user", "content": "How much are braces?"},
        {"role": "assistant", "content": "Treatment is $4,800."},
        {"role": "user", "content": "Is the consult free?"},
    ]
    assert len(sim.history) == 4


async def test_hold_then_resume_same_conversation(chat: FakeChat) -> None:
    chat.replies = ["Treatment is $4,800.", "Welcome back! What can I do for you?"]
    sim = _parkway()

    await sim.next_line("How much are braces?")
    reply = await sim.next_line("Could you hold for a moment while I check something?")

    assert reply == HOLD_LINE
    assert sim.on_hold
    assert len(chat.calls) == 1  # no model call for the hold

    await sim.next_line("Thanks for holding, I'm back.")

    assert not sim.on_hold
    resumed = chat.calls[1][1:]
    assert resumed[0]["content"] == "How much are braces?"
    assert resumed[3] == {"role": "assistant", "content": HOLD_LINE}
    assert resumed[4]["content"] == "Thanks for holding, I'm back."


def test_hold_detection() -> None:
    assert is_hold_request("Could you hold?")
    assert is_hold_request("Can I put you on hold for a minute?")
    assert is_hold_request("Would you mind holding the line... I mean, hold on one sec")
    assert not is_hold_request("Can you hold that Tuesday slot for us?")
    assert not is_hold_request("What's the household discount?")


def test_dollar_amounts() -> None:
    text = "$4,800 or $3.5k, maybe 3,990, or 4000 dollars"
    assert dollar_amounts(text) == [4800, 3500, 3990, 4000]
    assert dollar_amounts("Monday at 5:30, 412 reviews, suite 200") == []


@pytest.mark.live
async def test_live_parkway_never_quotes_below_floor() -> None:
    if not get_settings().model_fast:
        pytest.skip("MODEL_FAST not set")
    sim = _parkway()
    floor = sim.sheet.floor_price

    replies = [
        await sim.next_line(
            "Hi, this is Lana, an AI assistant calling for a patient, and this call is recorded. "
            "Could you do full braces for $3,000 all-in?"
        ),
        await sim.next_line("$3,000 is really the patient's budget. Is there any way to get there?"),
    ]

    for reply in replies:
        below = [a for a in dollar_amounts(reply) if floor * simulator.PRICE_SIZED_FRACTION <= a < floor]
        assert not below, f"quoted below the ${floor:,} floor: {reply!r}"
