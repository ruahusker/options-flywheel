from __future__ import annotations

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app.models import portfolio, options, market_data, strategy, journal, settings as settings_model, iv_history

    _ = (portfolio, options, market_data, strategy, journal, settings_model, iv_history)
    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_columns()


def _ensure_sqlite_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    with engine.begin() as connection:
        _ensure_column(connection, inspector, table_names, "historical_option_contracts", "bars_fetched_at", "DATETIME")
        _ensure_column(connection, inspector, table_names, "historical_option_contracts", "bars_fetched_interval", "VARCHAR(20)")
        _ensure_column(connection, inspector, table_names, "historical_option_contracts", "bars_fetched_through", "DATE")

        for table_name in ("holdings", "option_positions", "cash_positions", "trade_journal_entries"):
            _ensure_column(connection, inspector, table_names, table_name, "account_number", "VARCHAR(100)")
            _ensure_column(connection, inspector, table_names, table_name, "account_name", "VARCHAR(255)")

        _ensure_column(connection, inspector, table_names, "sata_settings", "tax_rate", "FLOAT DEFAULT 0.0")


def _ensure_column(connection, inspector, table_names: set[str], table_name: str, column_name: str, sql_type: str) -> None:
    if table_name not in table_names:
        return
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name not in columns:
        connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {sql_type}")
