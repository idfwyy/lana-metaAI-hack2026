"""State machine for the whole case. Owns the Case, decides every next step, emits every event.

Plain Python decides the call order, when to hold and resume, and who to negotiate with.
AI runs only inside the steps it calls (caller, extractor, negotiator phrasing, tradeoffs).
"""

import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from app.agent.caller import WORDS_PER_SECOND, Caller
from app.agent.extractor import build_quote
from app.agent.negotiator import MoveKind, negotiate, pick_target, plan_moves
from app.agent.ranker import add_tradeoffs, rank_offers
from app.agent.researcher import pick_clinics
from app.clinics.simulator import ClinicSimulator
from app.data.loader import load_demo_clinics
from app.schemas.case import Case
from app.schemas.checklist import ClinicChecklist
from app.schemas.clinic import Clinic, PriceSheet
from app.schemas.enums import CallStatus, CaseStatus, Speaker
from app.schemas.events import (
    CallStatusEvent,
    CaseStatusEvent,
    ConcessionWon,
    ErrorEvent,
    LogLine,
    OffersReady,
    QuoteUpdated,
    TranscriptFinal,
)
from app.schemas.negotiation import NegotiationResult
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

Emit = Callable[[BaseModel], None | Awaitable[None]]


class IllegalTransition(Exception):
    pass


class CaseFailed(Exception):
    """A failure whose message is already plain enough to show the patient."""


def _short(name: str) -> str:
    return name.split()[0]


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
            await self._log(f"{_short(first.name)} is holding while I check the others")
        else:
            await held.status(CallStatus.ENDED)

        for clinic in others:
            await self.advance(S.CALLING)
            call = await self._checklist_call(clinic, end_with="close")
            await call.status(CallStatus.ENDED)

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

    def _new_call(self, clinic: Clinic) -> _Call:
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
        await self._log(f"Back to {_short(held.clinic.name)}")

        target_id = pick_target(self.case.quotes, self.case.clinics, self.case.preferences)
        if target_id == held.clinic.id:
            return await self._negotiate(held, resumed=True)

        if target_id is None:
            await held.say(NO_DEAL_LINE)
            await held.status(CallStatus.ENDED)
            await self._log(f"{_short(held.clinic.name)} is already the best deal, no need to negotiate")
            return None

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
        if match is not None:
            await self._log(f"Asking {name} to match {_short(match.cited_clinic)}'s ${match.cited_amount:,}")
        else:
            await self._log(f"Negotiating with {name}")

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
        )
        await call.status(CallStatus.ENDED)

        running = result.first_all_in
        for concession in result.concessions:
            running -= concession.amount
            await self._emit(
                ConcessionWon(
                    case_id=self.case.id,
                    clinic_id=call.clinic.id,
                    concession=concession,
                    new_all_in=running,
                )
            )
        await self._log(f"Saved ${result.total_saved:,} with {name}")
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
