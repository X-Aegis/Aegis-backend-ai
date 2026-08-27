📝 **Description**

Today the risk dashboard and volatility model run on seeded rate data: a user in Lagos or Nairobi can see a "volatility score" but has no reason to trust it, because nothing behind it reflects a real market print. This PR makes the whole pipeline honest — live NGN/KES rates flow from public FX sources into `fx_rates`, and every downstream consumer reads them.

### 🛠 What's changed

#### 1. Ingester service (`services/forex_ingester.py`)
- **Scheduled worker** — one cycle every 15 minutes (`FX_INGEST_INTERVAL_SECONDS`), `--once` for cron/CI. A failing cycle is logged and the loop keeps going.
- **Source fallback** — the official chain is tried in order and fails over on the first vendor that returns nothing usable:

  | Order | Source | Market | Credentials |
  |---|---|---|---|
  | 1 | Fixer.io (`FX_PRIMARY_SOURCE`) | official | `FIXER_API_KEY` |
  | 2 | Open Exchange Rates | official | `OPEN_EXCHANGE_RATES_APP_ID` |
  | 3 | ExchangeRate-API open endpoint | official | none — keyless last resort |
  | — | Parallel-market feed | parallel | `PARALLEL_MARKET_API_URL` |

  "Nothing usable" includes a feed that answers `200` but keeps republishing the same timestamp — a frozen feed is treated exactly like a dead one. The keyless last resort means a deployment with no vendor keys still serves real rates instead of nothing.
- **Parallel market runs alongside the official chain, not instead of it.** NGN street rates can sit 20%+ from the official print, and that is the rate people actually transact at.
- **Validation before storage** — non-positive/non-finite quotes, untracked pairs, timestamps from a skewed clock, and stale prints are all dropped with a log line. Feeds that publish less often than we poll (the daily keyless fallback) carry their own freshness budget instead of being judged by the 45-minute rule.

#### 2. Storage (`db/schema.sql`, `db/migrations/001_fx_rates_multi_source.sql`)
`fx_rates` was keyed on `(timestamp, pair)`, so an official and a parallel quote for the same minute collided and one was silently dropped by `ON CONFLICT DO NOTHING`. The key is now `(timestamp, pair, source)`, plus the two lookup indexes the new endpoints need. The migration is idempotent and was verified against both the old and new schema on a real Postgres instance.

#### 3. API (`api/fx.py`)
- `GET /fx/current?pair=NGN/USD` — latest live rate in **either** orientation (`NGN/USD` or `USD/NGN`); callers never need to know the storage orientation.
- `GET /fx/history` — the rate window a score was computed from, so a user can audit the number.
- `GET /fx/sources` — per-feed health: last quote, its age, staleness, points in the last 24h. This is what separates "one feed stalled" from "ingestion is dead".
- **Stale-rate guard** — a quote past its source's freshness budget returns `503` instead of being served as live. `allow_stale=true` opts in explicitly, and every response carries `age_seconds` / `is_stale` either way.

#### 4. Risk endpoint now reads live rates (`api/risk.py`, `services/live_risk.py`)
`GET /risk/current` no longer reads stored predictions. It computes **realized volatility** — the standard deviation of log returns over the window, rescaled to the horizon by the square-root-of-time rule — from ingested rate history.

Two details that matter for trustworthiness:
- **One source per series.** Official and parallel quotes for NGN can be 20%+ apart; interleaving them would manufacture volatility that never happened. The score is computed *within* a single feed, preferring the parallel market, and the response names the `source` and `market` it used.
- **Irregular sampling.** Ingestion cycles can be missed, so the sample interval is measured from the data (median gap) rather than assumed.

No live history → `404`. Stale or too-thin history → `503` with the reason. A score is never invented. The response keeps `volatility_score` / `risk_level` intact, so the keeper bot needs no changes.

### 📸 Demo

`python scripts/demo_live_fx.py` runs the real ingester and the real endpoints with **no database and no API keys** (in-memory `fx_rates`, live network fetch). Output from a run just now:

```console
$ python scripts/demo_live_fx.py

Fixer.io API key not found.
Official source Fixer.io returned no usable rates — failing over.
Open Exchange Rates App ID not found.
Official source OpenExchangeRates returned no usable rates — failing over.

========================================================================
1. LIVE INGESTION — real rates fetched from the public FX feed
========================================================================
Feed:  https://open.er-api.com/v6/latest/USD
Time:  2026-08-26T18:41:36.985511+00:00

Ingestion summary: {'saved': 4, 'official_source': 'ExchangeRate-API', 'parallel_source': None,
                    'pairs': ['USD/GHS', 'USD/KES', 'USD/NGN', 'USD/ZAR']}

pair                rate  source              market    quoted at
USD/GHS          11.1820  ExchangeRate-API    official  2026-08-26T00:02:31+00:00
USD/KES         129.4560  ExchangeRate-API    official  2026-08-26T00:02:31+00:00
USD/NGN       1,350.7534  ExchangeRate-API    official  2026-08-26T00:02:31+00:00
USD/ZAR          15.9415  ExchangeRate-API    official  2026-08-26T00:02:31+00:00

========================================================================
2. LIVE ENDPOINTS — served from the rates fetched above
========================================================================

$ GET /fx/current?pair=NGN/USD
→ HTTP 200
{
  "pair": "NGN/USD",
  "rate": 0.0007403275655715676,
  "timestamp": "2026-08-26T00:02:31Z",
  "source": "ExchangeRate-API",
  "market": "official",
  "age_seconds": 67146.818236,
  "is_stale": false
}

========================================================================
3. GUARDS — the API refuses to dress stale or thin data up as live
========================================================================

$ GET /fx/current?pair=USD/ZAR&market=parallel
→ HTTP 503
{
  "detail": "Stale rate for 'USD/ZAR': last quote from SimulatedStalledFeed is 180.0 minutes old
             (max 45). Retry once ingestion recovers, or pass allow_stale=true."
}

$ GET /risk/current?pair=USD/NGN&allow_stale=true
→ HTTP 503
{
  "detail": "Insufficient live rate history for 'USD/NGN': Only 1 live observation(s) from
             ExchangeRate-API; 8 are required. Ingestion runs every 15 minutes; at least 8
             observations must accumulate in the 24h window."
}

========================================================================
4. RISK SCORING — over a SIMULATED intraday parallel-market series
========================================================================

!! Every rate in this section is SIMULATED, not market data. It stands in
!! for the 15-minute parallel-market feed (PARALLEL_MARKET_API_URL) that a
!! production deployment ingests.

$ GET /risk/current?pair=USD/KES&market=parallel&horizon=1
→ HTTP 200
{
  "timestamp": "2026-08-26T18:41:37.832135Z",
  "horizon": 1,
  "volatility_score": 50.67,
  "risk_level": "MEDIUM",
  "pair": "USD/KES",
  "source": "SimulatedParallelMarket",
  "market": "parallel",
  "latest_rate": 129.3650634461344,
  "rate_age_seconds": 0.002922,
  "data_points": 48,
  "window_hours": 24,
  "realized_volatility": 0.014133469690248491
}
```

Section 4 is explicitly labelled as simulated: the free keyless feed publishes once a day, which is enough to prove live fetching but cannot support a volatility score — so the demo shows the guard firing on real data (section 3) and the scoring path on a labelled synthetic intraday series. A production deployment with `PARALLEL_MARKET_API_URL` set scores real street rates on the same path.

I also ran the full stack against a throwaway PostgreSQL instance (schema + migration + real ingestion + all endpoints) to verify the SQL, not just the Python:

```console
$ GET /risk/current {'pair': 'USD/NGN', 'horizon': 24, 'market': 'parallel'} -> 200
  {'volatility_score': 85.64, 'risk_level': 'HIGH', 'pair': 'USD/NGN', 'source': 'ParallelMarket',
   'market': 'parallel', 'latest_rate': 1757.0, 'data_points': 48, 'window_hours': 24}
```

### ✅ Acceptance criteria

- [x] Ingester fetches NGN and KES rates on a schedule (every 15 min)
- [x] Rates stored in `fx_rates` (timestamp, pair, rate, source)
- [x] Primary API (Fixer / Open Exchange Rates) + parallel-market feed, with fallback if the primary is down
- [x] `GET /fx/current?pair=NGN/USD` returns the latest live rate
- [x] `/risk/current` uses live rate history, not stubs
- [x] Tests cover source fallback, stale-rate guard, and rate freshness validation

### 🧪 Tests

`233 passed` (111 more than on `main`), `ruff check .` clean. No test needs a database or an API key.

- `tests/test_forex_ingester.py` (40) — fallback when the primary is down *and* when it is frozen, primary preference and configurability, per-source freshness budgets, future-timestamp rejection, malformed/non-positive quotes, parallel-feed parsing and isolation, ingestion cycle, scheduler resilience.
- `tests/test_fx_api.py` (16) — orientation inversion, market filters, 404/400/422, stale guard and its explicit bypass, history windowing, source health.
- `tests/test_live_risk.py` (19) — source selection never mixes feeds, volatility maths, score bounds and monotonicity, horizon scaling, insufficient-history errors.
- `tests/test_fx_utils.py` (21) — pair normalisation and freshness helpers.
- `tests/test_risk_api.py` — rewritten for live-rate scoring (the previous tests asserted the stubbed `get_current_prediction` path this PR removes); `/risk/history` tests unchanged.

### ⚠️ Notes for the reviewer

- **Migration required before deploy** — `db/migrations/001_fx_rates_multi_source.sql` must run on existing databases; without it the parallel feed's prints are silently dropped by the old primary key.
- **`GET /risk/history` still reads the `predictions` table**, which is fed by `POST /monitoring/prediction`. Out of scope here, but persisting each live score so history and drift monitoring see live data is the obvious follow-up.
- **`fetch_binance_p2p_rates` is gone**, replaced by the configurable parallel-market feed. Binance shut down NGN P2P, so a generic adapter is a more honest primitive than a placeholder for one venue. The data-source-down runbook was updated accordingly.
- The keyless third vendor is deliberately last in the chain and carries a 25-hour freshness budget — it keeps `/fx/current` alive without keys, but it is too coarse to compute volatility from, and the endpoint says so rather than pretending otherwise.

---
Closes #30
cc @bbkenny
