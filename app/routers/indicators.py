from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.common import templates
from app.services import precompute


router = APIRouter(prefix="/indicators", tags=["indicators"])


@router.get("")
def indicators_page(request: Request, db: Session = Depends(get_db)):
    payload = precompute.load_or_build("indicators", db)
    return templates.TemplateResponse(request, "indicators.html", payload)
