# Pivot Git and CI Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for sequential setup and a scoped reviewer for dependency and CI review. Steps use checkbox syntax.

**Goal:** Version the existing QQQ/SPY pivot system in its own private GitHub repository, with credential-free Windows CI and all original research preserved locally.

**Architecture:** Initialize Git at this project root, independent of the parent home-directory repository. Preserve the existing import graph and source bytes; publish an explicit source/config allowlist while excluding downloaded market data, historical results, virtual environments and secrets. Add offline synthetic checks and GitHub Actions in an ignored isolated worktree, verify a clean clone, then integrate and push.

**Tech Stack:** Git for Windows, existing GitHub CLI authentication, Python 3.12 CI, unittest, existing Polars/NumPy/tzdata dependencies, GitHub Actions.

## Scope and risk review

- Git is already installed: 2.55.0.windows.5; installation is unnecessary.
- The active GitHub account is `jadont365-oss`; create `pivot-point-system` as private.
- Existing QQQ/SPY formulas, contract, original code and archived research are immutable for this task.
- Preserve the eager V3 -> V2 -> V1 -> week helper -> engine imports, plus V2 context/cash_rebuild. The original source locations are part of this import boundary.
- Existing acceptance suites require ignored recorded data. CI must independently test actual calculation, session timing, ticker separation, source integrity and JSON using synthetic inputs, without downloaded candles or API access.
- Track exact dependency versions for Python 3.12. Keep ThetaData SDK separate from offline CI dependencies.
- No deployment, live schedule, provider requests, trading orders, upstream skill-repository changes or edits to the parent Git repository.
- A Git clone restores code, not the ignored historical data or Windows DPAPI credential. Document those restore requirements explicitly.

## Tasks

- [x] **1. Preserve and version the baseline.**
  - Record SHA-256 for every original output artifact and parent Git HEAD/index/config in `.unlazy/git-ci/before.json`.
  - Add a fail-closed root `.gitignore`: track only required Python sources, original V1 contract, JSON schema, selected guides and the new setup files. Exclude all downloaded data, results, keys, environments and `.worktrees/`.
  - Add `.gitattributes` with `outputs/** -text` so original bound bytes survive Windows checkout.
  - Initialize this root with `git init -b main`; confirm `git rev-parse --show-toplevel` equals this directory before staging.
  - Commit the audited existing baseline. Create `.worktrees/git-ci` on `setup/git-ci` only after `git check-ignore .worktrees/git-ci` succeeds.

- [x] **2. Add minimal portable CI in the isolated worktree.**
  - Add `requirements.txt` (offline dependencies), `requirements-data.txt` (ThetaData), root `README.md` (Windows setup, formula, local-only data, testing and live limitations).
  - Add `SOURCE_MANIFEST.json` for the byte-identical original source/config files.
  - Add `tests/test_ci.py`: construct 20 complete synthetic prior cash sessions, qualified next-session ATM option pairs and a cash-open observation; independently verify variance/IV/rounding and every level for QQQ and SPY; cover missing/future cash data, wrong instrument, invalid option IV/clock/expiry, half-day normalization, rejected opening clocks, outcome-independent spacing, touch semantics and finite detailed JSON.
  - Add `scripts/check_repository.py`: audit tracked filenames/content, original-source hashes and secret patterns without printing values. Include a positive synthetic secret control in the tests.
  - Add `.github/workflows/ci.yml`: on push/pull_request/manual dispatch, Windows runner, Python 3.12, SHA-pinned official checkout/setup-python, read-only token, offline requirements; run repository audit, synthetic tests and historical CLI `--help`. No API keys or downloads.

- [x] **3. Verify before publication.**
  - Run `python -m unittest discover -s tests -v`; require zero failures.
  - Run `python scripts/check_repository.py`; require audit success and no forbidden tracked files.
  - Run the two original local suites with the existing environment: `python -m unittest discover -s outputs/pivot_algorithm -p test_*.py -v` and `python -m unittest discover -s outputs/pivot_json -p test_*.py -v` from the original project; require all pass without rewriting artifacts.
  - Create a temporary clean clone under ignored `work/`; run the CI commands with no market-data directories. Confirm original tracked file hashes and help command.
  - Independently review scope and quality, including secret positive control, source completeness and future probability labels.
  - Recompare every original artifact and parent Git metadata with the preservation snapshot.

- [ ] **4. Integrate and publish.**
  - Commit setup changes on `setup/git-ci`; fast-forward into this repository's `main` after checks.
  - Create private GitHub repository with `gh repo create jadont365-oss/pivot-point-system --private --source . --remote origin --push` after staged-tree audit.
  - Verify private visibility, origin URL and local/remote commit equality. Observe the actual GitHub CI run and repair any measured failure.
  - Record evidence and gate dispositions under `.unlazy/git-ci/`; report repository link, Git installed status, test result and remaining live-automation/data-restoration limits in plain language.

## Acceptance review ledger

The matching gate ledger is `.unlazy/git-ci/GATES.md`. Use direct authorized verification commands above; do not install hooks or authorize an unlazy CHECK dispatcher on the user's behalf. Gate dispositions are manual reviews of saved evidence, not claims that a gate-check dispatcher ran.
