from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.common import templates
from app.services import precompute
from app.services.roll_decision import action_label


router = APIRouter(prefix="/roll", tags=["roll"])


@router.get("")
def roll_page(request: Request, db: Session = Depends(get_db)):
    # Renders the precomputed payload (built on the schedule / after uploads); no live fetch here.
    payload = precompute.load_or_build("roll", db)
    return templates.TemplateResponse(request, "roll.html", {**payload, "action_label": action_label})
