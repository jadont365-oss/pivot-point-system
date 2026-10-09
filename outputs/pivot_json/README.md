# Daily Pivot JSON

This is the JSON export for the existing QQQ cash-open Python pivot system. Each file can be given directly to an LLM: it includes the seven prices, definitions, calculation inputs, timing, sources, missing information and measured research results.

The exporter reads the frozen model results. It does not change the pivot formula or rerun the historical evaluations.

## Run it

From the project directory in PowerShell:

```powershell
.\work\theta-venv\Scripts\python.exe outputs\pivot_json\export_daily.py
.\work\theta-venv\Scripts\python.exe outputs\pivot_json\export_daily.py --verify
.\work\theta-venv\Scripts\python.exe -m unittest discover -s outputs\pivot_json -p test_*.py -v
```

The default command exports the five recorded sessions, September 28 through October 2, 2026, into `daily/QQQ_YYYY-MM-DD.json`, with `manifest.json` binding the files to their source hashes.

To export one recorded session into another folder:

```powershell
.\work\theta-venv\Scripts\python.exe outputs\pivot_json\export_daily.py --date 2026-10-01 --out-dir outputs\pivot_json\one_day
.\work\theta-venv\Scripts\python.exe outputs\pivot_json\export_daily.py --out-dir outputs\pivot_json\one_day --verify
```

Use the provided Python functions when integrating this export into another Python process:

```python
import datetime as dt
from outputs.pivot_json.export_daily import build_daily_record, load_source_bundle, validate_record

forecasts, options, evidence, provenance = load_source_bundle()
record = build_daily_record(
    forecasts[0], options, evidence, provenance,
    dt.datetime.now(dt.timezone.utc).isoformat(),
)
validate_record(record)
```

The CLI is an archive exporter; a live opening-price collector has not been added. The builder accepts the existing V3 forecast format. Formal structure is in `schema.json`; verification uses the already-installed `jsonschema` package. Export construction uses the standard library.

## What each daily record explains

- **Levels:** P is the observed cash opening price. R1-R3 are above P; S1-S3 are below it. Prices, signed dollar offsets and definitions are preserved.
- **Session:** QQQ, USD, America/New_York, cash date, opening calculation clock, closing time and actual export time. Early closes use the shorter cash duration.
- **History:** The 20 completed prior cash sessions, each with its realized log-return variance and prior-close near-ATM IV. The numerical inputs are included; the full minute tape remains in the source archives.
- **Calculation:** Average normalized variance is rescaled for the target session length. Its square root, multiplied by the previous cash close, gives the historical dollar scale. The latest closing IV divided by the 20-session average IV adjusts that scale. Multipliers 0.38, 0.67 and 1.00 set the original rounded offsets; those frozen offsets are shifted to the cash-open P.
- **Options:** The latest qualifying call/put snapshots, their units, timestamps and selection rules. Their quote time must equal the prior session's recorded close minus one minute, including early closes. IV is stored as an annual decimal, so 0.20 means 20%. Delta helps qualify the snapshots; it does not independently set pivot spacing.
- **Evidence:** Each research cohort's touch counts and sample size, with separate R1, S1, either-side, both-side and higher-level results. Development, previously consumed replay and consumed historical holdouts retain distinct labels.
- **Data status:** ThetaData archive sources and hashes; unknown quote receipt/event age and historical publication status are explicit. No credentials are exported.

## Reading probabilities correctly

The Gaussian/Brownian formula `2 * (1 - Phi(z))` describes touching a **single** upper or lower line under continuous, zero-drift, constant-volatility assumptions. Its labels are theoretical and use the nominal multipliers; the original cent rounding is preserved. These labels are not calibrated probabilities for QQQ, nor the probability of touching either R1 or S1.

Measured rates are counts divided by evaluated sessions. Touch means a one-minute cash bar's low/high overlaps the fixed line, using the source model's tolerance. This does not certify a trade at the exact price, a reversal, profit or containing the day's range. P is an automatic anchor and is excluded from predictive touch rates.

All attached research results are retrospective evidence assembled after the original forecast clock. They are explicitly separated from forecast inputs; the export does not pretend those reports existed at the historic opening time. Target-session highs, lows and eventual touches do not enter the forecast.

## Current scope

The exported version uses historical cash price movement and prior-close options IV for spacing, plus the cash open for P. Collected volume and the earlier proposed value areas, VWAP, ADX, RVOL and other Greeks do not enter this version's calculation.

`load_source_bundle()` adds the hash-bound roster's prior cash opening and closing timestamps to its copied option snapshots. Callers providing their own snapshot mappings must supply that authoritative cash window; the builder rejects a present snapshot with missing or inconsistent timing. Missing optional quote details remain explicit rather than being invented.

The feature verifies JSON structure, calculation consistency, provenance and preservation. It adds no new calibration, market-performance test, live feed, order execution or LLM service. The historical archive does not certify historically available quotes or actual receipt latency.

`model_recheck.json` records a fresh read-only check against the source model and archived data. `preservation_before.json` binds the original artifacts before this change. `verification.json` records the final feature checks and review outcomes.
