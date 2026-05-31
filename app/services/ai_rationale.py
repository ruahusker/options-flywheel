from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from app.config import settings
from app.services.kimi_client import KimiClientError, call_kimi_chat, kimi_configured
from app.services.minimax_client import MiniMaxClientError, call_minimax_chat, minimax_configured
from app.services.indicators import IndicatorResult
from app.services.risk_engine import DashboardMetrics
from app.services.strategy_optimizer import StrategyCandidateResult


@dataclass
class RationaleResult:
    symbol: str
    provider: str
    model: str | None
    text: str
    warnings: list[str]
    used_ai: bool


def generate_rationale(
    symbol: str,
    candidate: StrategyCandidateResult,
    alternatives: list[StrategyCandidateResult],
    indicators: IndicatorResult | None,
    metrics: DashboardMetrics,
    optioned_pct: float,
    objective: str,
) -> RationaleResult:
    context = _rationale_context(symbol, candidate, alternatives, indicators, metrics, optioned_pct, objective)
    if minimax_configured():
        try:
            text = _call_minimax(context)
            return RationaleResult(
                symbol=symbol,
                provider="minimax_chat_completions",
                model=settings.minimax_model,
                text=text,
                warnings=[],
                used_ai=True,
            )
        except MiniMaxClientError as exc:
            return RationaleResult(
                symbol=symbol,
                provider="deterministic_fallback",
                model=settings.minimax_model,
                text=_fallback_rationale(context),
                warnings=[f"MiniMax rationale failed: {exc}", "Returned local deterministic fallback instead."],
                used_ai=False,
            )

    if kimi_configured():
        try:
            text = _call_kimi(context)
            return RationaleResult(
                symbol=symbol,
                provider="kimi_code_chat_completions",
                model=settings.kimi_model,
                text=text,
                warnings=[],
                used_ai=True,
            )
        except KimiClientError as exc:
            return RationaleResult(
                symbol=symbol,
                provider="deterministic_fallback",
                model=settings.kimi_model,
                text=_fallback_rationale(context),
                warnings=[f"Kimi rationale failed: {exc}", "Returned local deterministic fallback instead."],
                used_ai=False,
            )

    if not settings.openai_api_key:
        return RationaleResult(
            symbol=symbol,
            provider="deterministic_fallback",
            model=None,
            text=_fallback_rationale(context),
            warnings=["OPENAI_API_KEY is not configured, so this explanation used the local deterministic fallback."],
            used_ai=False,
        )

    try:
        text = _call_openai(context)
        return RationaleResult(
            symbol=symbol,
            provider="openai_responses_api",
            model=settings.ai_rationale_model,
            text=text,
            warnings=[],
            used_ai=True,
        )
    except Exception as exc:
        return RationaleResult(
            symbol=symbol,
            provider="deterministic_fallback",
            model=settings.ai_rationale_model,
            text=_fallback_rationale(context),
            warnings=[f"AI rationale failed: {exc}", "Returned local deterministic fallback instead."],
            used_ai=False,
        )


def _rationale_context(
    symbol: str,
    candidate: StrategyCandidateResult,
    alternatives: list[StrategyCandidateResult],
    indicators: IndicatorResult | None,
    metrics: DashboardMetrics,
    optioned_pct: float,
    objective: str,
) -> dict[str, Any]:
    exposure = metrics.option_exposure.get(symbol, {})
    return {
        "symbol": symbol,
        "objective": objective,
        "optioned_pct": optioned_pct,
        "portfolio": {
            "shares": metrics.shares_by_symbol.get(symbol, 0.0),
            "position_value": metrics.values_by_symbol.get(symbol, 0.0),
            "sata_value": metrics.sata_value,
            "cash_value": metrics.cash_value,
            "pending_activity": metrics.pending_activity,
            "short_option_liability": metrics.short_option_liability,
            "long_option_value": metrics.long_option_value,
            "true_strategy_value": metrics.true_strategy_value,
            "net_sidecar_value": metrics.net_sidecar_value,
            "current_exposure": exposure,
        },
        "recommended_candidate": _candidate_dict(candidate),
        "alternatives": [_candidate_dict(item) for item in alternatives[:5]],
        "indicators": _indicator_dict(indicators),
        "required_risk_framing": [
            "Do not describe option premium as free money.",
            "Always account for marked-to-market short-option liability.",
            "Compare against buy-and-hold, especially straight-line heavy bull risk.",
            "If wheel sleeve is in cash or put phase, call out reduced upside participation.",
            "SATA yield and price are assumptions, not guarantees.",
        ],
    }


def _candidate_dict(candidate: StrategyCandidateResult) -> dict[str, Any]:
    data = asdict(candidate)
    if data.get("expiration") is not None:
        data["expiration"] = str(data["expiration"])
    return data


def _indicator_dict(indicators: IndicatorResult | None) -> dict[str, Any] | None:
    if indicators is None:
        return None
    data = asdict(indicators)
    data["calculated_at"] = str(data["calculated_at"])
    return data


def _call_openai(context: dict[str, Any]) -> str:
    system = (
        "You are a cautious options-modeling analyst for a local decision-support app. "
        "Explain the recommendation using only the JSON context. Do not invent prices, fills, "
        "tax facts, guarantees, or missing market data. This is not financial advice and not trade execution."
    )
    user = (
        "Generate a concise but useful rationale for the options recommendation. "
        "Use this exact structure with short sections: Recommendation, Why it ranked well, "
        "Why alternatives were weaker, Buy-and-hold risk, What would change the recommendation, Data cautions. "
        "Be explicit when straight-line heavy bull markets may make buy-and-hold outperform. "
        "Be explicit that premiums go to SATA but SATA is not risk-free. "
        f"\n\nContext JSON:\n{json.dumps(context, sort_keys=True)}"
    )
    payload = {
        "model": settings.ai_rationale_model,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    with httpx.Client(timeout=settings.ai_rationale_timeout_seconds) as client:
        response = client.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    return _extract_response_text(data).strip()


def _call_kimi(context: dict[str, Any]) -> str:
    system = (
        "You are a cautious options-modeling analyst inside a local decision-support app. "
        "Explain the recommendation using only the JSON context. Do not invent prices, strikes, fills, "
        "tax facts, guarantees, or missing market data. This is not financial advice and not trade execution. "
        "Do not include hidden reasoning, scratchpad text, or <think> blocks in the response."
    )
    user = (
        "Generate a concise but useful rationale for the options recommendation. "
        "Use this exact structure with short sections: Recommendation, Why it ranked well, "
        "Why alternatives were weaker, Likelihood-of-profit proxy, Buy-and-hold risk, "
        "What would change the recommendation, Data cautions. "
        "Treat probability/likelihood as a proxy derived from delta and scenario filters, not a guarantee. "
        "Be explicit when straight-line heavy bull markets may make buy-and-hold outperform. "
        "Be explicit that premiums go to SATA but SATA is not risk-free. "
        f"\n\nContext JSON:\n{json.dumps(context, sort_keys=True)}"
    )
    return call_kimi_chat(system, user, max_completion_tokens=1600)


def _call_minimax(context: dict[str, Any]) -> str:
    system = (
        "You are a cautious options-modeling analyst inside a local decision-support app. "
        "Explain the recommendation using only the JSON context. Do not invent prices, strikes, fills, "
        "tax facts, guarantees, or missing market data. This is not financial advice and not trade execution. "
        "Do not include hidden reasoning, scratchpad text, or <think> blocks in the response."
    )
    user = (
        "Generate a concise but useful rationale for the options recommendation. "
        "Use this exact structure with short sections: Recommendation, Why it ranked well, "
        "Why alternatives were weaker, Likelihood-of-profit proxy, Buy-and-hold risk, "
        "What would change the recommendation, Data cautions. "
        "Treat probability/likelihood as a proxy derived from delta and scenario filters, not a guarantee. "
        "Be explicit when straight-line heavy bull markets may make buy-and-hold outperform. "
        "Be explicit that premiums go to SATA but SATA is not risk-free. "
        f"\n\nContext JSON:\n{json.dumps(context, sort_keys=True, default=str)}"
    )
    return call_minimax_chat(system, user, max_completion_tokens=1600)


def _extract_response_text(data: dict[str, Any]) -> str:
    if data.get("output_text"):
        return str(data["output_text"])
    chunks: list[str] = []
    for output in data.get("output", []) or []:
        for content in output.get("content", []) or []:
            text = content.get("text")
            if text:
                chunks.append(str(text))
    if chunks:
        return "\n".join(chunks)
    raise ValueError("OpenAI response did not contain output text")


def _fallback_rationale(context: dict[str, Any]) -> str:
    rec = context["recommended_candidate"]
    indicators = context.get("indicators") or {}
    portfolio = context["portfolio"]
    alternatives = context.get("alternatives") or []
    warnings = rec.get("warnings") or []
    alt_summary = "No close alternatives were available."
    if alternatives:
        alt_summary = "; ".join(
            f"{item.get('action')} {item.get('strike') or ''} scored {item.get('total_score', 0):.1f}"
            for item in alternatives[:3]
        )

    lines = [
        f"Recommendation: {context['symbol']} is currently ranked as {rec.get('action')} with "
        f"{rec.get('contracts')} contract(s), strike {rec.get('strike')}, expiration {rec.get('expiration')}, "
        f"delta {rec.get('delta')}, and estimated credit ${rec.get('expected_credit', 0):,.2f}.",
        "",
        "Why it ranked well: the score balances premium, upside preservation, liquidity, trend alignment, "
        f"and scenario robustness. It scored {rec.get('total_score', 0):.1f}; premium yield annualized is "
        f"{rec.get('premium_yield_annualized', 0) * 100:.1f}% and assignment proxy is "
        f"{rec.get('assignment_probability_proxy', 0) * 100:.1f}%.",
        "",
        f"Why alternatives were weaker: {alt_summary}. The optimizer is designed to skip trades or reject "
        "wide-spread, low-liquidity, stale, or poor risk/reward options rather than chase the highest premium.",
        "",
        "Buy-and-hold risk: covered calls and wheel cash phases can underperform if IBIT or ASST rises in a "
        "straight line. The current strategy must be judged against buy-and-hold, not only against starting capital.",
        "",
        "What would change the recommendation: stronger breakout momentum, lower IV, wider spreads, stale chains, "
        "or heavy-bull underperformance would push the app toward lower-delta calls or skip trade. Weakening or "
        "overbought conditions can make a more aggressive call delta more acceptable.",
        "",
        "Data cautions: premiums are compensation for real risk. SATA contributions assume option premium is invested, "
        "but SATA yield and price are assumptions, not guarantees. Open short options remain marked to market.",
    ]
    if indicators:
        lines.insert(
            2,
            f"Technical context: trend state is {indicators.get('trend_state')}; RSI is "
            f"{_fmt(indicators.get('rsi_14'))}; recommendation bias is {indicators.get('recommendation_bias')}.",
        )
    if portfolio.get("current_exposure"):
        lines.insert(
            3,
            f"Exposure context: current optioned percentage is "
            f"{portfolio['current_exposure'].get('optioned_percentage', 0) * 100:.1f}% for this ticker.",
        )
    if warnings:
        lines.append("")
        lines.append("Warnings already attached to this candidate: " + "; ".join(warnings))
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.1f}"
    except Exception:
        return str(value)
