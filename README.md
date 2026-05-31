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
MARKET_DATA_PROVIDER=tradier
MARKET_DATA_CACHE=true
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

`tradier` is the deployed provider. It returns native dealer Greeks (delta/gamma/theta/vega + IV via ORATS) on the option chain, so the roll engine uses dealer Greeks instead of locally-computed ones. Set `TRADIER_TOKEN` to a Tradier production token. Treat the data as delayed when the market is closed and confirm in Fidelity or another brokerage before trading.

`yahoo` remains a no-key fallback (Greeks computed locally from IV/price). `mock` exists for offline development with `sample_data/`; `polygon`, `alpaca`, and `ibkr` are scaffolded. API keys are read from environment variables only and are not stored in SQLite.

`MARKET_DATA_CACHE=true` puts the web app in cached mode: page loads read precomputed data and make **no** live provider calls (see "Scheduled Market-Data Refresh" below). Leave it unset for local development so pages fetch live from the configured provider.

## Scheduled Market-Data Refresh

In production the web app never fetches market data or runs the heavy page computation on request.
A scheduled job is the **only** thing that calls the live provider (Tradier); the web path renders a
precomputed snapshot, so pages load instantly and outbound/OPRA calls stay minimal.

How it fits together:

- `scripts/refresh_market_data.py` fetches quotes, history, expirations, and the front option chains
  for IBIT/ASST into `MarketDataCache`, records the daily ATM IV, and precomputes every heavy page's
  full render payload into `PrecomputeCache` (pickled). It is gated on the Tradier market clock and
  fetches only when the market is `open`/`postmarket` (use `--force` for a manual run).
- `app/services/market_data/cached_provider.py` (`CachedProvider`) serves pages from
  `MarketDataCache`. `get_provider()` returns it when `MARKET_DATA_CACHE=true`; the refresh job uses
  `get_refresh_provider()` for the real provider.
- `app/services/precompute.py` holds one builder per page (`week`, `roll`, `portfolio`, `optimizer`,
  `indicators`, `scenarios`, `monte_carlo`, `live_data`). Routers call `precompute.load_or_build`;
  a cold cache falls back to a live build through `CachedProvider`. Uploading a Fidelity CSV triggers
  an immediate rebuild so new positions show without waiting for the next tick.
- The topbar shows a "Data as of HH:MM · N min ago" indicator (formatted client-side in `app.js`),
  turning amber when the data is more than ~25 minutes stale.

Deployed timer (every 15 min during market hours, US Central window with the Tradier clock as the
precise gate):

```bash
sudo systemctl status market-refresh.timer
sudo systemctl start market-refresh.service          # run one refresh now (skips if market closed)
python scripts/refresh_market_data.py --force        # force a refresh regardless of market state
```

Units live in `deploy/market-refresh.{service,timer}`. The web unit
(`deploy/sata-options.service`) sets `MARKET_DATA_CACHE=true`.

## Authentication

Everything under `/options` is protected by nginx HTTP basic auth (`auth_basic`) on both the app and
static locations — see `deploy/nginx-options-location.conf`. The password file is server-only and is
**not** in git. Create or rotate it with:

```bash
# rotate the password (prompts; no plaintext on the command line)
sudo sh -c "printf 'options:%s\n' \"$(openssl passwd -apr1)\" > /etc/nginx/.options_htpasswd"
sudo nginx -t && sudo systemctl reload nginx
```

This keeps the deployment within Tradier/OPRA personal-use terms: the market data is for the account
holder only and is not displayed/redistributed to others.

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
