from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class HoldingSchema(BaseModel):
    account_number: str | None = None
    account_name: str | None = None
    symbol: str
    description: str | None = None
    quantity: float | None = None
    last_price: float | None = None
    current_value: float | None = None
    percent_of_account: float | None = None
    cost_basis_total: float | None = None
    average_cost_basis: float | None = None
    position_type: str | None = None
    asset_class: str = "unknown"


class OptionPositionSchema(BaseModel):
    account_number: str | None = None
    account_name: str | None = None
    raw_symbol: str
    normalized_symbol: str
    underlying: str
    expiration: date
    option_type: str
    strike: float
    side: str
    contracts: int
    quantity: float
    last_price: float | None = None
    current_value: float | None = None
    average_cost_basis: float | None = None
    description: str | None = None


class CashPositionSchema(BaseModel):
    account_number: str | None = None
    account_name: str | None = None
    symbol: str
    description: str | None = None
    current_value: float | None = None
