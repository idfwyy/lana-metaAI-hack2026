"""Transcript -> ClinicChecklist and Quote. Every answer keeps the clinic's exact words.

The model proposes values with a quote; Python decides whether the quote is real. A quote
that is not in a clinic line is dropped, so a made-up fact never reaches the Quote.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from app.config import get_settings
from app.integrations.meta_llm import call_json
from app.schemas.checklist import ClinicChecklist
from app.schemas.enums import Speaker
from app.schemas.quote import Quote

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "extractor.md"

# The fields Lana asks about, in the order she asks them.
CHECKLIST_ORDER = [
    "total_price",
    "records_fee",
    "includes",
    "monthly_payment",
    "down_payment",
    "next_slots",
    "evening_hours",
]

T = TypeVar("T")


class TranscriptLine(BaseModel):
    speaker: Speaker
    text: str
    t_seconds: float = Field(ge=0, description="Second in the call when the line starts")


class Transcript(BaseModel):
    call_id: str
    clinic_id: str
    lines: list[TranscriptLine] = Field(default_factory=list)


class _Found(BaseModel, Generic[T]):
    value: T
    evidence: str = Field(description="Exact words copied from one CLINIC line")


class _Extraction(BaseModel):
    """What the model returns. call_id, speaker and t_seconds are filled in by Python."""

    total_price: _Found[int] | None = None
    records_fee: _Found[int] | None = None
    consult_fee: _Found[int] | None = None
    includes: _Found[list[str]] | None = None
    monthly_payment: _Found[int] | None = None
    down_payment: _Found[int] | None = None
    payment_plan: _Found[bool] | None = None
    next_slots: _Found[list[datetime]] | None = Field(
        default=None, description="Local times in the clinic's time zone, no offset"
    )
    evening_hours: _Found[bool] | None = None
    needs_exam_first: _Found[bool] | None = None


def _normalize(text: str) -> tuple[str, list[int]]:
    """Lowercase, punctuation dropped, spaces collapsed; plus each kept char's index in `text`."""
    chars: list[str] = []
    index: list[int] = []
    gap = False
    for i, ch in enumerate(text):
        if ch.isalnum():
            if gap and chars:
                chars.append(" ")
                index.append(i)
            gap = False
            chars.append(ch.lower())
            index.append(i)
        elif ch.isspace():
            gap = True
    return "".join(chars), index


def find_evidence(evidence: str, line: str) -> str | None:
    """The exact substring of `line` that `evidence` quotes, or None if it isn't there.

    Case, punctuation and extra spaces are ignored when comparing, but whole words must match.
    The result is widened to keep punctuation stuck to the first and last word ("$4,200.").
    """
    needle, _ = _normalize(evidence)
    hay, index = _normalize(line)
    if not needle:
        return None
    pos = hay.find(needle)
    while pos != -1:
        end = pos + len(needle)
        if (pos == 0 or hay[pos - 1] == " ") and (end == len(hay) or hay[end] == " "):
            start, stop = index[pos], index[end - 1] + 1
            while start > 0 and not line[start - 1].isspace():
                start -= 1
            while stop < len(line) and not line[stop].isspace():
                stop += 1
            return line[start:stop]
        pos = hay.find(needle, pos + 1)
    return None


def _format_transcript(transcript: Transcript) -> str:
    return "\n".join(
        f"[{i}] {line.speaker.value.upper()} ({line.t_seconds:.1f}s): {line.text}"
        for i, line in enumerate(transcript.lines)
    )


def _format_current(checklist: ClinicChecklist) -> str:
    filled = {
        name: answer.value for name, answer in checklist if answer is not None
    }
    if not filled:
        return "Nothing confirmed yet."
    return "\n".join(f"- {name}: {value}" for name, value in filled.items())


def _localize(value: object) -> object:
    """Attach TIMEZONE to slot times the model gave without an offset."""
    if isinstance(value, list):
        tz = get_settings().tz
        return [dt.replace(tzinfo=tz) if dt.tzinfo is None else dt for dt in value]
    return value


async def update_checklist(checklist: ClinicChecklist, transcript: Transcript) -> ClinicChecklist:
    """Fill what the clinic has said so far. A field the model leaves empty keeps its old answer."""
    settings = get_settings()
    today = datetime.now(settings.tz)
    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(
        today=f"{today:%A, %B} {today.day}, {today.year}",
        timezone=settings.timezone,
        transcript=_format_transcript(transcript),
        current=_format_current(checklist),
    )
    found = await call_json(
        prompt, _Extraction, settings.model_fast, reasoning=settings.call_reasoning
    )

    clinic_lines = [line for line in transcript.lines if line.speaker == Speaker.CLINIC]
    updates: dict[str, dict] = {}
    for name, item in found:
        if item is None:
            continue
        for line in clinic_lines:
            quote = find_evidence(item.evidence, line.text)
            if quote is not None:
                updates[name] = {
                    "value": _localize(item.value) if name == "next_slots" else item.value,
                    "speaker": Speaker.CLINIC,
                    "evidence": quote,
                    "call_id": transcript.call_id,
                    "t_seconds": line.t_seconds,
                }
                break
        else:
            log.info(
                "extractor call=%s dropped %s: evidence not in a clinic line: %r",
                transcript.call_id,
                name,
                item.evidence,
            )
    return ClinicChecklist.model_validate({**checklist.model_dump(), **updates})


def _records_included(checklist: ClinicChecklist) -> bool:
    includes = checklist.includes.value if checklist.includes else []
    return any("record" in item.lower() for item in includes)


def build_quote(clinic_id: str, checklist: ClinicChecklist) -> Quote:
    """All-in price = treatment plus the records fee when records are not included."""
    all_in = None
    if checklist.total_price is not None:
        all_in = checklist.total_price.value
        if checklist.records_fee is not None and not _records_included(checklist):
            all_in += checklist.records_fee.value
    return Quote(
        clinic_id=clinic_id,
        checklist=checklist,
        all_in_price=all_in,
        missing_fields=[name for name in CHECKLIST_ORDER if getattr(checklist, name) is None],
        is_solid=checklist.total_price is not None,
    )
