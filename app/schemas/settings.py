from __future__ import annotations

from pydantic import BaseModel


class SATASettingsSchema(BaseModel):
    annual_dividend_rate: float = 0.13
    compounding_mode: str = "daily"
    drip_enabled: bool = True
    business_day_payments: bool = False
    assumed_price: float = 100.0
    tax_rate: float = 0.0
    notes: str | None = None
