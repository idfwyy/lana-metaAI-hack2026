from pydantic import BaseModel, Field, computed_field

from app.schemas.checklist import SourcedAnswer


class Concession(BaseModel):
    label: str
    amount: int = Field(ge=0, description="Dollars saved; 0 for non-cash items like retainers")
    evidence: SourcedAnswer[int | bool | str]


class NegotiationResult(BaseModel):
    clinic_id: str
    first_all_in: int = Field(ge=0)
    final_all_in: int = Field(ge=0)
    concessions: list[Concession] = Field(default_factory=list)
    refused: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def total_saved(self) -> int:
        return self.first_all_in - self.final_all_in
