# QQQ and SPY cash-session pivots

**SPY is now supported alongside QQQ.** Both use the existing cash-open pivot formula with their own candles and closing options IV. The original QQQ code, data, results and JSON exports remain unchanged.

- P is the instrument's cash-session opening price.
- R1–R3 and S1–S3 use the same 0.38, 0.67 and 1.00 multipliers and original rounding.
- Spacing uses the last 20 completed cash sessions and closing near-ATM call/put IV; no current-day prices beyond the opening price enter the projection.
- Volume and the previously proposed profile/VWAP/ADX/RVOL features remain outside this version's calculation.
- Each instrument gets separately named daily JSON. QQQ touch rates are never assigned to SPY.

## Run either instrument

From the project directory, using the existing Python environment:

```powershell
& .\work\theta-venv\Scripts\python.exe outputs/pivot_algorithm/run_pivots.py --symbol SPY --start 2026-09-28 --end 2026-10-02
& .\work\theta-venv\Scripts\python.exe outputs/pivot_algorithm/run_pivots.py --symbol QQQ --start 2026-09-28 --end 2026-10-02
```

The SPY example has already completed. The collector refuses to overwrite a completed result; inspect or verify that recorded run instead. Other historical ranges must have supported, hash-bound provider calendars and sufficient completed history. Missing candles or qualified options stop the run rather than silently excluding sessions.

The shared runner writes a separate `runs/SYMBOL_START_END/` folder containing request receipts, raw data, options, forecasts, daily JSON and verification results. Its CLI is a historical collector and calculator. The public `at_cash_open()` function accepts an observed opening price, but an automated live opening feed is not included.

Read-only verification of the recorded SPY example:

```powershell
& .\work\theta-venv\Scripts\python.exe outputs/pivot_algorithm/verify_run.py outputs/pivot_algorithm/runs/SPY_2026-09-28_2026-10-02
& .\work\theta-venv\Scripts\python.exe -m unittest discover -s outputs/pivot_algorithm -p test_*.py -v
```

## Daily JSON

`daily/` contains ten self-contained examples: five QQQ files and five SPY files for September 28–October 2, 2026. `daily_manifest.json` binds their paths and hashes.

Each record names the ticker, cash date, timezone, P/R/S prices, 20 historical variances and IV inputs, all 20 qualified option pairs, dollar offsets, calculation definitions and source hashes. Archive/publication freshness is explicit, and calibrated touch probability is null. The new shared record format is identified by `schema_version: pivot-algorithm-daily-v1`.

The previous `outputs/pivot_json/export_daily.py` and its schema remain the frozen QQQ archive exporter. Its original files and interface are preserved. New dual-instrument calculations and JSON use this `pivot_algorithm` entry point.

Python callers can import the shared core:

```python
from outputs.pivot_algorithm.algorithm import prepare, at_cash_open, daily_record

# data contains symbol, the authoritative session roster, exact prior-date
# lists, complete cash rows and qualified closing option pairs.
prepared = prepare("SPY", target_date, data)
projected = at_cash_open(prepared, observed_opening_price, observed_opening_time)
record = daily_record(projected, data["options"], data["session"][target_date], source_hashes)
```

The adapter rejects mismatched candle metadata, foreign option symbols, incorrect next-session expiry, bad cash clocks, nonpositive IV, incomplete history and invalid opening prices. Quote and stock provenance is checked against the actual requested symbol and source hashes before the runner calculates anything.

## Recorded SPY check

ThetaData returned **27 requests / 19,038 raw rows**. All **24 prior cash sessions / 9,360 prior cash minutes**, their closing ATM pairs, and **five target sessions / 1,950 cash minutes** passed validation.

| Cash date | P | R1 | S1 |
|---|---:|---:|---:|
| 2026-09-28 | 768.35 | 769.22 | 767.48 |
| 2026-09-29 | 766.83 | 768.22 | 765.44 |
| 2026-09-30 | 766.45 | 767.92 | 764.98 |
| 2026-10-01 | 764.36 | 765.98 | 762.74 |
| 2026-10-02 | 770.58 | 772.34 | 768.82 |

In this bounded development check, R1 touched on 3/5 sessions, S1 on 5/5, either on 5/5, and both on 3/5. These are five recorded observations, not a calibrated future probability or an untouched accuracy test. Touch means an archived cash-minute bar spans the level, not a certified fill, reversal or profit.

`spy_independent_check.log` confirms a separate recomputation of volatility/IV arithmetic, inherited rounded offsets, all 30 target outcomes, source hashes, chronological stages and JSON instrument identity. The formula was neither fitted nor changed for SPY.

QQQ regression checks reproduce all 35 original prices and all canonical forecast fields exactly. Only the old archive's supplementary `opening_source` metadata is replaced by the new record's own source manifest. All 24 original QQQ closing pairs match the old adapter. The 12 new tests and 16 existing JSON tests pass.

## Source and timing limits

The historical archive's publication vintage and real opening/quote receipt latency remain unverified. Prior records retain the 17:15 ET availability convention. Options use the original provider settings, including SOFR and zero annual dividend input. This feature does not calibrate SPY probabilities, test dividend effects, establish trading performance or add orders.

The rules are saved before provider requests; prior distances are saved before admitting each target open; projections and daily JSON are saved before touch scoring. Separate ticker folders prevent SPY/QQQ cache collisions. Credentials are read from the existing protected store and are absent from outputs.

### Architect Update

- Diagnostic: the math was reusable; the old collector and archive exporter were fixed to QQQ.
- Changes: a shared QQQ/SPY entry point, per-symbol data collection, detailed daily JSON and instrument/clock guards. Frozen QQQ artifacts remain unchanged.
- Quality gates: 12 new tests, 16 existing JSON tests, exact QQQ parity, recorded SPY data qualification and independent SPY arithmetic/touch/JSON checks passed; final preservation status is in `verification.json`.
- System state: SPY and QQQ supported for historical calculation; live automation and calibrated SPY probabilities remain unverified.
