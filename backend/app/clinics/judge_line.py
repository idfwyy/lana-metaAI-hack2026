"""Be the clinic: a judge plays one demo clinic's front desk through the browser mic.

JudgeLine has ClinicSimulator's interface, next_line(lana_line) -> str, so the caller and the
negotiator run unchanged. The judge's committed turns (Meta endpointing plus the server's grace
merge, main.py) arrive through hear(); next_line waits for one after Lana's line has finished
playing in the browser. No floor price applies: the judge can say anything, and the
extractor and the negotiator still keep only the judge's exact words.

Silence: after JUDGE_SILENCE_S Lana says "Sorry, I didn't catch that." once; after a second
silence she says goodbye and the call ends (CallEnded), with whatever the quote has so far.
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from app.agent.caller import CallEnded
from app.clinics.simulator import is_hold_request
from app.config import get_settings
from app.schemas.clinic import Clinic
from app.schemas.events import JudgePhase

log = logging.getLogger(__name__)

JUDGE_CLINIC_ID = "parkway"

DIDNT_CATCH = "Sorry, I didn't catch that."
CALL_BACK_LATER = "No worries, I'll call back another time. Thanks!"

# What Lana's line asks about, checked in this order; anything else is about the price.
_PHASES: list[tuple[JudgePhase, re.Pattern[str]]] = [
    ("opening", re.compile(r"\b(openings?|slots?|appointments?|available|availability|consult)\b", re.IGNORECASE)),
    ("monthly", re.compile(r"\b(monthly|month|down payment|payment plan|installments?)\b", re.IGNORECASE)),
    ("fees", re.compile(r"\b(records?|fees?|x-?rays?|scans?)\b", re.IGNORECASE)),
]

Say = Callable[[str], Awaitable[None]]
TurnStart = Callable[[JudgePhase], Awaitable[None]]
LanaDone = Callable[[], Awaitable[None]]


class JudgeLine:
    """The judge's side of one call."""

    def __init__(
        self,
        clinic: Clinic,
        *,
        say: Say,
        turn_start: TurnStart,
        lana_done: LanaDone,
        silence_s: float | None = None,
        hold_s: float | None = None,
    ) -> None:
        settings = get_settings()
        self.clinic = clinic
        self._say = say
        self._turn_start = turn_start
        self._lana_done = lana_done
        self.silence_s = silence_s if silence_s is not None else settings.judge_silence_s
        self.hold_s = hold_s if hold_s is not None else settings.judge_hold_s
        self.stage: Literal["checklist", "negotiation"] = "checklist"
        self.on_hold = False
        self._turns: list[str] = []  # committed turns nobody has read yet
        self._arrived = asyncio.Event()
        self._talked_at = 0.0  # monotonic time of the judge's last partial

    # ----- from the session -----

    def hear(self, text: str) -> None:
        """One committed turn from the judge (speech or typed)."""
        text = text.strip()
        if text:
            self._turns.append(text)
            self._arrived.set()

    def talking(self) -> None:
        """A partial: the judge is mid-sentence, so the silence timer starts over."""
        self._talked_at = time.monotonic()

    # ----- for the caller and the negotiator -----

    def phase_for(self, lana_line: str) -> JudgePhase:
        if is_hold_request(lana_line):
            return "hold"
        if self.stage == "negotiation":
            return "negotiation"
        for phase, pattern in _PHASES:
            if pattern.search(lana_line):
                return phase
        return "price"

    async def answer(self) -> str | None:
        """The judge picks up the phone. None if they stay silent; Lana starts anyway."""
        await self._turn_start("greeting")
        return await self._wait(self.silence_s)

    async def next_line(self, lana_line: str) -> str:
        phase = self.phase_for(lana_line)
        await self._lana_done()
        if self._turns:
            return self._take()  # they talked over Lana, or kept going while she thought
        await self._turn_start(phase)
        if phase == "hold":
            # Any words will do; a silent judge is simply on hold.
            self.on_hold = True
            return await self._wait(self.hold_s) or ""
        self.on_hold = False
        text = await self._wait(self.silence_s)
        if text is not None:
            return text
        log.info("judge clinic=%s silent for %.0fs after %r", self.clinic.id, self.silence_s, lana_line)
        await self._say(DIDNT_CATCH)
        await self._lana_done()
        if self._turns:
            return self._take()
        await self._turn_start(phase)
        text = await self._wait(self.silence_s)
        if text is not None:
            return text
        log.info("judge clinic=%s silent twice; Lana ends the call", self.clinic.id)
        await self._say(CALL_BACK_LATER)
        raise CallEnded(f"{self.clinic.id} stayed silent")

    # ----- waiting -----

    def _take(self) -> str:
        text = " ".join(self._turns)
        self._turns = []
        self._arrived.clear()
        return text

    async def _wait(self, seconds: float) -> str | None:
        """The judge's next turn, or None after `seconds` with no turn and no speech."""
        started = time.monotonic()
        while not self._turns:
            quiet_since = max(started, self._talked_at)
            left = quiet_since + seconds - time.monotonic()
            if left <= 0:
                return None
            try:
                await asyncio.wait_for(self._arrived.wait(), left)
            except TimeoutError:
                pass
        return self._take()
