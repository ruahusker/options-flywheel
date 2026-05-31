from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.market_data import HistoricalOptionContract, OptionPriceBar, PriceHistory
from app.services.massive_backfill import (
    MassiveBackfillService,
    query_focused_contracts_for_bar_backfill,
    upsert_contracts,
    upsert_option_bars,
    upsert_stock_bars,
)
from app.services.massive_client import MassiveBar, MassiveOptionContract


def test_massive_backfill_upserts_contracts_and_bars():
    session = make_session()
    contract = MassiveOptionContract(
        provider_symbol="O:IBIT260605C00045000",
        underlying="IBIT",
        expiration=date(2026, 6, 5),
        option_type="call",
        strike=45.0,
        shares_per_contract=100,
        exercise_style="american",
    )

    inserted, updated = upsert_contracts(session, [contract], as_of=date(2026, 5, 30))
    session.commit()
    inserted_again, updated_again = upsert_contracts(session, [contract], as_of=date(2026, 5, 31))
    session.commit()

    stored_contract = session.query(HistoricalOptionContract).one()
    assert (inserted, updated) == (1, 0)
    assert (inserted_again, updated_again) == (0, 1)
    assert stored_contract.last_seen_as_of == date(2026, 5, 31)

    bar = MassiveBar(
        symbol=contract.provider_symbol,
        date_time=datetime(2026, 6, 1),
        open=1.0,
        high=1.2,
        low=0.9,
        close=1.1,
        volume=10,
        vwap=1.05,
        transactions=3,
    )
    option_inserted, option_updated = upsert_option_bars(session, stored_contract, [bar], interval="1d")
    option_inserted_again, option_updated_again = upsert_option_bars(session, stored_contract, [bar], interval="1d")
    session.commit()

    assert (option_inserted, option_updated) == (1, 0)
    assert (option_inserted_again, option_updated_again) == (0, 1)
    assert session.query(OptionPriceBar).one().close == 1.1


def test_contract_upsert_dedupes_duplicate_symbols_in_same_page():
    session = make_session()
    first = MassiveOptionContract("O:ASST260220C00001000", "ASST", date(2026, 2, 20), "call", 1.0, 100)
    duplicate = MassiveOptionContract("O:ASST260220C00001000", "ASST", date(2026, 2, 20), "call", 1.0, 2105)

    inserted, updated = upsert_contracts(session, [first, duplicate])
    session.commit()

    stored = session.query(HistoricalOptionContract).one()
    assert (inserted, updated) == (1, 0)
    assert stored.provider_symbol == "O:ASST260220C00001000"
    assert stored.shares_per_contract == 2105


def test_massive_backfill_upserts_underlying_price_history():
    session = make_session()
    bar = MassiveBar(
        symbol="IBIT",
        date_time=datetime(2026, 6, 1),
        open=44.0,
        high=46.0,
        low=43.5,
        close=45.0,
        volume=1000,
    )

    inserted, updated = upsert_stock_bars(session, [bar], interval="1d")
    inserted_again, updated_again = upsert_stock_bars(session, [bar], interval="1d")
    session.commit()

    assert (inserted, updated) == (1, 0)
    assert (inserted_again, updated_again) == (0, 1)
    assert session.query(PriceHistory).one().close == 45.0


def test_option_bar_backfill_limit_skips_already_attempted_contracts():
    session = make_session()
    first = MassiveOptionContract("O:IBIT260605C00045000", "IBIT", date(2026, 6, 5), "call", 45.0)
    second = MassiveOptionContract("O:IBIT260605C00046000", "IBIT", date(2026, 6, 5), "call", 46.0)
    upsert_contracts(session, [first, second])
    stored_first = (
        session.query(HistoricalOptionContract)
        .filter(HistoricalOptionContract.provider_symbol == first.provider_symbol)
        .one()
    )
    stored_first.bars_fetched_interval = "1d"
    stored_first.bars_fetched_through = date(2026, 6, 5)
    session.commit()

    fake_client = FakeMassiveClient()
    service = MassiveBackfillService(fake_client)

    service.backfill_option_bars(
        session,
        ["IBIT"],
        start=date(2026, 6, 1),
        end=date(2026, 6, 5),
        max_contracts=1,
    )

    assert fake_client.symbols == [second.provider_symbol]
    stored_second = (
        session.query(HistoricalOptionContract)
        .filter(HistoricalOptionContract.provider_symbol == second.provider_symbol)
        .one()
    )
    assert stored_second.bars_fetched_interval == "1d"
    assert stored_second.bars_fetched_through == date(2026, 6, 5)


def test_option_bar_backfill_appends_after_last_fetched_date():
    session = make_session()
    contract = MassiveOptionContract("O:IBIT260605C00045000", "IBIT", date(2026, 6, 5), "call", 45.0)
    upsert_contracts(session, [contract])
    stored_contract = session.query(HistoricalOptionContract).one()
    stored_contract.bars_fetched_interval = "1d"
    stored_contract.bars_fetched_through = date(2026, 6, 3)
    session.commit()

    fake_client = FakeMassiveClient()
    service = MassiveBackfillService(fake_client)

    service.backfill_option_bars(session, ["IBIT"], start=date(2026, 6, 1), end=date(2026, 6, 5))

    assert fake_client.ranges == [(contract.provider_symbol, date(2026, 6, 4), date(2026, 6, 5))]
    assert stored_contract.bars_fetched_through == date(2026, 6, 5)


def test_contract_backfill_resumes_from_latest_cached_expiration():
    session = make_session()
    existing = MassiveOptionContract("O:IBIT260605C00045000", "IBIT", date(2026, 6, 5), "call", 45.0)
    upsert_contracts(session, [existing])
    session.commit()

    fake_client = FakeContractClient()
    service = MassiveBackfillService(fake_client)

    service.backfill_contracts(session, ["IBIT"], start=date(2025, 6, 1), end=date(2026, 6, 30), include_active=False)

    assert fake_client.expiration_gtes == [date(2026, 6, 6)]


def test_underlying_backfill_resumes_near_latest_cached_bar():
    session = make_session()
    upsert_stock_bars(
        session,
        [
            MassiveBar(
                symbol="IBIT",
                date_time=datetime(2026, 6, 10),
                open=44.0,
                high=46.0,
                low=43.5,
                close=45.0,
            )
        ],
        interval="1d",
    )
    session.commit()

    fake_client = FakeUnderlyingClient()
    service = MassiveBackfillService(fake_client)

    service.backfill_underlying_bars(
        session,
        ["IBIT"],
        start=date(2025, 6, 1),
        end=date(2026, 6, 12),
        refresh_lookback_days=3,
    )

    assert fake_client.ranges == [("IBIT", date(2026, 6, 7), date(2026, 6, 12))]


def test_focused_option_backfill_prioritizes_strategy_relevant_moneyness():
    session = make_session()
    upsert_stock_bars(
        session,
        [
            MassiveBar("IBIT", datetime(2026, 6, 5), 100, 101, 99, 100),
            MassiveBar("IBIT", datetime(2026, 6, 12), 101, 102, 100, 101),
        ],
        interval="1d",
    )
    contracts = [
        MassiveOptionContract("O:IBIT260619C00110000", "IBIT", date(2026, 6, 19), "call", 110.0),
        MassiveOptionContract("O:IBIT260619C00180000", "IBIT", date(2026, 6, 19), "call", 180.0),
        MassiveOptionContract("O:IBIT260619P00094000", "IBIT", date(2026, 6, 19), "put", 94.0),
        MassiveOptionContract("O:IBIT260619P00050000", "IBIT", date(2026, 6, 19), "put", 50.0),
    ]
    upsert_contracts(session, contracts)
    session.commit()

    focused = query_focused_contracts_for_bar_backfill(
        session,
        ["IBIT"],
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
    )

    symbols = [contract.provider_symbol for contract in focused]
    assert "O:IBIT260619C00110000" in symbols
    assert "O:IBIT260619P00094000" in symbols
    assert "O:IBIT260619C00180000" not in symbols
    assert "O:IBIT260619P00050000" not in symbols


class FakeMassiveClient:
    def __init__(self):
        self.calls_made = 0
        self.symbols: list[str] = []
        self.ranges: list[tuple[str, date, date]] = []

    def get_option_bars(self, option_symbol, from_date, to_date, *, timespan):
        self.calls_made += 1
        self.symbols.append(option_symbol)
        self.ranges.append((option_symbol, from_date, to_date))
        return []


class FakeContractClient:
    def __init__(self):
        self.calls_made = 0
        self.expiration_gtes: list[date | None] = []

    def iter_option_contract_pages(self, underlying, *, as_of, expired, expiration_gte, expiration_lte):
        self.calls_made += 1
        self.expiration_gtes.append(expiration_gte)
        return iter([[]])


class FakeUnderlyingClient:
    def __init__(self):
        self.calls_made = 0
        self.ranges: list[tuple[str, date, date]] = []

    def get_stock_bars(self, symbol, from_date, to_date, *, timespan):
        self.calls_made += 1
        self.ranges.append((symbol, from_date, to_date))
        return []


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()
