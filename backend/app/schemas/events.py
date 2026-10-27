"""Every WebSocket event sent to the browser. `type` is the discriminator."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, TypeAdapter

from app.config import get_settings
from app.schemas.enums import CallStatus, CaseStatus, Speaker
from app.schemas.negotiation import Concession
from app.schemas.offer import Offer
from app.schemas.quote import Quote


def _now() -> datetime:
    return datetime.now(get_settings().tz)


class _Event(BaseModel):
    case_id: str
    at: AwareDatetime = Field(default_factory=_now)


class CaseStatusEvent(_Event):
    type: Literal["case_status"] = "case_status"
    status: CaseStatus
    previous: CaseStatus | None = None


class AgentSaid(_Event):
    """Lana speaking. call_id is None when she is talking to the patient."""

    type: Literal["agent_said"] = "agent_said"
    text: str
    call_id: str | None = None


class UserSaid(_Event):
    type: Literal["user_said"] = "user_said"
    text: str


class LogLine(_Event):
    type: Literal["log_line"] = "log_line"
    text: str


class CallStatusEvent(_Event):
    type: Literal["call_status"] = "call_status"
    call_id: str
    clinic_id: str
    status: CallStatus


class TranscriptPartial(_Event):
    type: Literal["transcript_partial"] = "transcript_partial"
    call_id: str | None = None
    speaker: Speaker
    text: str


class TranscriptFinal(_Event):
    type: Literal["transcript_final"] = "transcript_final"
    call_id: str | None = None
    speaker: Speaker
    text: str
    t_seconds: float = Field(ge=0)


class QuoteUpdated(_Event):
    type: Literal["quote_updated"] = "quote_updated"
    quote: Quote


class ConcessionWon(_Event):
    type: Literal["concession_won"] = "concession_won"
    clinic_id: str
    concession: Concession
    new_all_in: int = Field(ge=0)


class OffersReady(_Event):
    type: Literal["offers_ready"] = "offers_ready"
    offers: list[Offer]


class CallbackStarted(_Event):
    type: Literal["callback_started"] = "callback_started"
    summary: str


class Booked(_Event):
    type: Literal["booked"] = "booked"
    clinic_id: str
    slot: AwareDatetime
    total_saved: int = Field(ge=0)


class ErrorEvent(_Event):
    type: Literal["error"] = "error"
    message: str
    step: str | None = None
    recoverable: bool = True


Event = Annotated[
    CaseStatusEvent
    | AgentSaid
    | UserSaid
    | LogLine
    | CallStatusEvent
    | TranscriptPartial
    | TranscriptFinal
    | QuoteUpdated
    | ConcessionWon
    | OffersReady
    | CallbackStarted
    | Booked
    | ErrorEvent,
    Field(discriminator="type"),
]

EventAdapter: TypeAdapter[Event] = TypeAdapter(Event)
