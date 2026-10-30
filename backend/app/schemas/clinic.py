from pydantic import AwareDatetime, BaseModel, Field, model_validator


class Clinic(BaseModel):
    """Public clinic info. This is everything the agent may see."""

    id: str
    name: str
    distance_mi: float = Field(ge=0)
    rating: float = Field(ge=0, le=5)
    review_count: int = Field(ge=0)
    hours: str
    review_summary: str
    phone: str
    is_demo: bool
    voice_id: str | None = None


class PriceSheet(BaseModel):
    """Hidden demo-clinic pricing and concession rules. Only clinics/simulator.py may read this."""

    list_price: int = Field(ge=0)
    records_fee: int = Field(ge=0)
    consult_fee: int = Field(ge=0)
    monthly: int = Field(ge=0)
    down_payment: int = Field(ge=0)
    includes: list[str] = Field(description="What the treatment price covers")
    floor_price: int = Field(ge=0)
    will_price_match: bool
    will_waive_records: bool
    pay_in_full_discount_pct: float = Field(ge=0, le=100)
    will_add_retainers: bool
    open_slots: list[AwareDatetime]
    personality: str

    @model_validator(mode="after")
    def _floor_not_above_list(self) -> "PriceSheet":
        if self.floor_price > self.list_price:
            raise ValueError("floor_price cannot be above list_price")
        return self
