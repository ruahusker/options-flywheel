from __future__ import annotations

import base64
import csv
from datetime import datetime
from io import StringIO

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.journal import TradeJournalEntry
from app.models.market_data import PriceHistory
from app.models.options import OptionChainSnapshot, OptionContract
from app.models.portfolio import CashPosition, Holding, OptionPosition, PortfolioSnapshot
from app.routers.common import templates
from app.services.account_names import canonical_account, canonical_account_name
from app.services.fidelity_history_parser import FidelityAccountHistoryCsvParser
from app.services.fidelity_parser import FidelityPositionsCsvParser


router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.get("")
def uploads_page(request: Request):
    return templates.TemplateResponse(request, "uploads.html", {})


@router.post("/preview")
async def preview_upload(
    request: Request,
    file_kind: str = Form(...),
    upload_file: UploadFile = File(...),
):
    raw = await upload_file.read()
    text = raw.decode("utf-8-sig")
    content_b64 = base64.b64encode(raw).decode("ascii")
    context = {
        "request": request,
        "file_kind": file_kind,
        "filename": upload_file.filename,
        "content_b64": content_b64,
        "warnings": [],
    }
    if file_kind in {"fidelity_positions", "fidelity_history"}:
        parser = FidelityPositionsCsvParser() if file_kind == "fidelity_positions" else FidelityAccountHistoryCsvParser()
        parsed = parser.parse_text(text)
        context.update(
            {
                "parsed": parsed,
                "preview_rows": parsed.holdings[:20],
                "preview_options": parsed.option_positions[:20],
                "warnings": parsed.diagnostics.warnings,
            }
        )
    else:
        rows = list(csv.DictReader(StringIO(text)))
        context.update({"generic_rows": rows[:25], "row_count": len(rows)})
    context.pop("request", None)
    return templates.TemplateResponse(request, "uploads.html", context)


@router.post("/import")
def import_upload(
    request: Request,
    file_kind: str = Form(...),
    filename: str = Form(...),
    content_b64: str = Form(...),
    db: Session = Depends(get_db),
):
    text = base64.b64decode(content_b64.encode("ascii")).decode("utf-8-sig")
    warnings: list[str] = []
    imported = 0
    journal_imported: int | None = None
    if file_kind in {"fidelity_positions", "fidelity_history"}:
        parser = FidelityPositionsCsvParser() if file_kind == "fidelity_positions" else FidelityAccountHistoryCsvParser()
        parsed = parser.parse_text(text)
        _import_parsed_portfolio(db, parsed, filename)
        warnings.extend(parsed.diagnostics.warnings)
        imported = parsed.diagnostics.rows_imported
        if isinstance(parser, FidelityAccountHistoryCsvParser):
            journal_imported = _import_history_journal_entries(db, parser.parse_journal_entries(text, filename))
    elif "option_chain" in file_kind:
        imported = _import_option_chain(db, text, file_kind)
    elif "ohlcv" in file_kind:
        imported = _import_ohlcv(db, text, file_kind)
    else:
        warnings.append("Generic portfolio preview is supported; import is not mapped to the portfolio snapshot schema.")
    db.commit()
    return templates.TemplateResponse(
        request,
        "uploads.html",
        {
            "imported": imported,
            "journal_imported": journal_imported,
            "warnings": warnings,
            "file_kind": file_kind,
            "filename": filename,
        },
    )


def _import_parsed_portfolio(db: Session, parsed, filename: str) -> None:
    snapshot = PortfolioSnapshot(
        source_filename=filename,
        account_number=parsed.account_number,
        account_name=canonical_account_name(parsed.account_number, parsed.account_name),
        tax_status="tax_free",
        total_value=parsed.diagnostics.total_account_value,
    )
    db.add(snapshot)
    db.flush()
    for holding in parsed.holdings:
        account_number, account_name = canonical_account(holding.account_number, holding.account_name)
        db.add(
            Holding(
                snapshot_id=snapshot.id,
                account_number=account_number,
                account_name=account_name,
                symbol=holding.symbol,
                description=holding.description,
                quantity=holding.quantity,
                last_price=holding.last_price,
                current_value=holding.current_value,
                cost_basis_total=holding.cost_basis_total,
                average_cost_basis=holding.average_cost_basis,
                percent_of_account=holding.percent_of_account,
                asset_class=holding.asset_class,
                position_type=holding.position_type,
            )
        )
    for option in parsed.option_positions:
        account_number, account_name = canonical_account(option.account_number, option.account_name)
        db.add(
            OptionPosition(
                snapshot_id=snapshot.id,
                account_number=account_number,
                account_name=account_name,
                raw_symbol=option.raw_symbol,
                normalized_symbol=option.normalized_symbol,
                underlying=option.underlying,
                expiration=option.expiration,
                option_type=option.option_type,
                strike=option.strike,
                side=option.side,
                contracts=option.contracts,
                quantity=option.quantity,
                last_price=option.last_price,
                current_value=option.current_value,
                average_cost_basis=option.average_cost_basis,
                description=option.description,
            )
        )
    for cash in parsed.cash_positions:
        account_number, account_name = canonical_account(cash.account_number, cash.account_name)
        db.add(
            CashPosition(
                snapshot_id=snapshot.id,
                account_number=account_number,
                account_name=account_name,
                symbol=cash.symbol,
                description=cash.description,
                current_value=cash.current_value,
            )
        )


def _import_history_journal_entries(db: Session, entries) -> int:
    imported = 0
    for item in entries:
        account_number, account_name = canonical_account(item.account_number, item.account_name)
        entry = TradeJournalEntry(
            created_at=item.created_at,
            account_number=account_number,
            account_name=account_name,
            ticker=item.ticker,
            strategy=item.strategy,
            action=item.action,
            contracts=item.contracts,
            strike=item.strike,
            expiration=item.expiration,
            credit_debit=item.credit_debit,
            sata_contribution=item.sata_contribution,
            notes=item.notes,
        )
        existing = db.execute(
            select(TradeJournalEntry).where(
                TradeJournalEntry.created_at == entry.created_at,
                TradeJournalEntry.account_number == entry.account_number,
                TradeJournalEntry.ticker == entry.ticker,
                TradeJournalEntry.action == entry.action,
                TradeJournalEntry.credit_debit == entry.credit_debit,
                TradeJournalEntry.notes == entry.notes,
            )
        ).scalars().first()
        if existing:
            continue
        db.add(entry)
        imported += 1
    return imported


def _import_option_chain(db: Session, text: str, file_kind: str) -> int:
    rows = list(csv.DictReader(StringIO(text)))
    grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        underlying = (row.get("underlying") or ("IBIT" if "ibit" in file_kind else "ASST")).upper()
        expiration = row["expiration"]
        grouped.setdefault((underlying, expiration), []).append(row)
    imported = 0
    for (underlying, expiration_text), group in grouped.items():
        expiration = datetime.fromisoformat(expiration_text).date()
        snapshot = OptionChainSnapshot(provider="uploaded_csv", underlying=underlying, expiration=expiration, is_stale=False, market_status="uploaded")
        db.add(snapshot)
        db.flush()
        for row in group:
            bid = _float(row.get("bid"))
            ask = _float(row.get("ask"))
            mid = _float(row.get("mid")) or ((bid + ask) / 2 if bid is not None and ask is not None else None)
            db.add(
                OptionContract(
                    chain_snapshot_id=snapshot.id,
                    underlying=underlying,
                    expiration=expiration,
                    option_type=row["option_type"],
                    strike=float(row["strike"]),
                    bid=bid,
                    ask=ask,
                    mid=mid,
                    last=_float(row.get("last")),
                    volume=_int(row.get("volume")),
                    open_interest=_int(row.get("open_interest")),
                    implied_volatility=_float(row.get("implied_volatility")),
                    delta=_float(row.get("delta")),
                    gamma=_float(row.get("gamma")),
                    theta=_float(row.get("theta")),
                    vega=_float(row.get("vega")),
                    dte=_int(row.get("dte")),
                    provider_symbol=row.get("provider_symbol"),
                    liquidity_score=_float(row.get("liquidity_score")),
                )
            )
            imported += 1
    return imported


def _import_ohlcv(db: Session, text: str, file_kind: str) -> int:
    symbol = "IBIT" if "ibit" in file_kind else "ASST"
    imported = 0
    for row in csv.DictReader(StringIO(text)):
        db.add(
            PriceHistory(
                symbol=(row.get("symbol") or symbol).upper(),
                date_time=datetime.fromisoformat(row["date_time"]),
                interval=row.get("interval") or "1d",
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=_int(row.get("volume")),
                provider="uploaded_csv",
            )
        )
        imported += 1
    return imported


def _float(value: str | None) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return float(value)


def _int(value: str | None) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(float(value))
