from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.common import templates
from app.services import precompute
from app.services.market_data import get_provider


router = APIRouter(prefix="/monte-carlo", tags=["monte-carlo"])


@router.get("")
def monte_carlo_page(request: Request, db: Session = Depends(get_db)):
    payload = precompute.load_or_build("monte_carlo", db)
    return templates.TemplateResponse(request, "monte_carlo.html", payload)


@router.post("")
def monte_carlo_post(
    request: Request,
    paths: int = Form(5000),
    years: int = Form(5),
    ibit_vol: float = Form(0.65),
    asst_vol: float = Form(0.85),
    drift: float = Form(0.08),
    correlation: float = Form(0.55),
    annual_premium_rate: float = Form(0.12),
    premium_variability: float = Form(0.35),
    optioned_pct: float = Form(0.35),
    db: Session = Depends(get_db),
):
    payload = precompute.build_monte_carlo(
        db,
        get_provider(),
        paths=paths,
        years=years,
        optioned_pct=optioned_pct,
        ibit_vol=ibit_vol,
        asst_vol=asst_vol,
        drift=drift,
        correlation=correlation,
        annual_premium_rate=annual_premium_rate,
        premium_variability=premium_variability,
    )
    return templates.TemplateResponse(request, "monte_carlo.html", payload)
