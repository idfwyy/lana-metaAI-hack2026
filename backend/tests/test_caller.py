"""Caller with stream_chat and the extractor mocked. No network."""

import re
from collections.abc import AsyncIterator

import pytest

from app.agent import caller
from app.agent.caller import (
    CLOSING_LINE,
    DISCLOSURE,
    FALLBACK_QUESTIONS,
    HOLD_REQUEST,
    MAX_LANA_TURNS,
    WORDS_PER_SECOND,
    Caller,
)
from app.agent.extractor import CHECKLIST_ORDER
from app.clinics.simulator import is_hold_request
from app.config import get_settings
from app.data.loader import get_public_clinics
from app.schemas.checklist import ClinicChecklist, SourcedAnswer
from app.schemas.enums import Speaker, Treatment
from app.schemas.preferences import Preferences

PREFS = Preferences(
    treatment=Treatment.BRACES,
    zip_code="76010",
    max_distance_mi=15,
    budget_max=5000,
    pay_in_full_ok=True,
    uses_hsa_fsa=True,
    prefers_monthly=True,
    must_haves=["evening hours"],
)


def _clinic(clinic_id: str = "sunrise"):
    return next(c for c in get_public_clinics() if c.id == clinic_id)


class FakeChat:
    """Stands in for stream_chat: scripted replies, then a default question."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def __call__(
        self, messages: list[dict], model: str, reasoning: str | None = None
    ) -> AsyncIterator[str]:
        self.calls.append(messages)
        reply = self.replies.pop(0) if self.replies else "Could you tell me about that?"
        for i in range(0, len(reply), 5):
            yield reply[i : i + 5]

    def asked_fields(self) -> list[str]:
        return [
            re.search(r"Ask about this field now: (\w+)", call[0]["content"])[1]
            for call in self.calls
        ]


class FakeReceptionist:
    def __init__(self, reply: str = "Let me check on that.") -> None:
        self.reply = reply
        self.heard: list[str] = []

    async def next_line(self, lana_line: str) -> str:
        self.heard.append(lana_line)
        return self.reply


def _answer(value) -> SourcedAnswer:
    return SourcedAnswer(
        value=value, speaker=Speaker.CLINIC, evidence="x", call_id="c1", t_seconds=0
    )


FULL = ClinicChecklist(
    total_price=_answer(4200),
    records_fee=_answer(0),
    includes=_answer(["records"]),
    monthly_payment=_answer(175),
    down_payment=_answer(500),
    next_slots=_answer([]),
    evening_hours=_answer(False),
)


@pytest.fixture
def chat(monkeypatch: pytest.MonkeyPatch) -> FakeChat:
    fake = FakeChat()
    monkeypatch.setattr(caller, "stream_chat", fake)
    return fake


@pytest.fixture
def no_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unchanged(checklist, transcript):
        return checklist

    monkeypatch.setattr(caller, "update_checklist", unchanged)


def _recorder() -> tuple[list[tuple[Speaker, str, float]], object]:
    lines: list[tuple[Speaker, str, float]] = []
    return lines, lambda speaker, text, t: lines.append((speaker, text, t))


def _lana(lines) -> list[str]:
    return [text for speaker, text, _ in lines if speaker == Speaker.AGENT]


async def test_first_line_is_exact_disclosure_then_question(chat, no_extraction) -> None:
    chat.replies = ["How much is comprehensive braces treatment at your office?"]
    lines, on_line = _recorder()

    await Caller(_clinic(), PREFS, on_line).run(FakeReceptionist())

    disclosure = DISCLOSURE.format(agent_name=get_settings().agent_name)
    assert disclosure == (
        f"Hi, this is {get_settings().agent_name}, an AI assistant calling for a patient, "
        "and this call is recorded."
    )
    assert lines[0] == (
        Speaker.AGENT,
        f"{disclosure} How much is comprehensive braces treatment at your office?",
        0.0,
    )


async def test_stops_after_ten_lana_turns(chat, no_extraction) -> None:
    lines, on_line = _recorder()
    receptionist = FakeReceptionist()

    result = await Caller(_clinic(), PREFS, on_line).run(receptionist)

    lana = _lana(lines)
    assert len(lana) == MAX_LANA_TURNS
    assert lana[-1] == CLOSING_LINE
    assert len(receptionist.heard) == MAX_LANA_TURNS - 1
    assert len(result.transcript.lines) == len(lines) == 2 * MAX_LANA_TURNS - 1


async def test_each_field_asked_at_most_twice_in_order(chat, no_extraction) -> None:
    await Caller(_clinic(), PREFS, lambda *a: None).run(FakeReceptionist())

    expected = [f for f in CHECKLIST_ORDER for _ in range(2)][: MAX_LANA_TURNS - 1]
    assert chat.asked_fields() == expected


async def test_ends_when_all_fields_filled(chat, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fill_all(checklist, transcript):
        return FULL

    monkeypatch.setattr(caller, "update_checklist", fill_all)
    lines, on_line = _recorder()

    result = await Caller(_clinic(), PREFS, on_line).run(FakeReceptionist("It's $4,200."))

    assert _lana(lines)[-1] == CLOSING_LINE
    assert len(_lana(lines)) == 2
    assert result.checklist == FULL


async def test_lines_never_name_another_clinic(chat, no_extraction) -> None:
    chat.replies = [
        "How much is the full treatment?",
        "Parkway Orthodontics quoted less, could you match it?",
        "Is that cheaper than lakeside?",
    ]
    lines, on_line = _recorder()

    await Caller(_clinic("sunrise"), PREFS, on_line).run(FakeReceptionist())

    lana = _lana(lines)
    others = [c.name for c in get_public_clinics() if c.id != "sunrise"]
    for line in lana:
        for name in others:
            assert name.split()[0].lower() not in line.lower(), line
    assert lana[1] == FALLBACK_QUESTIONS["total_price"]
    assert lana[2] == FALLBACK_QUESTIONS["records_fee"]


async def test_failed_model_turn_uses_fallback(monkeypatch: pytest.MonkeyPatch, no_extraction) -> None:
    async def broken(messages, model, reasoning=None):
        raise RuntimeError("network down")
        yield  # pragma: no cover

    monkeypatch.setattr(caller, "stream_chat", broken)
    lines, on_line = _recorder()

    await Caller(_clinic(), PREFS, on_line).run(FakeReceptionist())

    assert lines[0][1].endswith(FALLBACK_QUESTIONS["total_price"])


async def test_times_are_running_start_seconds(chat, no_extraction) -> None:
    lines, on_line = _recorder()

    await Caller(_clinic(), PREFS, on_line).run(FakeReceptionist())

    clock = 0.0
    for _, text, t in lines:
        assert t == round(clock, 1)
        clock += len(text.split()) / WORDS_PER_SECOND


async def test_async_on_line_is_awaited(chat, no_extraction) -> None:
    seen: list[str] = []

    async def on_line(speaker, text, t):
        seen.append(text)

    result = await Caller(_clinic(), PREFS, on_line).run(FakeReceptionist())

    assert seen == [line.text for line in result.transcript.lines]


async def test_budget_and_patient_info_in_prompt(chat, no_extraction) -> None:
    await Caller(_clinic(), PREFS, lambda *a: None).run(FakeReceptionist())

    system = chat.calls[0][0]["content"]
    assert "Sunrise Orthodontic Studio" in system
    assert "76010" in system
    assert "evening hours" in system
    assert "5000" not in system and "5,000" not in system
    assert "{" not in system


async def test_history_roles(chat, no_extraction) -> None:
    await Caller(_clinic(), PREFS, lambda *a: None).run(FakeReceptionist("We're open."))

    assert [m["role"] for m in chat.calls[0]] == ["user"]  # the API needs a user message
    second = chat.calls[1]
    assert [m["role"] for m in second] == ["system", "assistant", "user"]
    assert second[2]["content"] == "We're open."


async def test_em_dashes_and_labels_are_cleaned(chat, no_extraction) -> None:
    chat.replies = ["Lana: Great — is there a records fee?"]
    lines, on_line = _recorder()

    await Caller(_clinic(), PREFS, on_line).run(FakeReceptionist())

    assert lines[0][1].endswith("Great, is there a records fee?")


async def test_end_with_hold_asks_to_hold_and_keeps_reply(chat, no_extraction) -> None:
    lines, on_line = _recorder()
    receptionist = FakeReceptionist("Sure, I'll hold.")

    result = await Caller(_clinic(), PREFS, on_line, end_with="hold").run(receptionist)

    assert _lana(lines)[-1] == HOLD_REQUEST
    assert CLOSING_LINE not in _lana(lines)
    assert receptionist.heard[-1] == HOLD_REQUEST
    assert result.transcript.lines[-1].speaker == Speaker.CLINIC
    assert result.transcript.lines[-1].text == "Sure, I'll hold."


def test_hold_request_is_heard_as_a_hold() -> None:
    assert is_hold_request(HOLD_REQUEST)


async def test_default_ends_with_closing_line(chat, no_extraction) -> None:
    lines, on_line = _recorder()

    await Caller(_clinic(), PREFS, on_line).run(FakeReceptionist())

    assert _lana(lines)[-1] == CLOSING_LINE
    assert HOLD_REQUEST not in _lana(lines)


async def test_on_checklist_called_only_on_change(chat, monkeypatch: pytest.MonkeyPatch) -> None:
    filled = ClinicChecklist(total_price=_answer(4200))
    results = [filled, filled, FULL]

    async def scripted(checklist, transcript):
        return results.pop(0) if results else checklist

    monkeypatch.setattr(caller, "update_checklist", scripted)
    seen: list[ClinicChecklist] = []

    async def on_checklist(checklist):
        seen.append(checklist)

    await Caller(_clinic(), PREFS, lambda *a: None, on_checklist=on_checklist).run(FakeReceptionist())

    assert seen == [filled, FULL]
