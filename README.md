# Options Flywheel

Local-first FastAPI app for modeling weekly option-income strategies on IBIT and ASST while preserving upside and building a SATA income sleeve.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Then open:

```text
http://127.0.0.1:8000
```

On this host, the deployed nginx route is:

```text
https://vibeprojects.us/options/
```

The deployed service can be checked or restarted with:

```bash
sudo systemctl status sata-options.service
sudo systemctl restart sata-options.service
```

## Configuration

Live data is selected with environment variables in `.env`:

```bash
MARKET_DATA_PROVIDER=yahoo
APP_BASE_PATH=
MINIMAX_API_KEY=
MINIMAX_BASE_URL=https://api.minimax.io/v1
MINIMAX_MODEL=MiniMax-M2.7
MINIMAX_TIMEOUT_SECONDS=90
KIMI_API_KEY=
KIMI_BASE_URL=https://api.kimi.com/coding/v1
KIMI_MODEL=kimi-for-coding
KIMI_TIMEOUT_SECONDS=90
OPENAI_API_KEY=
AI_RATIONALE_MODEL=gpt-5.3-codex
AI_RATIONALE_TIMEOUT_SECONDS=45
TRADIER_TOKEN=
POLYGON_API_KEY=
MASSIVE_API_KEY=
MASSIVE_BASE_URL=https://api.massive.com
MASSIVE_CALLS_PER_MINUTE=5
ALPACA_KEY_ID=
ALPACA_SECRET_KEY=
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
IBKR_CLIENT_ID=1
```

`yahoo` is the default deployed provider for no-key real quotes, price history, expirations, and option chains. Yahoo option Greeks are calculated locally from the chain's IV/price fields when the provider does not return Greeks. Treat Yahoo data as public/delayed and confirm in Fidelity or another brokerage before trading.

`mock` still exists for offline development with `sample_data/`, but production use should be `yahoo`, `tradier`, `polygon`, `alpaca`, or `ibkr`. API keys are read from environment variables only and are not stored in SQLite.

Tradier is the recommended API-key provider for richer live option-chain data because its options-chain endpoint can include Greeks when requested. Polygon/Massive, Alpaca, and IBKR are scaffolded with configuration warnings in this MVP.

## Massive Historical Backfill

Massive historical options data is cached locally before it is used for backtests. The backfill runner is intentionally rate-limited and resumable; it upserts rows into SQLite and stops cleanly after the configured call budget.

Run a five-call chunk:

```bash
python scripts/massive_backfill.py --underlying IBIT --underlying ASST --max-calls 5 --calls-per-minute 5
```

Useful staged runs:

```bash
python scripts/massive_backfill.py --mode underlying-bars --max-calls 5
python scripts/massive_backfill.py --mode contracts --max-calls 5
python scripts/massive_backfill.py --mode option-bars --focused-option-bars --max-calls 5 --max-contracts 5
python scripts/massive_backfill.py --mode focused-cycle --max-calls 5 --max-contracts 5
```

Tables populated by the backfill:

- `price_history` for IBIT/ASST underlying bars.
- `historical_option_contracts` for option reference data.
- `option_price_bars` for historical option OHLCV bars.

Underlying bar backfill refreshes from the latest cached bar with a short lookback window, so it keeps recent prices current without replaying the full two-year range. Contract backfill resumes after the latest cached expiration per underlying. Option-bar backfill stores the date fetched through per contract, so later runs append new daily bars instead of replaying the same range.

The deployed timer uses `focused-cycle`: it first spends calls on strategy-relevant option bars, then uses leftover calls to extend contract metadata. Focused option bars prioritize covered-call and cash-secured-put moneyness bands instead of every deep out-of-the-money strike. The `Backtest` page shows current cache coverage, technical-regime coverage, and the next focused contracts waiting for option bars. The backtester should query these local tables, not Massive directly.

The optimizer page has a portfolio-level AI review button plus a per-ticker `Generate` rationale button. Provider preference is MiniMax, then Kimi, then OpenAI, then local deterministic fallback. If `MINIMAX_API_KEY` is configured, the app calls the MiniMax OpenAI-compatible chat-completions endpoint using `MINIMAX_MODEL`.

## Uploading Positions

Go to `Uploads`, select `Fidelity positions CSV`, and upload a Fidelity-style export.

The parser handles:

- 16-column Fidelity header.
- Data rows with trailing blank 17th field.
- Fidelity disclaimer/download stop rows.
- Dollar and percent parsing.
- Fractional shares.
- Signed option quantities.
- Cash rows such as `SPAXX**`.
- `Pending activity`.
- Fidelity option symbols with optional leading dash.

Option side is always inferred from quantity:

- Negative quantity means short option.
- Positive quantity means long option.
- The leading dash in Fidelity option symbols is ignored for side detection.

## Recommendations

The optimizer scores candidates across:

- Upside preservation.
- Premium generation.
- Trend alignment.
- IV richness.
- Liquidity.
- Scenario robustness.

It does not choose the highest premium solely because it is high. If candidates have poor liquidity, stale data, excessive spread, low premium, trend conflict, or heavy-bull underperformance risk, the app can recommend `skip trade`.

Defaults:

- Preserve at least 50% of IBIT/ASST untouched.
- Use 25%, 35%, 40%, or 50% optioned sleeves.
- Prefer 30-35 delta calls in balanced mode.
- Penalize 40 delta calls during bullish breakouts.
- Use 40-50 delta puts only when modeling re-entry after assignment.

## SATA Projections

SATA assumptions live in `Settings`.

Defaults:

- Annual dividend assumption: `13%`.
- DRIP enabled.
- Assumed SATA price: `$100`.
- Tax status: tax-free.

Only option premium is automatically modeled as a SATA contribution. Called-away principal remains cash collateral for put selling unless manually modeled otherwise.

## Buy-And-Hold Comparison

Dashboard, scenarios, and Monte Carlo compare the strategy against buy-and-hold. The app distinguishes:

- Gain/loss versus starting capital.
- Outperformance/underperformance versus buy-and-hold.

A strategy can lose money overall but still outperform buy-and-hold in a bearish scenario. The heavy-bull scenario explicitly warns when covered calls or wheel cash drag underperform buy-and-hold.

## Running Tests

```bash
pytest
```

Tests cover Fidelity parsing, option-symbol parsing, cash/pending classification, options math, SATA projection, scenario math, optimizer skip behavior, and Monte Carlo output shape.

## Sample Data

Sample files are in `sample_data/`:

- `portfolio_sample_fidelity.csv`
- `ibit_option_chain_sample.csv`
- `asst_option_chain_sample.csv`
- `ibit_ohlcv_sample.csv`
- `asst_ohlcv_sample.csv`

The mock provider uses these files and generates deterministic fallback history/chains when a sample file has fewer rows than requested.
