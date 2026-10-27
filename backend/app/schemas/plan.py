from pydantic import BaseModel, Field

from app.schemas.enums import Treatment


class ProcedureItem(BaseModel):
    """One line as written on the plan. Copied, never interpreted."""

    description: str
    cdt_code: str | None = None
    fee: int | None = Field(default=None, ge=0)


class ParsedPlan(BaseModel):
    """What plan_reader.py read from a plan photo. Unknown fields stay None."""

    treatment: Treatment | None = None
    provider_name: str | None = None
    items: list[ProcedureItem] = Field(default_factory=list)
    total_fee: int | None = Field(default=None, ge=0)
    estimated_months: int | None = Field(default=None, ge=0)
    notes: str | None = None
    confirmed_by_patient: bool = False
