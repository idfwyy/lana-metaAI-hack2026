"""Negotiation: Python picks the target and the moves, the AI only phrases them.

Honesty is enforced in Python: a phrased line may only use the dollar amounts and clinic names
stored on its Move, and a concession only counts when the clinic's own words back it up.
"""

import inspect
import logging
import math
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from app.agent.caller import DISCLOSURE, WORDS_PER_SECOND, OnLine, Receptionist
from app.agent.extractor import find_evidence
from app.agent.ranker import passes_filters, sub_scores, weighted
from app.clinics.simulator import dollar_amounts
from app.config import get_settings
from app.data.loader import get_public_clinics
from app.integrations.meta_llm import call_json, stream_chat
from app.schemas.checklist import SourcedAnswer
from app.schemas.clinic import Clinic
from app.schemas.enums import Speaker
from app.schemas.negotiation import Concession, NegotiationResult
from app.schemas.preferences import Preferences
from app.schemas.quote import Quote

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "negotiator.md"
# negotiator.md holds two prompts: phrasing a move, then reading the clinic's reply.
PROMPT_SPLIT = "<!-- read-reply -->"

MAX_ASKS = 4


class MoveKind(StrEnum):
    MATCH = "match_lowest"
    WAIVE_RECORDS = "waive_records"
    PAY_IN_FULL = "pay_in_full"
    RETAINERS = "extra_retainers"


LABELS = {
    MoveKind.MATCH: "Price match",
    MoveKind.WAIVE_RECORDS: "Records fee waived",
    MoveKind.PAY_IN_FULL: "Pay-in-full discount",
    MoveKind.RETAINERS: "Second set of retainers",
}

# Said when the model's line fails the honesty check twice.
TEMPLATES = {
    MoveKind.MATCH: "{cited_clinic} quoted ${cited_amount:,} all in for the same treatment, "
    "could you match that price?",
    MoveKind.WAIVE_RECORDS: "Would you be able to waive the ${records_fee:,} records fee?",
    MoveKind.PAY_IN_FULL: "If the patient pays in full up front, is there a discount on the price?",
    MoveKind.RETAINERS: "Could you include a second set of retainers at no extra charge?",
}
SOURCE_LINE = "That was {cited_clinic}, at ${cited_amount:,} all in."
WAIT_LINE = "Of course, take your time."
CLOSING_LINE = (
    "Thank you so much for working with me on this, I'll share it with the patient "
    "and we'll call back to book."
)

# A percent discount the clinic said ("5% off", "3 percent").
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s?(?:%|percent\b)", re.IGNORECASE)

# A clinic-like name ("Bright Smiles Orthodontics"), to catch names the model made up.
_CLINIC_NAME_RE = re.compile(
    r"(?:\b[A-Z][\w'&]*\s+){1,3}(?:Orthodontics|Orthodontic|Dental|Dentistry|Clinic|Smiles?|Studio)\b"
)


class Move(BaseModel):
    """One ask. Every fact Lana may say for it is stored here."""

    kind: MoveKind
    clinic_name: str
    current_all_in: int
    cited_clinic: str | None = None
    cited_amount: int | None = None
    records_fee: int | None = None

    @property
    def label(self) -> str:
        return LABELS[self.kind]

    @property
    def allowed_amounts(self) -> set[int]:
        return {a for a in (self.current_all_in, self.cited_amount, self.records_fee) if a is not None}

    @property
    def allowed_names(self) -> list[str]:
        return [n for n in (self.clinic_name, self.cited_clinic) if n]

    def template(self) -> str:
        return TEMPLATES[self.kind].format(**self.model_dump())


class Verdict(StrEnum):
    YES = "yes"
    NO = "no"
    UNCLEAR = "unclear"
    PENDING = "pending"
    ASKS_WHICH_CLINIC = "asks_which_clinic"


class ReplyReading(BaseModel):
    verdict: Verdict
    new_all_in: int | None = None
    amount_off: int | None = None
    evidence: str = ""


# ---------- target and moves (plain Python) ----------


def pick_target(
    quotes: list[Quote],
    clinics: list[Clinic],
    preferences: Preferences,
    now: datetime | None = None,
) -> str | None:
    """Best clinic on everything except price, unless it already has the cheapest solid quote.

    The budget filter is skipped here: negotiating is how an over-budget clinic gets under it.
    """
    now = now or datetime.now(get_settings().tz)
    by_id = {c.id: c for c in clinics}
    solid = [
        q
        for q in quotes
        if q.is_solid
        and q.all_in_price is not None
        and q.clinic_id in by_id
        and passes_filters(q, by_id[q.clinic_id], preferences, q.all_in_price, check_budget=False)
    ]
    if len(solid) < 2:
        return None
    def non_price_score(q: Quote) -> float:
        scores = sub_scores(q, by_id[q.clinic_id], preferences, q.all_in_price, None, now)
        return weighted(scores, skip="price")

    # Ties go to the alphabetically first id, the same as the ranker.
    best = min(solid, key=lambda q: (-non_price_score(q), q.clinic_id))
    cheapest = min(q.all_in_price for q in solid)
    return None if best.all_in_price <= cheapest else best.clinic_id


def plan_moves(
    target_quote: Quote,
    all_quotes: list[Quote],
    clinics: list[Clinic],
    preferences: Preferences,
) -> list[Move]:
    """The asks, in order. Only real quotes are cited; nothing outside the preferences is offered."""
    target_price = target_quote.all_in_price
    names = {c.id: c.name for c in clinics}
    if target_price is None or target_quote.clinic_id not in names:
        return []
    base = {"clinic_name": names[target_quote.clinic_id], "current_all_in": target_price}

    moves = []
    lower = [
        q
        for q in all_quotes
        if q.clinic_id != target_quote.clinic_id
        and q.clinic_id in names
        and q.is_solid
        and q.all_in_price is not None
        and q.all_in_price < target_price
    ]
    if lower:
        lowest = min(lower, key=lambda q: (q.all_in_price, q.clinic_id))
        moves.append(
            Move(
                kind=MoveKind.MATCH,
                cited_clinic=names[lowest.clinic_id],
                cited_amount=lowest.all_in_price,
                **base,
            )
        )
    records = target_quote.checklist.records_fee
    total = target_quote.checklist.total_price
    # The fee is billed on top when it shows up in the all-in price.
    if records and records.value > 0 and total and target_price > total.value:
        moves.append(Move(kind=MoveKind.WAIVE_RECORDS, records_fee=records.value, **base))
    if preferences.pay_in_full_ok:
        moves.append(Move(kind=MoveKind.PAY_IN_FULL, **base))
    moves.append(Move(kind=MoveKind.RETAINERS, **base))
    return moves


# ---------- phrasing (AI, checked by Python) ----------


def _prompts() -> tuple[str, str]:
    phrase, read = PROMPT_PATH.read_text(encoding="utf-8").split(PROMPT_SPLIT, 1)
    return phrase.strip(), read.strip()


def _facts(move: Move) -> str:
    facts = [f"- This clinic: {move.clinic_name}", f"- Its current all-in price: ${move.current_all_in:,}"]
    if move.cited_clinic is not None:
        facts.append(f"- Competing clinic: {move.cited_clinic}")
        facts.append(f"- Its real all-in quote: ${move.cited_amount:,}")
    if move.records_fee is not None:
        facts.append(f"- This clinic's records fee: ${move.records_fee:,}")
    return "\n".join(facts)


def _mentions(name: str, line: str) -> bool:
    words = {name, name.split()[0]}
    return any(re.search(rf"\b{re.escape(w)}\b", line, re.IGNORECASE) for w in words)


def phrasing_problem(move: Move, line: str) -> str | None:
    """Why the line says something not on the Move, or None if it is clean."""
    for amount in dollar_amounts(line):
        if amount not in move.allowed_amounts:
            return f"amount ${amount:,} is not on the move"
    allowed = [n.lower() for n in move.allowed_names]
    for clinic in get_public_clinics():
        if clinic.name.lower() not in allowed and _mentions(clinic.name, line):
            return f"names {clinic.name}"
    for match in _CLINIC_NAME_RE.finditer(line):
        found = match.group().strip().lower()
        if not any(a in found or found in a for a in allowed):
            return f"names an unknown clinic {match.group().strip()!r}"
    return None


async def _generate(move: Move) -> str:
    settings = get_settings()
    phrase, _ = _prompts()
    prompt = phrase.format(
        agent_name=settings.agent_name, move=move.kind.value, facts=_facts(move)
    )
    stream = stream_chat(
        [{"role": "user", "content": prompt}], settings.model_smart, reasoning=settings.call_reasoning
    )
    line = "".join([c async for c in stream]).strip().strip('"').strip()
    return re.sub(r"\s*—\s*", ", ", line)


async def phrase_move(move: Move) -> str:
    """One polite sentence from Lana. Regenerated once if it fails the check, then the template."""
    for attempt in (1, 2):
        try:
            line = await _generate(move)
        except Exception as err:
            log.warning("negotiator phrasing failed (attempt %d): %s", attempt, err)
            continue
        problem = phrasing_problem(move, line) if line else "empty line"
        if problem is None:
            return line
        log.warning("negotiator line rejected (attempt %d): %s: %r", attempt, problem, line)
    return move.template()


# ---------- reading the reply ----------


async def read_reply(move: Move, clinic_line: str) -> ReplyReading:
    settings = get_settings()
    _, read = _prompts()
    prompt = read.format(move=move.kind.value, facts=_facts(move), clinic_line=clinic_line)
    return await call_json(prompt, ReplyReading, settings.model_fast, reasoning=settings.call_reasoning)


def concession_from(
    move: Move, reading: ReplyReading, clinic_line: str, t_seconds: float, call_id: str
) -> Concession | None:
    """A Concession only for a clear yes backed by the clinic's words. Anything else is a no."""
    if reading.verdict != Verdict.YES:
        return None
    evidence = find_evidence(reading.evidence, clinic_line)
    if evidence is None:
        log.info("negotiator dropped %s: evidence not in clinic line: %r", move.kind, reading.evidence)
        return None
    stated = set(dollar_amounts(clinic_line))
    current = move.current_all_in

    if move.kind == MoveKind.MATCH:
        new = reading.new_all_in if reading.new_all_in in stated else move.cited_amount
        amount = current - new if new is not None else 0
    elif move.kind == MoveKind.WAIVE_RECORDS:
        amount = move.records_fee or 0
    elif move.kind == MoveKind.PAY_IN_FULL:
        if reading.amount_off in stated:
            amount = reading.amount_off
        elif reading.new_all_in in stated:
            amount = current - reading.new_all_in
        elif percent := _PERCENT_RE.search(clinic_line):
            # Whole dollars, halves rounded up (Python's round() would round 252.5 down).
            amount = math.floor(current * float(percent[1]) / 100 + 0.5)
        else:
            amount = 0  # a discount with no amount the clinic actually said
    else:
        amount = 0

    is_cash = move.kind != MoveKind.RETAINERS
    if is_cash and not 0 < amount <= current:
        return None
    return Concession(
        label=move.label,
        amount=amount,
        evidence=SourcedAnswer[int | bool | str](
            value=amount if is_cash else True,
            speaker=Speaker.CLINIC,
            evidence=evidence,
            call_id=call_id,
            t_seconds=t_seconds,
        ),
    )


async def record_reply(
    move: Move, clinic_line: str, t_seconds: float, call_id: str
) -> Concession | None:
    """Yes with evidence -> Concession. No, unclear or unbacked -> None."""
    reading = await read_reply(move, clinic_line)
    return concession_from(move, reading, clinic_line, t_seconds, call_id)


# ---------- running the moves on a call ----------


class _Call:
    def __init__(self, receptionist: Receptionist, on_line: OnLine, start_t: float = 0.0) -> None:
        self.receptionist = receptionist
        self.on_line = on_line
        self.clock = start_t

    async def say(self, speaker: Speaker, text: str) -> float:
        t = round(self.clock, 1)
        self.clock += len(text.split()) / WORDS_PER_SECOND
        result = self.on_line(speaker, text, t)
        if inspect.isawaitable(result):
            await result
        return t

    async def exchange(self, lana_line: str) -> tuple[str, float]:
        await self.say(Speaker.AGENT, lana_line)
        reply = await self.receptionist.next_line(lana_line)
        return reply, await self.say(Speaker.CLINIC, reply)


async def negotiate(
    target_quote: Quote,
    moves: list[Move],
    receptionist: Receptionist,
    on_line: OnLine,
    call_id: str,
    opening: bool = True,
    opening_line: str | None = None,
    start_t: float = 0.0,
) -> NegotiationResult:
    """Ask each move once (at most 4 asks). A no ends that move; the next one is tried.

    `opening_line` (e.g. "Thanks for holding.") goes in front of Lana's first line, after the
    disclosure. `start_t` continues a call's clock, so evidence seconds match that call.
    """
    call = _Call(receptionist, on_line, start_t)
    first = target_quote.all_in_price or 0
    current = first
    concessions: list[Concession] = []
    refused: list[str] = []
    matched = False
    asks = 0

    for planned in moves:
        if asks >= MAX_ASKS:
            break
        if planned.kind == MoveKind.WAIVE_RECORDS and matched:
            continue  # the matched price was already all-in
        move = planned.model_copy(update={"current_all_in": current})
        line = await phrase_move(move)
        if asks == 0 and opening_line:
            line = f"{opening_line} {line}"
        if opening and asks == 0:
            line = f"{DISCLOSURE.format(agent_name=get_settings().agent_name)} {line}"
        asks += 1

        reply, t = await call.exchange(line)
        reading = await _read_safely(move, reply)
        answered_source = waited = False
        while True:
            if reading.verdict == Verdict.ASKS_WHICH_CLINIC and move.cited_clinic and not answered_source:
                answered_source = True
                reply, t = await call.exchange(SOURCE_LINE.format(**move.model_dump()))
            elif reading.verdict == Verdict.PENDING and not waited:
                waited = True
                reply, t = await call.exchange(WAIT_LINE)
            else:
                break
            reading = await _read_safely(move, reply)

        concession = concession_from(move, reading, reply, t, call_id)
        if concession is None:
            refused.append(move.label)
            continue
        concessions.append(concession)
        current -= concession.amount
        matched = matched or move.kind == MoveKind.MATCH

    closing = f"{opening_line} {CLOSING_LINE}" if asks == 0 and opening_line else CLOSING_LINE
    await call.say(Speaker.AGENT, closing)
    return NegotiationResult(
        clinic_id=target_quote.clinic_id,
        first_all_in=first,
        final_all_in=current,
        concessions=concessions,
        refused=refused,
    )


async def _read_safely(move: Move, reply: str) -> ReplyReading:
    """A reply we can't read counts as unclear, which counts as no."""
    try:
        return await read_reply(move, reply)
    except Exception as err:
        log.warning("negotiator could not read reply: %s", err)
        return ReplyReading(verdict=Verdict.UNCLEAR)
