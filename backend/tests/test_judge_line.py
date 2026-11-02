"""JudgeLine with a scripted judge: no socket, no STT, no network."""

import asyncio

import pytest

from app.agent.caller import HOLD_REQUEST, CallEnded
from app.clinics.judge_line import CALL_BACK_LATER, DIDNT_CATCH, JUDGE_CLINIC_ID, JudgeLine
from app.data.loader import get_public_clinics

PRICE = "How much is the full treatment at your office?"


class Judge:
    """Plays the browser: each judge_turn_start is answered with the next scripted reply (None = silence)."""

    def __init__(self, *replies: str | None, silence_s: float = 0.05, hold_s: float = 0.05) -> None:
        self.replies = list(replies)
        self.phases: list[str] = []
        self.said: list[str] = []  # Lana's own lines from the JudgeLine
        self.waits = 0  # times the JudgeLine waited for Lana's line to finish
        clinic = next(c for c in get_public_clinics() if c.id == JUDGE_CLINIC_ID)
        self.line = JudgeLine(
            clinic,
            say=self._say,
            turn_start=self._turn_start,
            lana_done=self._lana_done,
            silence_s=silence_s,
            hold_s=hold_s,
        )

    async def _say(self, text: str) -> None:
        self.said.append(text)

    async def _lana_done(self) -> None:
        self.waits += 1

    async def _turn_start(self, phase: str) -> None:
        self.phases.append(phase)
        reply = self.replies.pop(0) if self.replies else None
        if reply is not None:
            asyncio.get_running_loop().call_later(0.01, self.line.hear, reply)


async def test_a_committed_turn_becomes_the_clinic_line() -> None:
    judge = Judge("It's $4,800, plus a $250 records fee.")

    reply = await judge.line.next_line(PRICE)

    assert reply == "It's $4,800, plus a $250 records fee."
    assert judge.phases == ["price"]
    assert judge.waits == 1  # Lana's line finished playing before the judge's turn
    assert judge.said == []


async def test_turns_heard_before_the_question_ends_are_kept() -> None:
    judge = Judge()
    judge.line.hear("We're $4,800")  # talked over Lana (barge-in)
    judge.line.hear("all in.")

    reply = await judge.line.next_line(PRICE)

    assert reply == "We're $4,800 all in."
    assert judge.phases == []  # no need to ask for a turn


async def test_one_silence_gives_didnt_catch_then_the_reply_counts() -> None:
    judge = Judge(None, "Sorry, it's $4,800.")

    reply = await judge.line.next_line(PRICE)

    assert judge.said == [DIDNT_CATCH]
    assert reply == "Sorry, it's $4,800."
    assert judge.phases == ["price", "price"]


async def test_two_silences_end_the_call_politely() -> None:
    judge = Judge(None, None)

    with pytest.raises(CallEnded):
        await judge.line.next_line(PRICE)

    assert judge.said == [DIDNT_CATCH, CALL_BACK_LATER]
    assert CALL_BACK_LATER == "No worries, I'll call back another time. Thanks!"


async def test_speech_in_progress_is_not_a_silence() -> None:
    judge = Judge(silence_s=0.1)

    async def slow_talker() -> None:
        for _ in range(4):  # 0.2 s of partials, longer than the silence window
            await asyncio.sleep(0.05)
            judge.line.talking()
        judge.line.hear("Our total is $4,800.")

    talker = asyncio.create_task(slow_talker())
    reply = await judge.line.next_line(PRICE)
    await talker

    assert reply == "Our total is $4,800."
    assert judge.said == []


async def test_hold_waits_for_any_reply() -> None:
    judge = Judge("Sure.")

    assert await judge.line.next_line(HOLD_REQUEST) == "Sure."
    assert judge.phases == ["hold"]
    assert judge.line.on_hold


async def test_a_silent_hold_moves_on_without_a_line() -> None:
    judge = Judge(None)

    assert await judge.line.next_line(HOLD_REQUEST) == ""
    assert judge.said == []  # no "didn't catch that" and no hang-up on a hold


async def test_the_judge_picks_up_first_or_lana_starts_anyway() -> None:
    picked = Judge("Parkway Orthodontics, how can I help?")
    assert await picked.line.answer() == "Parkway Orthodontics, how can I help?"
    assert picked.phases == ["greeting"]

    silent = Judge(None)
    assert await silent.line.answer() is None
    assert silent.said == []


@pytest.mark.parametrize(
    "line, phase",
    [
        ("Hi there! This is Lana. What's the total price for braces at your office?", "price"),
        ("Is there a separate records fee for x-rays and scans?", "fees"),
        ("If the patient pays monthly, how much is each payment?", "monthly"),
        ("Do you have any evening openings next week?", "opening"),
        (HOLD_REQUEST, "hold"),
    ],
)
def test_phase_follows_lanas_question(line: str, phase: str) -> None:
    assert Judge().line.phase_for(line) == phase


def test_negotiation_stage_overrides_the_question_words() -> None:
    judge = Judge()
    judge.line.stage = "negotiation"
    assert judge.line.phase_for("Would you be able to waive the $250 records fee?") == "negotiation"
    assert judge.line.phase_for(HOLD_REQUEST) == "hold"


async def test_the_judge_may_go_below_the_floor() -> None:
    judge = Judge("We can do $3,000.")  # below Parkway's $3,990 floor: the judge's call, not ours

    assert await judge.line.next_line(PRICE) == "We can do $3,000."
