from pydantic import BaseModel, Field

from app.schemas.checklist import ClinicChecklist


class Quote(BaseModel):
    """A clinic's price, built from its filled checklist."""

    clinic_id: str
    checklist: ClinicChecklist
    all_in_price: int | None = Field(
        default=None, ge=0, description="Price plus fees that are not included; None if not confirmed"
    )
    missing_fields: list[str] = Field(default_factory=list)
    is_solid: bool = False
