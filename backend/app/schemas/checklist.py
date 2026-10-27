from typing import Generic, TypeVar

from pydantic import AwareDatetime, BaseModel, Field

from app.schemas.enums import Speaker

T = TypeVar("T")


class SourcedAnswer(BaseModel, Generic[T]):
    """A fact plus who said it, their exact words, and when in the call."""

    value: T
    speaker: Speaker
    evidence: str = Field(description="Exact words from the transcript")
    call_id: str
    t_seconds: float = Field(ge=0)


class ClinicChecklist(BaseModel):
    """What Lana asks every clinic. None means not confirmed."""

    total_price: SourcedAnswer[int] | None = None
    records_fee: SourcedAnswer[int] | None = None
    consult_fee: SourcedAnswer[int] | None = None
    includes: SourcedAnswer[list[str]] | None = None
    monthly_payment: SourcedAnswer[int] | None = None
    down_payment: SourcedAnswer[int] | None = None
    payment_plan: SourcedAnswer[bool] | None = None
    next_slots: SourcedAnswer[list[AwareDatetime]] | None = None
    evening_hours: SourcedAnswer[bool] | None = None
    needs_exam_first: SourcedAnswer[bool] | None = None
