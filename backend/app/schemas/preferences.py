from pydantic import BaseModel, Field

from app.schemas.enums import Treatment


class Preferences(BaseModel):
    """The patient's intake answers. Money is whole dollars."""

    treatment: Treatment
    zip_code: str = Field(pattern=r"^\d{5}$")
    max_distance_mi: float = Field(gt=0)
    budget_max: int = Field(ge=0)
    # Defaults are the conservative choice: Lana never offers what the patient did not agree to.
    pay_in_full_ok: bool = False
    uses_hsa_fsa: bool = False
    prefers_monthly: bool = False
    has_xrays: bool = False
    has_plan: bool = False
    must_haves: list[str] = Field(default_factory=list, examples=[["evening hours"]])
    free_times: list[str] = Field(default_factory=list, examples=[["weekday evenings"]])
