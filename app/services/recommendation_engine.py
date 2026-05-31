from __future__ import annotations

from dataclasses import dataclass

from app.schemas.market_data import OptionContractSchema, Quote
from app.services.indicators import IndicatorResult
from app.services.strategy_optimizer import OptimizerSettings, StrategyCandidateResult, rank_candidates


@dataclass
class RecommendationResult:
    symbol: str
    quote: Quote | None
    best: StrategyCandidateResult
    alternatives: list[StrategyCandidateResult]
    warnings: list[str]


def generate_recommendation(
    symbol: str,
    shares: float,
    available_cash: float,
    quote: Quote,
    chain: list[OptionContractSchema],
    indicators: IndicatorResult | None,
    settings: OptimizerSettings,
    wheel_in_cash: bool = False,
    existing_short_call_contracts: int = 0,
    iv_rank: float | None = None,
) -> RecommendationResult:
    candidates = rank_candidates(
        symbol,
        shares,
        available_cash,
        quote,
        chain,
        indicators,
        settings,
        wheel_in_cash,
        existing_short_call_contracts,
        iv_rank=iv_rank,
    )
    best = candidates[0]
    warnings = list(best.warnings)
    if best.action == "skip trade":
        warnings.append("No trade is preferred to forcing a low-quality premium sale.")
    if best.option_type == "call" and best.delta is not None and abs(best.delta) >= 0.40:
        warnings.append("This materially reduces upside participation if the underlying rallies.")
    if quote.is_stale:
        warnings.append("Refresh quote and option chain before using this recommendation.")
    return RecommendationResult(symbol=symbol, quote=quote, best=best, alternatives=candidates[1:6], warnings=warnings)
