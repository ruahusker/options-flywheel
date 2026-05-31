from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_name: str = "Options Flywheel"
    base_path: str = os.getenv("APP_BASE_PATH", "").rstrip("/")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./sata_options_optimizer.db")
    market_data_provider: str = os.getenv("MARKET_DATA_PROVIDER", "mock").lower()
    # When true, the web path reads market data from MarketDataCache via CachedProvider (no live
    # calls on page load); the scheduled refresh job is the only thing that hits the real provider.
    # Default false so dev/tests use the real/mock provider directly; the deployed unit sets it true.
    use_market_cache: bool = os.getenv("MARKET_DATA_CACHE", "false").strip().lower() in {"1", "true", "yes", "on"}
    tradier_token: str | None = os.getenv("TRADIER_TOKEN") or None
    minimax_api_key: str | None = os.getenv("MINIMAX_API_KEY") or None
    minimax_base_url: str = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1").rstrip("/")
    minimax_model: str = os.getenv("MINIMAX_MODEL", "MiniMax-M2.7")
    minimax_timeout_seconds: float = float(os.getenv("MINIMAX_TIMEOUT_SECONDS", "90") or 90)
    kimi_api_key: str | None = os.getenv("KIMI_API_KEY") or None
    kimi_base_url: str = os.getenv("KIMI_BASE_URL", "https://api.kimi.com/coding/v1").rstrip("/")
    kimi_model: str = os.getenv("KIMI_MODEL", "kimi-for-coding")
    kimi_timeout_seconds: float = float(os.getenv("KIMI_TIMEOUT_SECONDS", "90") or 90)
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY") or None
    ai_rationale_model: str = os.getenv("AI_RATIONALE_MODEL", "gpt-5.3-codex")
    ai_rationale_timeout_seconds: float = float(os.getenv("AI_RATIONALE_TIMEOUT_SECONDS", "45") or 45)
    polygon_api_key: str | None = os.getenv("POLYGON_API_KEY") or None
    massive_api_key: str | None = os.getenv("MASSIVE_API_KEY") or None
    massive_base_url: str = os.getenv("MASSIVE_BASE_URL", "https://api.massive.com").rstrip("/")
    massive_calls_per_minute: float = float(os.getenv("MASSIVE_CALLS_PER_MINUTE", "5") or 5)
    alpaca_key_id: str | None = os.getenv("ALPACA_KEY_ID") or None
    alpaca_secret_key: str | None = os.getenv("ALPACA_SECRET_KEY") or None
    ibkr_host: str = os.getenv("IBKR_HOST", "127.0.0.1")
    ibkr_port: int = int(os.getenv("IBKR_PORT", "7497") or 7497)
    ibkr_client_id: int = int(os.getenv("IBKR_CLIENT_ID", "1") or 1)
    default_tax_status: str = "tax_free"
    default_sata_annual_rate: float = 0.13
    default_sata_price: float = 100.0
    # Risk-free rate used for locally computed Greeks / risk-neutral probabilities. Has negligible
    # effect at the 1-14 DTE this app trades, but is configurable rather than hard-coded.
    risk_free_rate: float = float(os.getenv("RISK_FREE_RATE", "0.04") or 0.04)
    sample_data_dir: Path = Path("sample_data")


settings = Settings()
