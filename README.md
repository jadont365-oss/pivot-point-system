# QQQ / SPY Pivot Point System

Python cash-session pivot calculations, historical ThetaData collection, detailed daily JSON and independent recorded-data verification. The Git repository preserves the existing calculation; it does not include downloaded market data, credentials or archived performance results.

## What this version does

- **P:** observed opening price in the 09:30 Eastern cash minute, preserved to eight decimal places.
- **Spacing:** the last 20 completed cash sessions' one-minute realized variance, adjusted by the latest prior closing ATM IV divided by the average prior 20 closing IVs.
- **Levels:** R1/S1, R2/S2, R3/S3 use 0.38, 0.67 and 1.00 times that dollar movement scale. Original cent rounding is applied before the offsets move onto the opening P.
- **Freeze:** spacing is reconstructed at 09:25 ET, the observed cash open anchors P at 09:30, and the resulting levels remain fixed for that session.
- **Outputs:** ticker-separated forecasts, daily JSON, touch results and source/hash records. The target cash session's later candles are used only to score touches.

Volume, value areas, VWAP, ADX, RVOL and Greeks beyond option eligibility do not determine this version's spacing. No live opening collector, automatic trading, deployment or calibrated probability is included. A historical minute bar overlapping a target does not prove an executable fill or profitability.

## Windows setup

For a new code-only checkout, install Python 3.12 and run these PowerShell commands from the repository root:

```powershell
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe scripts/check_repository.py
& .\.venv\Scripts\python.exe outputs/pivot_algorithm/run_pivots.py --help
```

The original computer already has `work/theta-venv/Scripts/python.exe`; it can continue using that existing environment. No environment folder is tracked by Git.

## Historical collection and recorded tests

For collection and legacy JSON validation, install `requirements-data.txt`. The historical CLI additionally requires the existing `outputs/qqq_range/collection_status.json` and its hash-bound `data/calendar_2025.parquet` / `calendar_2026.parquet`. Restore those from the original local project at the same relative paths before running a historical request.

The Windows collector reads the existing DPAPI credential from `%USERPROFILE%\.codex\secrets\thetadata-qqq-range.dpapi.xml`. It is tied to the Windows user context; a code clone does not recreate it. Do not commit the key or its encrypted credential file. CI does not load a key or request market data.

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements-data.txt
& .\.venv\Scripts\python.exe outputs/pivot_algorithm/run_pivots.py --symbol SPY --start 2026-07-01 --end 2026-07-31
```

This example requires restored calendars and provider access. A completed historical run cannot be overwritten. To verify the already recorded July run, restore its full `outputs/pivot_algorithm/runs/SPY_2026-07-01_2026-07-31/` directory and raw data files referenced by its manifests, then run:

```powershell
& .\.venv\Scripts\python.exe outputs/pivot_algorithm/verify_run.py outputs/pivot_algorithm/runs/SPY_2026-07-01_2026-07-31
```

The original recorded-data acceptance suites require additional QQQ archives/manifests from the original project. They are separate from the offline CI suite:

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s outputs/pivot_algorithm -p test_*.py -v
& .\.venv\Scripts\python.exe -m unittest discover -s outputs/pivot_json -p test_*.py -v
```

Keep a separate backup of the original ignored `outputs/` research archive. Git stores the selected code/config and setup, not those results or datasets. Preserve original manifests, raw data and consumed evaluation dates together. Code or CI success does not establish a new market test or future touch probability.

## Git and CI

`.gitignore` is an explicit file allowlist. `.gitattributes` preserves the exact bytes of hash-bound original files under `outputs/`. `SOURCE_MANIFEST.json` binds the 18 original source/config/guide files included in the initial release; future changes need an explicit reviewed manifest update.

GitHub Actions runs on push, pull request or manual dispatch. It uses Windows, Python 3.12, pinned dependencies and SHA-pinned official actions. The checks audit tracked source/content and run synthetic calculations, session/option boundary checks, JSON checks and CLI help without provider credentials or historical datasets.

Routine workflow:

```powershell
git switch -c feature/my-change
# Make and verify the intended changes.
git add <specific-files>
git diff --cached
& .\.venv\Scripts\python.exe scripts/check_repository.py
git commit -m "Describe the verified change"
git push -u origin feature/my-change
```

The audit checks staged content; stage the files before running it. This task uses a separate project repository and leaves the parent home-directory Git repository alone.

See [the original system guide](outputs/pivot_algorithm/README.md), [legacy JSON guide](outputs/pivot_json/README.md) and [setup plan](docs/superpowers/plans/2026-10-09-pivot-git-ci.md).

CI references: [GitHub's Python testing documentation](https://docs.github.com/en/actions/tutorials/build-and-test-code/python), [Git attributes](https://git-scm.com/docs/gitattributes).
