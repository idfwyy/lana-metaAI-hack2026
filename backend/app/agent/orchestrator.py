"""State machine for the whole case. Owns the Case, decides every next step, emits every event.

Plain Python decides the call order, when to hold and resume, and who to negotiate with.
AI runs only inside the steps it calls (caller, extractor, negotiator phrasing, tradeoffs,
the callback summary).
"""

import inspect
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from app.agent.caller import WORDS_PER_SECOND, Caller
from app.agent.extractor import build_quote
from app.agent.negotiator import MoveKind, negotiate, pick_target, plan_moves
from app.agent.ranker import add_tradeoffs, rank_offers
from app.agent.researcher import pick_clinics, slot_matches
from app.clinics.simulator import ClinicSimulator, dollar_amounts
from app.config import get_settings
from app.data.loader import load_demo_clinics
from app.integrations.meta_llm import stream_chat
from app.schemas.case import Case
from app.schemas.checklist import ClinicChecklist
from app.schemas.clinic import Clinic, PriceSheet
from app.schemas.enums import CallStatus, CaseStatus, Speaker
from app.schemas.events import (
    Booked,
    CallbackStarted,
    CallStatusEvent,
    CaseStatusEvent,
    ConcessionWon,
    ErrorEvent,
    LogLine,
    OffersReady,
    QuoteUpdated,
    TranscriptFinal,
)
from app.schemas.negotiation import Concession, NegotiationResult
from app.schemas.offer import Offer
from app.schemas.preferences import TIME_PLURAL
from app.schemas.quote import Quote

log = logging.getLogger(__name__)

S = CaseStatus

# SETUP 3.8. CALLING -> NEGOTIATING (resuming the held call) is checked in _allowed().
ALLOWED: dict[CaseStatus, set[CaseStatus]] = {
    S.NEW: {S.INTAKE},
    S.INTAKE: {S.PLAN_CHECK, S.RESEARCH},
    S.PLAN_CHECK: {S.RESEARCH},
    S.RESEARCH: {S.CALLING},
    S.CALLING: {S.HOLDING, S.CALLING, S.QUOTES_IN},
    S.HOLDING: {S.CALLING},
    S.QUOTES_IN: {S.NEGOTIATING, S.OFFERS_READY},
    S.NEGOTIATING: {S.OFFERS_READY},
    S.OFFERS_READY: {S.CALLBACK},
    S.CALLBACK: {S.BOOKED, S.SAVED},
    S.BOOKED: set(),
    S.SAVED: set(),
    S.ERROR: set(),
}

RESUME_LINE = "Thanks for holding."
NO_DEAL_LINE = (
    "Thanks for holding. That's everything I needed, I'll share your quote with the patient "
    "and we'll call back to book. Thank you so much!"
)
RESUME_CLOSE_LINE = (
    "Thanks for holding. That's everything I needed for now, I'll share your quote with the "
    "patient. Thank you so much for your help!"
)

SUMMARY_PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "summary.md"
# Said when the model's summary fails the number check, errors or comes back empty.
SUMMARY_TEMPLATE = "{clinic} came in at ${final:,} all in."
SUMMARY_SAVED = " I got it down from ${first:,}, so you save ${saved:,}."
SUMMARY_SLOT = " Their next opening is {slot}."
NO_OFFER_SUMMARY = "I called the clinics, but none confirmed a price within what you asked for."
# When even the best offer is over the budget, the summary says so first.
OVER_BUDGET_SUMMARY = (
    "Nobody would go under ${budget:,}. The best I got was ${final:,} at {clinic}, ${over:,} over your budget."
)
# Put in front of the summary when the patient stepped away during the calls.
AWAY_LINE = "I made {n} call{s} while you were away."
_GREETING_RE = re.compile(r"^\s*(?:hi|hey|hello)\b[^.!?]*[.!?]\s*", re.IGNORECASE)

Emit = Callable[[BaseModel], None | Awaitable[None]]


class IllegalTransition(Exception):
    pass


class CaseFailed(Exception):
    """A failure whose message is already plain enough to show the patient."""


def _short(name: str) -> str:
    return name.split()[0]


def spoken_slot(slot: datetime) -> str:
    """'Tuesday, November 4 at 5:30 PM' in the clinics' time zone."""
    local = slot.astimezone(get_settings().tz)
    return f"{local:%A, %B} {local.day} at {local.hour % 12 or 12}:{local:%M %p}"


def first_slot(offer: Offer) -> datetime | None:
    slots = offer.quote.checklist.next_slots
    return min(slots.value) if slots is not None and slots.value else None


def pick_slot(offer: Offer, time_pref: str = "any") -> tuple[datetime | None, bool]:
    """The earliest slot in the patient's preferred time, and True.

    With none in that time: the earliest slot of any time and False. With no slots: (None, False).
    """
    slots = offer.quote.checklist.next_slots
    times = sorted(slots.value) if slots is not None else []
    match = next((s for s in times if slot_matches(s, time_pref)), None)
    if match is not None:
        return match, True
    return (times[0] if times else None), False


@dataclass
class NoMatchingSlot:
    """book() found no slot in the patient's preferred time; nothing was booked."""

    clinic_id: str
    time_pref: str
    earliest: datetime


def _final_all_in(offer: Offer) -> int | None:
    return offer.negotiation.final_all_in if offer.negotiation is not None else offer.quote.all_in_price


def allowed_amounts(case: Case) -> set[int]:
    """Every dollar amount the summary may say: what the clinics quoted and what Lana won."""
    amounts: set[int] = set()
    for quote in case.quotes:
        if quote.all_in_price is not None:
            amounts.add(quote.all_in_price)
        c = quote.checklist
        for answer in (c.total_price, c.records_fee, c.consult_fee, c.monthly_payment, c.down_payment):
            if answer is not None:
                amounts.add(answer.value)
    deals = [case.negotiation] if case.negotiation else []
    deals += [o.negotiation for o in case.offers if o.negotiation is not None]
    for deal in deals:
        amounts |= {deal.first_all_in, deal.final_all_in, deal.total_saved}
        amounts |= {c.amount for c in deal.concessions if c.amount}
    # The budget and how far each offer is over it, for an honest over-budget summary.
    if case.preferences is not None:
        amounts.add(case.preferences.budget_max)
    amounts |= {o.over_budget_by for o in case.offers if o.over_budget_by}
    return amounts


def summary_problem(case: Case, text: str) -> str | None:
    """Why the summary can't be said, or None. Every dollar amount must be in the case's data."""
    if not text.strip():
        return "empty summary"
    allowed = allowed_amounts(case)
    for amount in dollar_amounts(text):
        if amount not in allowed:
            return f"amount ${amount:,} is not in the offers"
    return None


def template_summary(case: Case) -> str:
    if not case.offers or _final_all_in(case.offers[0]) is None:
        return NO_OFFER_SUMMARY
    top = case.offers[0]
    if top.over_budget_by and case.preferences is not None:
        text = OVER_BUDGET_SUMMARY.format(
            budget=case.preferences.budget_max, final=_final_all_in(top), clinic=top.clinic.name, over=top.over_budget_by
        )
    else:
        text = SUMMARY_TEMPLATE.format(clinic=top.clinic.name, final=_final_all_in(top))
    if top.negotiation is not None and top.negotiation.total_saved > 0:
        text += SUMMARY_SAVED.format(first=top.negotiation.first_all_in, saved=top.negotiation.total_saved)
    slot, matches = pick_slot(top, _time_pref(case))
    if slot is not None and matches:
        text += SUMMARY_SLOT.format(slot=spoken_slot(slot))
    return text


def _time_pref(case: Case) -> str:
    return case.preferences.time_pref if case.preferences is not None else "any"


def _summary_facts(case: Case) -> str:
    top = case.offers[0]
    facts = [f"- Best clinic: {top.clinic.name}", f"- Its final all-in price: ${_final_all_in(top):,}"]
    prefs = case.preferences
    if top.over_budget_by and prefs is not None:
        facts.append("- No clinic came in under the patient's budget")
        facts.append(f"- Patient's budget: ${prefs.budget_max:,}")
        facts.append(f"- Best clinic is over budget by: ${top.over_budget_by:,}")
    if prefs is not None and prefs.place:
        facts.append(f"- These are demo clinics placed near {prefs.place}, not real clinics there")
    deal = top.negotiation
    if deal is not None and deal.total_saved > 0:
        facts.append(f"- Its price before {get_settings().agent_name} negotiated: ${deal.first_all_in:,}")
        facts.append(f"- Total saved: ${deal.total_saved:,}")
        facts += [f"- Won: {c.label}" + (f" (${c.amount:,})" if c.amount else "") for c in deal.concessions]
    monthly = top.quote.checklist.monthly_payment
    if monthly is not None:
        facts.append(f"- Monthly payment: ${monthly.value:,}")
    time_pref = _time_pref(case)
    slot, matches = pick_slot(top, time_pref)
    if slot is not None and matches:
        when = f" in the patient's preferred time ({TIME_PLURAL[time_pref]})" if time_pref != "any" else ""
        facts.append(f"- Next opening{when}: {spoken_slot(slot)}")
    elif slot is not None:
        # The booking question right after the summary says this; the summary leaves the slot out.
        facts.append(f"- No opening in the patient's preferred time ({TIME_PLURAL[time_pref]}); the question after your summary covers it")
    checklist = top.quote.checklist
    if checklist.note_question and checklist.note_answer is not None:
        facts.append(f"- {get_settings().agent_name} asked: {checklist.note_question}")
        facts.append(f"- The clinic said: {checklist.note_answer.value}")
    for other in case.offers[1:]:
        price = _final_all_in(other)
        if price is not None:
            facts.append(f"- Also called: {other.clinic.name}, ${price:,} all in")
    return "\n".join(facts)


async def write_summary(case: Case) -> str:
    """Lana's callback summary from MODEL_SMART, checked in Python; the template if it fails."""
    if not case.offers or _final_all_in(case.offers[0]) is None:
        return template_summary(case)
    settings = get_settings()
    prompt = SUMMARY_PROMPT.read_text(encoding="utf-8").format(
        agent_name=settings.agent_name, facts=_summary_facts(case)
    )
    try:
        stream = stream_chat(
            [{"role": "user", "content": prompt}], settings.model_smart, reasoning=settings.call_reasoning
        )
        text = "".join([c async for c in stream]).strip().strip('"').strip()
    except Exception as err:
        log.warning("case=%s summary failed, using the template: %s", case.id, err)
        return template_summary(case)
    text = re.sub(r"\s*—\s*", ", ", text)
    problem = summary_problem(case, text)
    if problem is not None:
        log.warning("case=%s summary rejected (%s): %r", case.id, problem, text)
        return template_summary(case)
    return text


class _Call:
    """One phone call: its own id, receptionist and clock (seconds from the start of the call)."""

    def __init__(
        self, orchestrator: "Orchestrator", clinic: Clinic, sheet: PriceSheet
    ) -> None:
        self.orchestrator = orchestrator
        self.clinic = clinic
        self.receptionist = ClinicSimulator(clinic, sheet)
        self.call_id = f"{clinic.id}-{uuid.uuid4().hex[:6]}"
        self.clock = 0.0

    async def line(self, speaker: Speaker, text: str, t_seconds: float) -> None:
        """on_line for Caller and negotiate(): emit the line and remember where the call is."""
        self.clock = t_seconds + len(text.split()) / WORDS_PER_SECOND
        await self.orchestrator._emit(
            TranscriptFinal(
                case_id=self.orchestrator.case.id,
                call_id=self.call_id,
                speaker=speaker,
                text=text,
                t_seconds=t_seconds,
            )
        )

    async def say(self, text: str) -> None:
        """A line from Lana that the orchestrator decides itself."""
        await self.line(Speaker.AGENT, text, round(self.clock, 1))

    async def status(self, status: CallStatus) -> None:
        await self.orchestrator._emit(
            CallStatusEvent(
                case_id=self.orchestrator.case.id,
                call_id=self.call_id,
                clinic_id=self.clinic.id,
                status=status,
            )
        )


class Orchestrator:
    def __init__(self, case: Case, emit: Emit, max_clinics: int = 3) -> None:
        if max_clinics not in (2, 3):
            raise ValueError("max_clinics must be 2 or 3")
        self.case = case
        self.emit = emit
        self.max_clinics = max_clinics
        self._step = "starting the case"
        self._sheets: dict[str, PriceSheet] = {}
        self.summary: str | None = None  # set by start_callback()
        self.calls_made = 0  # calls dialed; resuming a held call is not a new one
        self.stepped_away = False  # set by the server when the patient steps away during the calls

    # ---------- states ----------

    def _allowed(self, current: CaseStatus, to: CaseStatus) -> bool:
        if to == S.ERROR:
            return current != S.ERROR
        if current == S.CALLING and to == S.NEGOTIATING:
            return self.case.held_call_id is not None
        return to in ALLOWED[current]

    async def advance(self, to: CaseStatus) -> None:
        current = self.case.status
        if not self._allowed(current, to):
            raise IllegalTransition(f"cannot move from {current.value} to {to.value}")
        self.case.status = to
        await self._emit(CaseStatusEvent(case_id=self.case.id, status=to, previous=current))

    async def _emit(self, event: BaseModel) -> None:
        result = self.emit(event)
        if inspect.isawaitable(result):
            await result

    async def _log(self, text: str) -> None:
        await self._emit(LogLine(case_id=self.case.id, text=text))

    # ---------- the run ----------

    async def run(self) -> Case:
        """Run the case to OFFERS_READY, or to ERROR with an error event. Never raises."""
        try:
            await self._run()
        except Exception as err:
            log.exception("case=%s failed while %s", self.case.id, self._step)
            await self._fail(err)
        return self.case

    async def _fail(self, err: Exception) -> None:
        step = self.case.status
        if isinstance(err, CaseFailed):
            message = str(err)
        else:
            message = f"Something went wrong while {self._step}. Please try again."
        self.case.error = message
        try:
            if step != S.ERROR:
                await self.advance(S.ERROR)
            await self._emit(
                ErrorEvent(case_id=self.case.id, message=message, step=step.value, recoverable=False)
            )
        except Exception:
            log.exception("case=%s could not emit the error", self.case.id)
            self.case.status = S.ERROR

    async def _run(self) -> None:
        prefs = self.case.preferences
        if prefs is None:
            raise CaseFailed("The case has no preferences yet, so there is nothing to search for.")
        # Intake is not built yet: preferences arrive with the case, so intake is already done.
        if self.case.status == S.NEW:
            await self.advance(S.INTAKE)
        if self.case.status in (S.INTAKE, S.PLAN_CHECK):
            await self.advance(S.RESEARCH)

        self._step = "finding clinics"
        lines: list[str] = []
        clinics = pick_clinics(prefs, self.max_clinics, on_log=lines.append)
        for line in lines:
            await self._log(line)
        if not clinics:
            raise CaseFailed(f"No demo clinics within {prefs.max_distance_mi:g} miles.")
        self.case.clinics = clinics
        self._sheets = {clinic.id: sheet for clinic, sheet in load_demo_clinics()}

        first, others = clinics[0], clinics[1:]
        await self.advance(S.CALLING)
        held = await self._checklist_call(first, end_with="hold" if others else "close")
        if others:
            self.case.held_call_id = held.call_id
            await self.advance(S.HOLDING)
            await held.status(CallStatus.ON_HOLD)
            await self._log(self._hold_line(first, others))
        else:
            await held.status(CallStatus.ENDED)

        for clinic in others:
            await self.advance(S.CALLING)
            call = await self._checklist_call(clinic, end_with="close")
            await call.status(CallStatus.ENDED)
            await self._log(self._compare_line(clinic, first))

        if others:
            negotiation = await self._resume(held)
        else:
            await self.advance(S.QUOTES_IN)
            negotiation = None
            target_id = pick_target(self.case.quotes, self.case.clinics, prefs)
            if target_id is not None:
                await self.advance(S.NEGOTIATING)
                negotiation = await self._negotiate(self._new_call(self._clinic(target_id)))

        await self._finish(negotiation)

    # ---------- steps ----------

    def _clinic(self, clinic_id: str) -> Clinic:
        return next(c for c in self.case.clinics if c.id == clinic_id)

    def _quote(self, clinic_id: str) -> Quote:
        return next(q for q in self.case.quotes if q.clinic_id == clinic_id)

    def _price(self, clinic_id: str) -> int | None:
        quote = next((q for q in self.case.quotes if q.clinic_id == clinic_id), None)
        return quote.all_in_price if quote is not None else None

    def _hold_line(self, first: Clinic, others: list[Clinic]) -> str:
        """'Parkway is $5,050 all in. That's the best fit so far, so I'm asking them to hold while I check Sunrise'."""
        name, price = _short(first.name), self._price(first.id)
        check = " and ".join(_short(c.name) for c in others)
        said = f"{name} is ${price:,} all in. That's" if price is not None else f"{name} didn't confirm a full price, but they're"
        return f"{said} the best fit so far, so I'm asking them to hold while I check {check}"

    def _compare_line(self, clinic: Clinic, held: Clinic) -> str:
        """'Sunrise is $4,200 all in, $850 less than Parkway'."""
        name, price, other = _short(clinic.name), self._price(clinic.id), self._price(held.id)
        if price is None:
            return f"{name} didn't confirm a full price"
        line = f"{name} is ${price:,} all in"
        if other is None:
            return line
        if price == other:
            return f"{line}, the same as {_short(held.name)}"
        gap = abs(other - price)
        return f"{line}, ${gap:,} {'less' if price < other else 'more'} than {_short(held.name)}"

    def _new_call(self, clinic: Clinic) -> _Call:
        self.calls_made += 1
        return _Call(self, clinic, self._sheets[clinic.id])

    async def _set_quote(self, quote: Quote) -> None:
        self.case.quotes = [q for q in self.case.quotes if q.clinic_id != quote.clinic_id] + [quote]
        await self._emit(QuoteUpdated(case_id=self.case.id, quote=quote))

    async def _checklist_call(self, clinic: Clinic, end_with: str) -> _Call:
        """The checklist call. Every line and every quote change is emitted as it happens."""
        self._step = f"calling {clinic.name}"
        call = self._new_call(clinic)
        await call.status(CallStatus.DIALING)
        await call.status(CallStatus.LIVE)

        async def on_checklist(checklist: ClinicChecklist) -> None:
            await self._set_quote(build_quote(clinic.id, checklist))

        caller = Caller(
            clinic,
            self.case.preferences,
            call.line,
            call_id=call.call_id,
            end_with=end_with,
            on_checklist=on_checklist,
        )
        result = await caller.run(call.receptionist)
        if not any(q.clinic_id == clinic.id for q in self.case.quotes):
            await self._set_quote(build_quote(clinic.id, result.checklist))
        return call

    async def _resume(self, held: _Call) -> NegotiationResult | None:
        """Back to the held clinic, then negotiate with whoever pick_target chose."""
        self._step = f"resuming the call with {held.clinic.name}"
        await self.advance(S.NEGOTIATING)
        self.case.held_call_id = None
        await held.status(CallStatus.LIVE)

        target_id = pick_target(self.case.quotes, self.case.clinics, self.case.preferences)
        if target_id == held.clinic.id:
            return await self._negotiate(held, resumed=True)

        if target_id is None:
            await held.say(NO_DEAL_LINE)
            await held.status(CallStatus.ENDED)
            await self._log(f"{_short(held.clinic.name)} is already the best deal, so I'm wrapping up with them")
            return None

        await self._log(f"Wrapping up with {_short(held.clinic.name)}")
        await held.say(RESUME_CLOSE_LINE)
        await held.status(CallStatus.ENDED)
        call = self._new_call(self._clinic(target_id))
        await call.status(CallStatus.DIALING)
        return await self._negotiate(call)

    async def _negotiate(self, call: _Call, resumed: bool = False) -> NegotiationResult:
        name = _short(call.clinic.name)
        self._step = f"negotiating with {call.clinic.name}"
        quote = self._quote(call.clinic.id)
        moves = plan_moves(quote, self.case.quotes, self.case.clinics, self.case.preferences)
        match = next((m for m in moves if m.kind == MoveKind.MATCH), None)
        lead = f"Going back to {name}" if resumed else f"Calling {name} back"
        if match is not None:
            await self._log(f"{lead} to ask them to match {_short(match.cited_clinic)}'s ${match.cited_amount:,}")
        else:
            await self._log(f"{lead} to ask for a better price")

        async def on_concession(concession: Concession, new_all_in: int) -> None:
            # The price drops on screen the moment the clinic agrees, while the call goes on.
            await self._emit(
                ConcessionWon(
                    case_id=self.case.id,
                    clinic_id=call.clinic.id,
                    concession=concession,
                    new_all_in=new_all_in,
                )
            )

        await call.status(CallStatus.NEGOTIATING)
        result = await negotiate(
            quote,
            moves,
            call.receptionist,
            call.line,
            call.call_id,
            opening=not resumed,
            opening_line=RESUME_LINE if resumed else None,
            start_t=call.clock,
            on_concession=on_concession,
        )
        await call.status(CallStatus.ENDED)
        if result.total_saved > 0:
            await self._log(f"{name} came down to ${result.final_all_in:,}, ${result.total_saved:,} off")
        else:
            await self._log(f"{name} wouldn't move from ${result.final_all_in:,}")
        return result

    async def _finish(self, negotiation: NegotiationResult | None) -> None:
        self._step = "ranking the offers"
        self.case.negotiation = negotiation
        offers = rank_offers(
            self.case.quotes,
            self.case.clinics,
            self.case.preferences,
            [negotiation] if negotiation is not None else None,
        )
        try:
            offers = await add_tradeoffs(offers)
        except Exception as err:
            # Tradeoff lines are extra; the ranked offers stand without them.
            log.warning("case=%s tradeoffs failed, offers kept without them: %s", self.case.id, err)
        self.case.offers = offers
        await self._emit(OffersReady(case_id=self.case.id, offers=offers))
        await self.advance(S.OFFERS_READY)

    # ---------- the callback ----------

    async def start_callback(self) -> str:
        """OFFERS_READY -> CALLBACK: write Lana's summary and tell the browser to ring."""
        self._step = "writing the summary"
        await self.advance(S.CALLBACK)
        summary = await write_summary(self.case)
        if self.stepped_away and self.calls_made:
            # "I made 3 calls while you were away." comes first, before any "Hi, it's Lana."
            away = AWAY_LINE.format(n=self.calls_made, s="s" if self.calls_made != 1 else "")
            summary = f"{away} {_GREETING_RE.sub('', summary, count=1)}"
        self.summary = summary
        await self._emit(CallbackStarted(case_id=self.case.id, summary=self.summary))
        return self.summary

    async def book(self, any_time: bool = False) -> Booked | NoMatchingSlot | None:
        """CALLBACK -> BOOKED at the winner's earliest slot in the patient's preferred time.

        If none is in that time, nothing is booked and NoMatchingSlot says what the earliest is;
        any_time=True (the patient said yes to that slot) books it. None if the clinic gave no slot.
        """
        top = self.case.offers[0] if self.case.offers else None
        if top is None:
            return None
        time_pref = _time_pref(self.case)
        slot, matches = pick_slot(top, time_pref)
        if slot is None:
            return None
        if not matches and not any_time:
            return NoMatchingSlot(clinic_id=top.clinic.id, time_pref=time_pref, earliest=slot)
        deal = top.negotiation
        saved = deal.total_saved if deal is not None and deal.clinic_id == top.clinic.id else 0
        await self.advance(S.BOOKED)
        booked = Booked(case_id=self.case.id, clinic_id=top.clinic.id, slot=slot, total_saved=saved)
        await self._emit(booked)
        return booked

    async def save(self) -> None:
        """CALLBACK -> SAVED: the patient said not yet; the quote is kept, nothing is booked."""
        await self.advance(S.SAVED)
