from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.common import get_sata_settings, templates


router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
def settings_page(request: Request, db: Session = Depends(get_db)):
    sata_settings = get_sata_settings(db)
    return templates.TemplateResponse(request, "settings.html", {"settings": sata_settings})


@router.post("")
def update_settings(
    request: Request,
    annual_dividend_rate: float = Form(0.13),
    compounding_mode: str = Form("daily"),
    drip_enabled: bool = Form(False),
    business_day_payments: bool = Form(False),
    assumed_price: float = Form(100.0),
    tax_rate: float = Form(0.0),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    sata_settings = get_sata_settings(db)
    sata_settings.annual_dividend_rate = annual_dividend_rate
    sata_settings.compounding_mode = compounding_mode
    sata_settings.drip_enabled = drip_enabled
    sata_settings.business_day_payments = business_day_payments
    sata_settings.assumed_price = assumed_price
    sata_settings.tax_rate = tax_rate
    sata_settings.notes = notes
    db.commit()
    db.refresh(sata_settings)
    return templates.TemplateResponse(request, "settings.html", {"settings": sata_settings, "message": "Settings saved."})
