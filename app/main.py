from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.routers import backtest, dashboard, indicators, journal, live_data, monte_carlo, optimizer, performance, roll, scenarios, settings as settings_router, situation, uploads, week


app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
init_db()


@app.on_event("startup")
def on_startup() -> None:
    init_db()


app.include_router(week.router)
app.include_router(dashboard.router)
app.include_router(roll.router)
app.include_router(situation.router)
app.include_router(backtest.router)
app.include_router(uploads.router)
app.include_router(live_data.router)
app.include_router(optimizer.router)
app.include_router(indicators.router)
app.include_router(scenarios.router)
app.include_router(monte_carlo.router)
app.include_router(journal.router)
app.include_router(performance.router)
app.include_router(settings_router.router)
