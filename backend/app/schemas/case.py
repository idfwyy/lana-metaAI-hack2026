from uuid import uuid4

from pydantic import BaseModel, Field

from app.schemas.clinic import Clinic
from app.schemas.enums import CaseStatus, DemoMode
from app.schemas.negotiation import NegotiationResult
from app.schemas.offer import Offer
from app.schemas.plan import ParsedPlan
from app.schemas.preferences import Preferences
from app.schemas.quote import Quote


class Case(BaseModel):
    """One patient's request, from intake to booking. Moved through its states by orchestrator.py."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    status: CaseStatus = CaseStatus.NEW
    mode: DemoMode = DemoMode.WATCH
    preferences: Preferences | None = None
    plan: ParsedPlan | None = None
    clinics: list[Clinic] = Field(default_factory=list, description="Picked clinics, in call order")
    quotes: list[Quote] = Field(default_factory=list, description="Latest quote per clinic")
    held_call_id: str | None = Field(default=None, description="Set while a call is on hold")
    negotiation: NegotiationResult | None = None
    offers: list[Offer] = Field(default_factory=list)
    error: str | None = None
