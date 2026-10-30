"""Runs one call: AI disclosure, checklist questions one at a time, polite close.

Python decides what Lana asks next (the first missing field) and when the call ends.
The model only phrases the question and answers the receptionist from the patient's
Preferences.
"""

import inspect
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.agent.extractor import CHECKLIST_ORDER, Transcript, TranscriptLine, update_checklist
from app.config import get_settings
from app.data.loader import get_public_clinics
from app.integrations.meta_llm import stream_chat
from app.schemas.checklist import ClinicChecklist
from app.schemas.clinic import Clinic
from app.schemas.enums import Speaker
from app.schemas.preferences import Preferences

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "caller.md"

DISCLOSURE = "Hi, this is {agent_name}, an AI assistant calling for a patient, and this call is recorded."
CLOSING_LINE = "That's everything I needed, thank you so much for your help. Have a great day!"

# Said instead of the model's line when it is empty or names another clinic.
FALLBACK_QUESTIONS = {
    "total_price": "How much is the full treatment at your office?",
    "records_fee": "Is there a separate records fee for x-rays and scans?",
    "includes": "What does that price include?",
    "monthly_payment": "If the patient pays monthly, how much is each payment?",
    "down_payment": "How much is the down payment for the monthly plan?",
    "next_slots": "When is your next available consult appointment?",
    "evening_hours": "Do you have any evening appointments?",
}

MAX_LANA_TURNS = 10  # including the closing line
MAX_ASKS = 2  # after this many asks a field is left as not confirmed
WORDS_PER_SECOND = 2.6  # text mode speaking pace

OnLine = Callable[[Speaker, str, float], None | Awaitable[None]]


class Receptionist(Protocol):
    async def next_line(self, lana_line: str) -> str: ...


@dataclass
class CallResult:
    transcript: Transcript
    checklist: ClinicChecklist


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _patient_text(prefs: Preferences) -> str:
    """What Lana may tell the clinic. The budget is left out on purpose."""
    return "\n".join(
        [
            f"- Treatment wanted: {prefs.treatment.value}",
            f"- ZIP code: {prefs.zip_code}",
            f"- Will travel up to: {prefs.max_distance_mi:g} miles",
            f"- Would pay in full if it is cheaper: {_yes_no(prefs.pay_in_full_ok)}",
            f"- Uses an HSA or FSA: {_yes_no(prefs.uses_hsa_fsa)}",
            f"- Prefers a monthly plan: {_yes_no(prefs.prefers_monthly)}",
            f"- Already has x-rays: {_yes_no(prefs.has_xrays)}",
            f"- Already has a treatment plan: {_yes_no(prefs.has_plan)}",
            f"- Must-haves: {', '.join(prefs.must_haves) or 'none given'}",
            f"- Free times: {', '.join(prefs.free_times) or 'not given'}",
        ]
    )


def _clean(line: str) -> str:
    """Drop a speaker label or wrapping quotes the model sometimes adds, and em dashes."""
    line = re.sub(r"^\s*\**\s*lana\s*\**\s*:\s*", "", line.strip(), flags=re.IGNORECASE)
    line = re.sub(r"\s*—\s*", ", ", line)
    return line.strip().strip('"').strip()


def _names_pattern(names: list[str]) -> re.Pattern[str] | None:
    """Matches a full clinic name or its first word ("Parkway"), as whole words."""
    words = {name.split()[0] for name in names} | set(names)
    if not words:
        return None
    alternatives = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"\b(?:{alternatives})\b", re.IGNORECASE)


class Caller:
    """Lana's side of one call with one clinic."""

    def __init__(
        self,
        clinic: Clinic,
        preferences: Preferences,
        on_line: OnLine,
        call_id: str | None = None,
    ) -> None:
        self.clinic = clinic
        self.preferences = preferences
        self.on_line = on_line
        self.checklist = ClinicChecklist()
        self.transcript = Transcript(call_id=call_id or uuid.uuid4().hex[:12], clinic_id=clinic.id)
        self._asks = dict.fromkeys(CHECKLIST_ORDER, 0)
        self._lana_turns = 0
        self._clock = 0.0
        self._agent_name = get_settings().agent_name
        self._other_clinics = _names_pattern(
            [c.name for c in get_public_clinics() if c.id != clinic.id]
        )

    async def run(self, receptionist: Receptionist) -> CallResult:
        while True:
            fields = self._fields_to_ask()
            if not fields or self._lana_turns >= MAX_LANA_TURNS - 1:
                await self._say(Speaker.AGENT, CLOSING_LINE)
                break
            line = await self._question(fields)
            if self._lana_turns == 0:
                line = f"{DISCLOSURE.format(agent_name=self._agent_name)} {line}"
            self._asks[fields[0]] += 1
            await self._say(Speaker.AGENT, line)
            reply = await receptionist.next_line(line)
            await self._say(Speaker.CLINIC, reply)
            await self._extract()
        return CallResult(transcript=self.transcript, checklist=self.checklist)

    def _fields_to_ask(self) -> list[str]:
        return [
            name
            for name in CHECKLIST_ORDER
            if getattr(self.checklist, name) is None and self._asks[name] < MAX_ASKS
        ]

    async def _question(self, fields: list[str]) -> str:
        """Lana's next line from the model, or a fixed question if it fails the checks."""
        settings = get_settings()
        system = PROMPT_PATH.read_text(encoding="utf-8").format(
            agent_name=self._agent_name,
            clinic_name=self.clinic.name,
            patient=_patient_text(self.preferences),
            missing_fields=", ".join(fields),
            next_field=fields[0],
        )
        history = [
            {"role": "assistant" if line.speaker == Speaker.AGENT else "user", "content": line.text}
            for line in self.transcript.lines
        ]
        # The Meta API needs at least one user message, so the opening turn sends the prompt as one.
        first = {"role": "system" if history else "user", "content": system}
        try:
            stream = stream_chat(
                [first, *history],
                settings.model_fast,
                reasoning=settings.call_reasoning,
            )
            line = _clean("".join([c async for c in stream]))
        except Exception as err:
            log.warning("caller call=%s turn failed, using fallback: %s", self.transcript.call_id, err)
            line = ""
        if not line or (self._other_clinics and self._other_clinics.search(line)):
            if line:
                log.warning("caller call=%s line named another clinic: %r", self.transcript.call_id, line)
            return FALLBACK_QUESTIONS[fields[0]]
        return line

    async def _say(self, speaker: Speaker, text: str) -> None:
        t = round(self._clock, 1)
        self._clock += len(text.split()) / WORDS_PER_SECOND
        if speaker == Speaker.AGENT:
            self._lana_turns += 1
        self.transcript.lines.append(TranscriptLine(speaker=speaker, text=text, t_seconds=t))
        result = self.on_line(speaker, text, t)
        if inspect.isawaitable(result):
            await result

    async def _extract(self) -> None:
        """Update the checklist; a failed extraction keeps what we had so the call goes on."""
        try:
            self.checklist = await update_checklist(self.checklist, self.transcript)
        except Exception as err:
            log.warning("caller call=%s extraction failed: %s", self.transcript.call_id, err)
