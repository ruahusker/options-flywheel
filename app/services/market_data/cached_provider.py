from __future__ import annotations

from datetime import date, datetime

from app.database import SessionLocal
from app.models.market_data import MarketDataCache
from app.schemas.market_data import Bar, MarketStatus, OptionContractSchema, OptionContractSnapshot, Quote
from app.services.market_data.base import MarketDataProvider


# Payload kinds stored in MarketDataCache.
KIND_QUOTE = "quote"
KIND_HISTORY = "history"
KIND_EXPIRATIONS = "expirations"
KIND_CHAIN = "chain"
KIND_STATUS = "status"


def latest_refresh_at() -> datetime | None:
    """Most recent market-data refresh timestamp across all cached rows (for the 'as of' indicator)."""
    with SessionLocal() as db:
        row = (
            db.query(MarketDataCache.refreshed_at)
            .order_by(MarketDataCache.refreshed_at.desc())
            .first()
        )
        return row[0] if row else None


class CachedProvider(MarketDataProvider):
    """Serves market data from MarketDataCache instead of calling a live provider.

    Every web request and every precompute/upload rebuild goes through this, so the only component
    that ever touches the live provider (Tradier) is the scheduled refresh job. Missing cache rows
    degrade gracefully: empty lists, or a stale Quote/MarketStatus carrying a 'refresh pending'
    warning, so pages render instead of crashing on a cold cache.
    """

    name = "cached"

    def _read(self, symbol: str, kind: str, key: str = "") -> str | None:
        with SessionLocal() as db:
            row = (
                db.query(MarketDataCache.payload_json)
                .filter(
                    MarketDataCache.symbol == symbol.upper(),
                    MarketDataCache.kind == kind,
                    MarketDataCache.key == key,
                )
                .one_or_none()
            )
            return row[0] if row else None

    def get_quote(self, symbol: str) -> Quote:
        payload = self._read(symbol, KIND_QUOTE)
        if payload is None:
            return Quote(
                symbol=symbol.upper(),
                price=None,
                timestamp=datetime.utcnow(),
                provider=self.name,
                market_status="unknown",
                is_stale=True,
                warnings=["Market data refresh pending; no cached quote yet."],
            )
        return Quote.model_validate_json(payload)

    def get_price_history(self, symbol: str, lookback_days: int, interval: str) -> list[Bar]:
        payload = self._read(symbol, KIND_HISTORY)
        if payload is None:
            return []
        bars = _validate_list(payload, Bar)
        return bars[-lookback_days:] if lookback_days and len(bars) > lookback_days else bars

    def get_option_expirations(self, symbol: str) -> list[date]:
        payload = self._read(symbol, KIND_EXPIRATIONS)
        if payload is None:
            return []
        import json

        return [date.fromisoformat(item) for item in json.loads(payload)]

    def get_option_chain(self, symbol: str, expiration: date) -> list[OptionContractSchema]:
        payload = self._read(symbol, KIND_CHAIN, key=expiration.isoformat())
        if payload is None:
            return []
        return _validate_list(payload, OptionContractSchema)

    def get_option_snapshot(self, symbol: str, option_symbol: str) -> OptionContractSnapshot:
        return OptionContractSnapshot(
            symbol=symbol.upper(),
            option_symbol=option_symbol,
            contract=None,
            timestamp=datetime.utcnow(),
            provider=self.name,
            market_status="unknown",
            is_stale=True,
            warnings=["Option snapshots are not cached; use the chain view."],
        )

    def get_market_status(self) -> MarketStatus:
        payload = self._read("__market__", KIND_STATUS)
        if payload is None:
            return MarketStatus(
                provider=self.name,
                status="unknown",
                timestamp=datetime.utcnow(),
                warnings=["Market data refresh pending; no cached market status yet."],
            )
        return MarketStatus.model_validate_json(payload)


def _validate_list(payload: str, model) -> list:
    import json

    return [model.model_validate(item) for item in json.loads(payload)]
