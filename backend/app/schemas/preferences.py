from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.enums import Treatment

TimePref = Literal["morning", "afternoon", "evening", "weekend", "any"]
# How Lana says a time preference out loud: "mornings, got it", "no mornings".
TIME_PLURAL = {"morning": "mornings", "afternoon": "afternoons", "evening": "evenings", "weekend": "weekends"}


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
    # When the patient wants their appointments. "any" means they didn't say.
    time_pref: TimePref = "any"
    # Short plain notes of anything else the patient said that matters, in their order.
    notes: list[str] = Field(default_factory=list, examples=[["nervous about pain", "wants Spanish-speaking staff"]])
    # A place outside the DFW city list, as the patient said it ("San Francisco"). The demo
    # clinics are placed near it; zip_code then stays the default.
    place: str | None = None
