from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from app.config import settings
from app.services import minimax_client


@dataclass
class SituationBrief:
    provider: str
    model: str | None
    used_ai: bool
    text: str
    warnings: list[str]


@dataclass
class SituationAnswer:
    provider: str
    model: str | None
    used_ai: bool
    question: str
    text: str
    warnings: list[str]


def generate_situation_brief(context: dict[str, Any]) -> SituationBrief:
    if not minimax_client.minimax_configured():
        return SituationBrief(
            provider="deterministic_fallback",
            model=None,
            used_ai=False,
            text=_fallback_brief(context),
            warnings=["MINIMAX_API_KEY is not configured, so this used the local deterministic fallback."],
        )

    system = (
        "You are a portfolio and options strategy analyst inside Options Flywheel. "
        "Use only the supplied JSON context. Be concrete, numerical, and practical. "
        "Do not invent contracts, fills, prices, Greeks, account balances, or historical backtests. "
        "When discussing chance of profit, use the supplied profit_likelihood_proxy (probability the underlying "
        "finishes above breakeven) and assignment_probability_proxy (risk-neutral N(d2)); call them proxies. "
        "Treat assignment on a covered call as the max-profit outcome, not a loss. "
        "Focus on preserving high upside first, then premium income that can be routed to the SATA sleeve. "
        "Do not include hidden reasoning, scratchpad text, or <think> blocks."
    )
    user = (
        "Write a detailed current-situation brief for the account owner. "
        "The owner wants high upside participation, but also wants option premiums to compound into SATA. "
        "Explain current portfolio exposure, option coverage, short-option mark-to-market liability, net sidecar value, "
        "and how the current setup aligns or conflicts with the high-upside goal. "
        "Analyze the current technical indicators in the context JSON before comparing sleeves. "
        "Explicitly compare 25%, 35%, and 50% covered-call sleeves using sleeve_comparison. "
        "Explain whether 25% or 50% is more appropriate right now for the high-upside goal, and why. "
        "Include the current recommended trades with exact size, strike, expiration, delta, expected credit, and "
        "delta-derived likelihood proxy. If the current recommendation is skip/add nothing, explain why and then "
        "separately describe the best target-size setup if the sleeve were reset to the target. "
        "If assigned, use assignment_reentry_plan to explain the cash-secured put re-entry plan, including size, "
        "put delta band, collateral, and how it affects upside participation while in cash. "
        "If a target_setup action is skip trade, do not infer or estimate a strike, delta, expiration, or credit; "
        "write that the current chain did not provide a qualifying target setup for that symbol. "
        "Use the phrase 'best-chance delta band' for the target delta range, but do not claim a backtest unless supplied. "
        "Discuss how premiums going to SATA change the sidecar projection, while separating that from the IBIT/ASST upside tradeoff. "
        "Do not print account numbers. "
        "Use these sections: Current read, Technical read, 25% vs 50% covered, Premiums to SATA, "
        "Current recommended trades, Assignment and short-put re-entry, Best-chance delta band, Main risks, Next actions. "
        "Keep it detailed but readable, under 1200 words, and complete every section. End with 'Brief complete.'"
        f"\n\nContext JSON:\n{json.dumps(context, sort_keys=True, default=str)}"
    )
    try:
        text = minimax_client.call_minimax_chat(system, user, max_completion_tokens=4200)
        return SituationBrief(
            provider="minimax_chat_completions",
            model=settings.minimax_model,
            used_ai=True,
            text=text,
            warnings=[],
        )
    except Exception as exc:
        return SituationBrief(
            provider="deterministic_fallback",
            model=settings.minimax_model,
            used_ai=False,
            text=_fallback_brief(context),
            warnings=[f"MiniMax situation brief failed: {exc}", "Returned local deterministic fallback instead."],
        )


def answer_situation_question(context: dict[str, Any], question: str) -> SituationAnswer:
    clean_question = question.strip()
    if not clean_question:
        return SituationAnswer(
            provider="local",
            model=None,
            used_ai=False,
            question=question,
            text="Ask a question about the current brief or portfolio context.",
            warnings=[],
        )
    if not minimax_client.minimax_configured():
        return SituationAnswer(
            provider="deterministic_fallback",
            model=None,
            used_ai=False,
            question=clean_question,
            text=_fallback_answer(context, clean_question),
            warnings=["MINIMAX_API_KEY is not configured, so this used the local deterministic fallback."],
        )

    system = (
        "You answer follow-up questions about an Options Flywheel situation brief. "
        "Use only the supplied JSON context and the user's question. Be direct, numerical, and practical. "
        "Do not invent contracts, fills, prices, Greeks, account numbers, or backtests. "
        "If the data does not support an answer, say exactly what is missing. "
        "Use delta only as a likelihood proxy, not as a guarantee. Do not include hidden reasoning or <think> blocks."
    )
    user = (
        f"Question: {clean_question}\n\n"
        "Answer in 2-6 concise paragraphs. If useful, include a short bullet list. "
        "Tie the answer back to the high-upside goal and premiums-to-SATA plan when relevant."
        " If the question asks about 25% vs 50% coverage, use sleeve_comparison. "
        "If the question asks about assignment or puts, use assignment_reentry_plan."
        f"\n\nContext JSON:\n{json.dumps(context, sort_keys=True, default=str)}"
    )
    try:
        text = minimax_client.call_minimax_chat(system, user, max_completion_tokens=1800)
        return SituationAnswer(
            provider="minimax_chat_completions",
            model=settings.minimax_model,
            used_ai=True,
            question=clean_question,
            text=text,
            warnings=[],
        )
    except Exception as exc:
        return SituationAnswer(
            provider="deterministic_fallback",
            model=settings.minimax_model,
            used_ai=False,
            question=clean_question,
            text=_fallback_answer(context, clean_question),
            warnings=[f"MiniMax follow-up failed: {exc}", "Returned local deterministic fallback instead."],
        )


def candidate_dict(candidate) -> dict[str, Any]:
    data = asdict(candidate)
    if data.get("expiration") is not None:
        data["expiration"] = str(data["expiration"])
    data["profit_likelihood_proxy"] = _profit_likelihood_proxy(data)
    return data


def indicator_dict(indicator) -> dict[str, Any] | None:
    if indicator is None:
        return None
    data = asdict(indicator)
    data["calculated_at"] = str(data["calculated_at"])
    return data


def quote_dict(quote) -> dict[str, Any] | None:
    return quote.model_dump(mode="json") if quote is not None else None


def _profit_likelihood_proxy(candidate: dict[str, Any]) -> float | None:
    """Probability the trade is profitable vs. holding cash/stock.

    For a covered call or cash-secured put the position profits whenever the underlying
    finishes above breakeven (cost basis minus premium / strike minus premium) — and being
    *assigned* is the max-profit outcome, not a loss. So this is P(price > breakeven), not
    1 - P(assignment). We use the model's breakeven-based `profit_probability` when available
    and only fall back to the cruder delta complement if it is missing.
    """
    if candidate.get("action") == "skip trade":
        return None
    profit_probability = candidate.get("profit_probability")
    if profit_probability is not None:
        return max(0.0, min(1.0, float(profit_probability)))
    assignment = candidate.get("assignment_probability_proxy")
    if assignment is None:
        return None
    return max(0.0, min(1.0, 1.0 - float(assignment)))


def _fallback_brief(context: dict[str, Any]) -> str:
    portfolio = context["portfolio"]
    journal = context["journal_summary"]
    lines = [
        "Current read",
        (
            f"Account value is ${portfolio['true_strategy_value']:,.2f}. IBIT is "
            f"${portfolio['values_by_symbol'].get('IBIT', 0):,.2f}, ASST is "
            f"${portfolio['values_by_symbol'].get('ASST', 0):,.2f}, and SATA is "
            f"${portfolio['sata_value']:,.2f}. The current optioned sleeve is "
            f"{context['strategy']['actual_optioned_pct']:.1%}."
        ),
        "",
        "High-upside fit",
        (
            "The current sleeve is much more optioned than a high-upside target if the target is "
            f"{context['strategy']['target_optioned_pct']:.0%} optioned with "
            f"{context['strategy']['min_untouched_pct']:.0%} minimum untouched. That makes the current "
            "setup more income/cap oriented than upside-first."
        ),
        "",
        "Premiums to SATA",
        (
            f"The imported journal shows ${journal['net_option_premium']:,.2f} net option premium and "
            f"${journal['sata_contributions']:,.2f} of SATA contributions. Routing premium to SATA can build "
            "income-side compounding, but it does not remove the upside cap created by covered calls."
        ),
        "",
        "Current recommended trades",
    ]
    for row in context["trade_rows"]:
        current = row["current_recommendation"]
        setup = row["target_setup"]
        lines.append(
            f"{row['symbol']}: current add-on action is {current['action']} for "
            f"{current['contracts']} contract(s). Target reset setup is {setup['action']} "
            f"{setup['contracts']} contract(s), strike {setup['strike']}, expiration {setup['expiration']}, "
            f"delta {setup['delta']}, expected credit ${setup['expected_credit']:,.2f}."
        )
    lines.extend(
        [
            "",
            "Best-chance delta band",
            (
                f"The configured best-chance delta band is {context['strategy']['call_delta_min']:.0%}-"
                f"{context['strategy']['call_delta_max']:.0%} absolute delta for calls. Lower delta usually "
                "means a higher probability proxy of expiring out of the money, but lower premium."
            ),
            "",
            "Assignment and short-put re-entry",
            "If shares are assigned, the wheel plan is to use cash-secured puts for re-entry rather than immediately chasing shares. The put re-entry band in the context should be checked against live collateral and liquidity.",
            "",
            "Main risks",
            "Covered calls can cap much of the upside if IBIT or ASST rallies hard. ASST also deserves extra liquidity attention.",
            "",
            "Next actions",
            "Check the live chain, confirm bid/ask width and open interest, and avoid adding calls while current exposure already exceeds the target sleeve.",
            "Brief complete.",
        ]
    )
    return "\n".join(lines)


def _fallback_answer(context: dict[str, Any], question: str) -> str:
    portfolio = context["portfolio"]
    strategy = context["strategy"]
    question_lower = question.lower()
    if "sata" in question_lower:
        projection = context["sata_projection_if_target_setup_credit_repeats"][0]
        return (
            f"SATA is currently ${portfolio['sata_value']:,.2f}. If the target setup credit repeats, the one-year "
            f"projection is ${projection['ending_value']:,.2f}, with ${projection['total_contributions']:,.2f} "
            f"of contributions. The tradeoff is that premium routed to SATA does not remove the upside cap from "
            "covered calls on IBIT or ASST."
        )
    if "delta" in question_lower or "chance" in question_lower or "probability" in question_lower:
        return (
            f"The configured best-chance delta band is {strategy['best_chance_delta_band']}. In this app, "
            "assignment probability uses the risk-neutral N(d2), and profit likelihood is the modeled probability the "
            "underlying finishes above breakeven (cost basis minus premium for a covered call). Lower delta usually "
            "preserves more upside but pays less premium; on a covered call, being assigned is the best-case outcome."
        )
    return (
        f"The current optioned sleeve is {strategy['actual_optioned_pct']:.1%} versus a target of "
        f"{strategy['target_optioned_pct']:.0%}. That means the account is currently more capped than the "
        "high-upside goal implies. Current add-on trades should stay conservative unless live chain data changes."
    )
