from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PremiumAllocationLeg:
    destination: str
    percentage: float
    amount: float
    reason: str


@dataclass(frozen=True)
class PremiumAllocationPlan:
    weekly_premium: float
    legs: list[PremiumAllocationLeg]
    summary: str
    warnings: list[str]

    def amount_for(self, destination: str) -> float:
        destination = destination.upper()
        return sum(leg.amount for leg in self.legs if leg.destination.upper() == destination)


@dataclass(frozen=True)
class AccountPremiumAllocation:
    account: object
    weekly_premium: float
    legs: list[PremiumAllocationLeg]
    basis: str

    def amount_for(self, destination: str) -> float:
        destination = destination.upper()
        return sum(leg.amount for leg in self.legs if leg.destination.upper() == destination)


def build_premium_allocation(metrics, roll_rows) -> PremiumAllocationPlan:
    # Use the recurring weekly premium (target sleeve on the nearest liquid weekly), not the displayed
    # reset trade — so the SATA routing/projection reflects the ongoing flywheel rather than collapsing
    # to $0 when you are already covered this week or the far-dated reset chain is too stale to price.
    weekly_premium = sum(
        getattr(row, "recurring_weekly_premium", 0.0)
        for row in roll_rows
        if getattr(row, "recurring_weekly_premium", 0.0) > 0
    )
    if weekly_premium <= 0:
        return PremiumAllocationPlan(
            weekly_premium=0.0,
            legs=[PremiumAllocationLeg("SATA", 1.0, 0.0, "No modeled premium is available to allocate.")],
            summary="No modeled premium is available to allocate.",
            warnings=[],
        )

    reserve_pct = _reserve_pct(metrics)
    sata_pct = _sata_anchor_pct(metrics)
    symbol_scores = {row.symbol: _symbol_score(row) for row in roll_rows if row.symbol in {"IBIT", "ASST"}}
    positive_scores = {symbol: score for symbol, score in symbol_scores.items() if score > 0}

    if not positive_scores:
        sata_pct = 1.0 - reserve_pct
        risky_pct = 0.0
    else:
        risky_pct = min(max(1.0 - sata_pct - reserve_pct, 0.0), _max_risky_reinvestment_pct(roll_rows))
        sata_pct = max(1.0 - reserve_pct - risky_pct, 0.0)

    legs = [PremiumAllocationLeg("SATA", sata_pct, weekly_premium * sata_pct, _sata_reason(metrics, sata_pct))]
    if reserve_pct > 0:
        legs.append(
            PremiumAllocationLeg(
                "Cash reserve",
                reserve_pct,
                weekly_premium * reserve_pct,
                "Hold back cash because imported cash plus pending activity is negative.",
            )
        )

    if risky_pct > 0 and positive_scores:
        score_total = sum(positive_scores.values())
        for symbol in ("IBIT", "ASST"):
            score = positive_scores.get(symbol, 0.0)
            pct = risky_pct * score / score_total if score_total else 0.0
            if pct <= 0:
                continue
            legs.append(
                PremiumAllocationLeg(
                    symbol,
                    pct,
                    weekly_premium * pct,
                    _symbol_reason(next(row for row in roll_rows if row.symbol == symbol)),
                )
            )

    legs = _normalize(legs, weekly_premium)
    summary = _summary(legs)
    return PremiumAllocationPlan(weekly_premium=weekly_premium, legs=legs, summary=summary, warnings=[])


def build_account_premium_allocations(plan: PremiumAllocationPlan, account_rows) -> list[AccountPremiumAllocation]:
    account_credits: dict[object, float] = {}
    account_basis: dict[object, set[str]] = {}
    for row in account_rows:
        credit = max(float(row.target_credit or 0.0), 0.0)
        if credit <= 0:
            continue
        account_credits[row.account] = account_credits.get(row.account, 0.0) + credit
        account_basis.setdefault(row.account, set()).add(row.basis)

    total_credit = sum(account_credits.values())
    if total_credit <= 0 or plan.weekly_premium <= 0:
        return []

    rows: list[AccountPremiumAllocation] = []
    for account, credit in sorted(account_credits.items(), key=lambda item: _account_label(item[0])):
        account_premium = plan.weekly_premium * credit / total_credit
        legs = [
            PremiumAllocationLeg(
                leg.destination,
                leg.percentage,
                account_premium * leg.percentage,
                leg.reason,
            )
            for leg in plan.legs
        ]
        basis = ", ".join(sorted(account_basis.get(account, {"latest account-level positions"})))
        rows.append(AccountPremiumAllocation(account, account_premium, legs, basis))
    return rows


def _sata_anchor_pct(metrics) -> float:
    strategy_value = max(float(metrics.true_strategy_value or metrics.total_account_value or 0.0), 1.0)
    sata_ratio = float(metrics.sata_value or 0.0) / strategy_value
    if sata_ratio < 0.05:
        return 0.80
    if sata_ratio < 0.10:
        return 0.70
    if sata_ratio < 0.15:
        return 0.60
    return 0.50


def _reserve_pct(metrics) -> float:
    liquid_cash = float(metrics.cash_value or 0.0) + float(metrics.pending_activity or 0.0)
    return 0.10 if liquid_cash < 0 else 0.0


def _max_risky_reinvestment_pct(roll_rows) -> float:
    if any(row.rsi_14 is not None and row.rsi_14 <= 35 for row in roll_rows):
        return 0.35
    if any(row.trend_state in {"bullish breakout", "bullish trend"} for row in roll_rows):
        return 0.30
    return 0.25


def _symbol_score(row) -> float:
    score = 0.0
    rsi = row.rsi_14
    if rsi is not None and rsi <= 35:
        score += 4.0
    elif rsi is not None and rsi >= 70:
        score -= 4.0

    if row.trend_state in {"bullish breakout", "bullish trend"}:
        score += 3.0
    elif row.trend_state == "neutral/chop":
        score += 1.0
    elif row.trend_state == "bearish" and rsi is not None and rsi > 35:
        score -= 1.0

    target_covered = row.posture.coverage_pct
    current_covered = row.current_covered_pct / 100 if row.current_covered_pct > 1 else row.current_covered_pct
    if current_covered > target_covered + 0.20 and (rsi is None or rsi < 70):
        score += 1.0
    return max(score, 0.0)


def _sata_reason(metrics, sata_pct: float) -> str:
    strategy_value = max(float(metrics.true_strategy_value or metrics.total_account_value or 0.0), 1.0)
    sata_ratio = float(metrics.sata_value or 0.0) / strategy_value
    if sata_ratio < 0.05:
        return "SATA is still below a 5% income-sleeve anchor, so most premium keeps compounding there."
    if sata_ratio < 0.15:
        return "SATA is below the long-run income-sleeve target, so it remains the main premium destination."
    if sata_pct >= 1.0:
        return "No attractive upside reinvestment signal is present, so premium stays in SATA."
    return "SATA remains the income anchor while some premium is reinvested into higher-upside holdings."


def _symbol_reason(row) -> str:
    if row.rsi_14 is not None and row.rsi_14 <= 35:
        return "Oversold setup; reinvest a slice to preserve upside after selling calls."
    if row.trend_state in {"bullish breakout", "bullish trend"}:
        return "Constructive trend; reinvest a slice into the underlying for upside participation."
    if row.current_covered_pct > row.posture.coverage_pct * 100 + 20:
        return "Current calls are above target coverage; adding shares helps restore uncapped upside."
    return "Neutral setup with some room for upside reinvestment."


def _normalize(legs: list[PremiumAllocationLeg], weekly_premium: float) -> list[PremiumAllocationLeg]:
    total_pct = sum(leg.percentage for leg in legs)
    if total_pct <= 0:
        return legs
    normalized = [
        PremiumAllocationLeg(
            leg.destination,
            leg.percentage / total_pct,
            weekly_premium * (leg.percentage / total_pct),
            leg.reason,
        )
        for leg in legs
    ]
    return sorted(normalized, key=lambda leg: (leg.destination != "SATA", leg.destination))


def _summary(legs: list[PremiumAllocationLeg]) -> str:
    return ", ".join(f"{leg.destination} {leg.percentage:.0%}" for leg in legs)


def _account_label(account: object) -> str:
    return str(getattr(account, "label", account))
