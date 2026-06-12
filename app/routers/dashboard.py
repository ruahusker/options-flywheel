from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.common import templates
from app.services import precompute


router = APIRouter()


@router.get("/portfolio")
def dashboard(request: Request, db: Session = Depends(get_db)):
    payload = precompute.load_or_build("portfolio", db)
    return templates.TemplateResponse(request, "portfolio.html", payload)
