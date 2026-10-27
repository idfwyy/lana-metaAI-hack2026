from pydantic import BaseModel, Field

from app.schemas.clinic import Clinic
from app.schemas.negotiation import NegotiationResult
from app.schemas.quote import Quote


class Offer(BaseModel):
    """One ranked result. Order comes from ranker.py, never from the AI."""

    rank: int = Field(ge=1)
    clinic: Clinic
    quote: Quote
    negotiation: NegotiationResult | None = None
    score: float = Field(ge=0, le=1)
    tradeoffs: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
