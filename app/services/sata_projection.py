from __future__ import annotations

from dataclasses import dataclass

# SATA is a $100-par variable-rate preferred stock currently paying ~13%. Unlike a
# high-distribution ETF, the dividend is genuine cash income (not return-of-capital) and the
# price sits near par, so a flat ~$100 assumed price is reasonable. The realistic risks are:
#   * SATA pays dividends DAILY (the issuer's stated cadence), so daily compounding/DRIP is the
#     correct model here — not the quarterly cadence of a conventional preferred;
#   * the coupon is *variable* (13% is the current reset, can step), modeled via rate_path;
#   * it is callable at par (caps appreciation) and dividends could be deferred (credit risk);
#   * distributions may be taxable depending on the account (tax_rate).
_PAYMENTS_PER_YEAR = {"daily": 365, "business_day": 250, "weekly": 52, "monthly": 12, "quarterly": 4}


@dataclass(frozen=True)
class SATAProjection:
    years: int
    ending_value: float
    total_contributions: float
    dividend_growth: float
    estimated_shares: float
    annual_income_at_rate: float


def _rate_for_year(year_index: int, annual_rate: float, rate_path: dict[int, float] | None) -> float:
    if not rate_path:
        return annual_rate
    return float(rate_path.get(year_index, annual_rate))


def project_sata_value(
    initial_value: float,
    weekly_contribution: float,
    years: int,
    annual_rate: float = 0.13,
    drip_enabled: bool = True,
    compounding_mode: str = "daily",
    assumed_price: float = 100.0,
    business_day_payments: bool = False,
    rate_path: dict[int, float] | None = None,
    tax_rate: float = 0.0,
) -> SATAProjection:
    days = int(365 * years)
    value = float(initial_value or 0.0)
    total_contributions = 0.0
    contribution_interval = 7

    if not drip_enabled:
        # Dividends are taken as cash rather than reinvested, so principal only grows by contributions.
        total_contributions = weekly_contribution * 52 * years
        ending = value + total_contributions
        return SATAProjection(
            years=years,
            ending_value=ending,
            total_contributions=total_contributions,
            dividend_growth=0.0,
            estimated_shares=ending / assumed_price if assumed_price else 0.0,
            annual_income_at_rate=ending * annual_rate,
        )

    mode = "business_day" if business_day_payments else compounding_mode
    payments_per_year = _PAYMENTS_PER_YEAR.get(mode, 4)
    payment_interval = max(1, round(365 / payments_per_year))
    tax_keep = max(0.0, 1.0 - float(tax_rate or 0.0))

    for day in range(1, days + 1):
        if day % contribution_interval == 0:
            value += weekly_contribution
            total_contributions += weekly_contribution
        if day % payment_interval == 0:
            year_index = (day - 1) // 365
            period_rate = _rate_for_year(year_index, annual_rate, rate_path) / payments_per_year
            dividend = value * period_rate * tax_keep
            value += dividend  # DRIP: reinvested at ~par

    dividend_growth = value - float(initial_value or 0.0) - total_contributions
    return SATAProjection(
        years=years,
        ending_value=value,
        total_contributions=total_contributions,
        dividend_growth=dividend_growth,
        estimated_shares=value / assumed_price if assumed_price else 0.0,
        annual_income_at_rate=value * _rate_for_year(max(years - 1, 0), annual_rate, rate_path),
    )


def project_multiple_horizons(
    initial_value: float,
    weekly_contribution: float,
    annual_rate: float = 0.13,
    drip_enabled: bool = True,
    assumed_price: float = 100.0,
    compounding_mode: str = "daily",
    rate_path: dict[int, float] | None = None,
    tax_rate: float = 0.0,
) -> list[SATAProjection]:
    return [
        project_sata_value(
            initial_value,
            weekly_contribution,
            years,
            annual_rate,
            drip_enabled,
            compounding_mode,
            assumed_price,
            rate_path=rate_path,
            tax_rate=tax_rate,
        )
        for years in (1, 3, 5)
    ]
