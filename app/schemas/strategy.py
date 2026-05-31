from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class CandidateSchema(BaseModel):
    symbol: str
    action: str
    contracts: int
    expiration: date | None = None
    strike: float | None = None
    option_type: str | None = None
    side: str | None = None
    delta: float | None = None
    expected_credit: float = 0.0
    total_score: float = 0.0
    reason: str = ""
    warnings: list[str] = Field(default_factory=list)
