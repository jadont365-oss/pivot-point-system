"""Read-only export of frozen QQQ cash-open V3 pivots; no model/provider execution."""
import argparse
import copy
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import mean
import tempfile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
NY = ZoneInfo("America/New_York")
VERSION = "cash_touch_targets_cash_open_v3"
SCHEMA_VERSION = "1.0.0"
NAMES = ("S3", "S2", "S1", "P", "R1", "R2", "R3")
TARGETS = ("R1", "S1", "R2", "S2", "R3", "S3")
MULTIPLIERS = {name: (.38, .67, 1.0)[int(name[1]) - 1] for name in TARGETS}
COHORTS = {
    VERSION: ("development_week", "consumed_development", 5),
    "cash_touch_targets_month_2026_08": ("retrospective_month_replay", "previously_consumed_retrospective", 21),
    "cash_touch_targets_holdout_2025_09": ("first_unused_historical_holdout_at_evaluation", "now_consumed_once", 21),
    "cash_touch_targets_holdout_2025_07": ("first_unused_historical_holdout_at_evaluation", "now_consumed_once", 22),
}
METRICS = TARGETS + ("either_R1_S1", "both_R1_S1", "neither_R1_S1")
VARIANCE_DEFINITION = ("Sum squared log(close/previous_price) returns over cash one-minute bars, "
                       "starting with the first bar open; normalize each prior session by 390/its bar count.")
QUALITY_DEFINITION = ("Frozen nearest common CALL/PUT strike to prior cash close, expiry next cash session, "
                      "final prior cash minute; finite positive uncrossed bid/ask and IV<5; spread/mid<=0.20; "
                      "abs(iv_error)<=0.01; 0.10<=abs(delta)<=0.90; underlying timestamp within prior cash "
                      "session and underlying price within 1% of prior close. Full chain selection is not rerun by exporter.")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value, positive=False):
    require(type(value) in (int, float) and math.isfinite(value), "Expected finite numeric value")
    require(not positive or value > 0, "Expected positive numeric value")
    return value


def check_finite(value):
    if isinstance(value, float):
        require(math.isfinite(value), "Nonfinite value in record")
    elif isinstance(value, dict):
        for item in value.values():
            check_finite(item)
    elif isinstance(value, list):
        for item in value:
            check_finite(item)


def date_value(value):
    require(isinstance(value, str), "Date must be ISO text")
    parsed = dt.date.fromisoformat(value)
    require(parsed.isoformat() == value, "Date must have YYYY-MM-DD form")
    return parsed


def stamp(day, clock):
    return dt.datetime.combine(date_value(day), dt.time.fromisoformat(clock), NY)


def aware_time(value):
    parsed = dt.datetime.fromisoformat(value)
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None, "Timestamp needs timezone")
    return parsed


def close_number(actual, expected, label):
    number(actual)
    require(math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12), "Inconsistent " + label)


def cents(value):
    return float(Decimal(str(value)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_path(base, relative):
    require(isinstance(relative, str) and relative and "\\" not in relative, "Invalid relative path")
    relative_path = Path(relative)
    require(not relative_path.is_absolute() and ".." not in relative_path.parts, "Path must remain within root")
    resolved = (Path(base) / relative_path).resolve()
    require(resolved.is_relative_to(Path(base).resolve()), "Path escapes root")
    return resolved


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"),
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON: " + value)))


def summarize_evidence(evidence):
    require(set(evidence) == set(COHORTS), "Exactly four approved ThetaData cohorts are required")
    cohorts = []
    for cohort_id, (scope, consumed, expected_size) in COHORTS.items():
        report = evidence[cohort_id]
        daily = []
        for row in report["daily"]:
            day = row["date"]
            date_value(day)
            events = row["cash_open_anchored" if cohort_id == VERSION else "events"]
            touches = {name: events[name]["touched"] for name in TARGETS}
            require(all(type(v) is bool for v in touches.values()), "Touch events must be booleans")
            r1, s1 = touches["R1"], touches["S1"]
            outcome = dict(touches, either_R1_S1=r1 or s1, both_R1_S1=r1 and s1, neither_R1_S1=not (r1 or s1))
            if cohort_id != VERSION:
                for key, expected in {"R1_touch": r1, "S1_touch": s1, "either_touch": r1 or s1,
                                      "both_touch": r1 and s1, "neither_touch": not (r1 or s1)}.items():
                    require(type(row[key]) is bool and row[key] == expected, "Stored daily evidence disagrees")
            minutes = row.get("cash_minutes", 390)
            require(type(minutes) is int and minutes in (210, 390), "Unsupported cash duration")
            daily.append({"date": day, "cash_minutes": minutes,
                          "cash_end": (stamp(day, "09:30:00") + dt.timedelta(minutes=minutes)).isoformat(),
                          "outcomes": outcome})
        dates = [row["date"] for row in daily]
        require(dates == sorted(set(dates)) and len(dates) == expected_size, "Invalid evidence dates or cohort size")
        count = {name: sum(row["outcomes"][name] for row in daily) for name in METRICS}
        if cohort_id == VERSION:
            require(report["sessions"] == dates, "Development session mismatch")
            stored = report["target_touch_counts"]["cash_open_anchored"]
            require(all(stored[name] == count[name] for name in TARGETS), "Stored target counts disagree")
            require(report["either_R1_S1_days"] == count["either_R1_S1"] and
                    report["both_R1_S1_days"] == count["both_R1_S1"], "Stored aggregate counts disagree")
        else:
            require(report["sessions"] == len(daily), "Stored denominator disagrees")
            aliases = {"R1_touch": "R1", "S1_touch": "S1", "either_touch": "either_R1_S1",
                       "both_touch": "both_R1_S1", "neither_touch": "neither_R1_S1"}
            require(all(report["counts"][key] == count[name] for key, name in aliases.items()), "Stored counts disagree")
            if "rates" in report:
                for key, name in aliases.items():
                    if key in report["rates"]:
                        close_number(report["rates"][key], count[name] / len(daily), "stored measured rate")
        require(report["cash_minutes"] == sum(row["cash_minutes"] for row in daily), "Stored cash minutes disagree")
        cohorts.append({"id": cohort_id, "scope": scope, "consumed_status": consumed,
                        "provider": "ThetaData", "sample_size": len(daily), "evaluated_dates": dates,
                        "source_report_created_at": report.get("created_utc"),
                        "source_report_timestamp_status": "recorded" if report.get("created_utc") else "not_recorded",
                        "timing_status": "POSTHOC_NOT_AVAILABLE_AT_FORECAST",
                        "available_at_original_forecast": False, "counts_independently_recounted": True,
                        "count_crosscheck": "passed", "daily": daily,
                        "measured_rates": {name: {"count": count[name], "denominator": len(daily),
                                                   "rate": count[name] / len(daily)} for name in METRICS},
                        "denominator_definition": "All evaluated complete cash sessions in this cohort; P excluded."})
    return {"timing_status": "POSTHOC_NOT_AVAILABLE_AT_FORECAST", "available_at_original_forecast": False,
            "touch_definition": "One-minute cash bar overlaps fixed line with original numerical tolerance: low-1e-8 <= price <= high+1e-8; opening beyond alone is not a touch.",
            "cohorts": cohorts, "pooled_calibrated_probability": None,
            "interpretation": "Separate retrospective descriptive rates; not calibrated decision probabilities, fills, reversal or profit."}


def build_daily_record(forecast, options, evidence, provenance, generated_at):
    """Build a deterministic record from frozen mappings; inputs are never mutated.

    options maps prior dates to frozen V1 snapshots plus authoritative ISO
    prior_cash_open/prior_cash_close clocks (load_source_bundle supplies these).
    Missing optional quote detail is explicit; the 20 annual IV and variance
    values in forecast remain mandatory. A present snapshot needs both clocks.
    evidence maps the four COHORTS IDs to their original report mappings.
    provenance contains a sources list of project-relative paths and SHA-256 hashes.
    """
    try:
        check_finite(forecast)
        require(forecast["version"] == VERSION, "Wrong model version")
        require(forecast["current_input"] == "opening_price_only" and
                forecast["projection_kind"] == "retrospective_first_cash_open_proxy", "Wrong frozen input mode")
        day = forecast["target_date"]
        start = stamp(day, "09:30:00")
        dates, variances, ivs = (forecast[key] for key in ("input_dates", "prior_cash_variances_390", "prior_atm_ivs"))
        require(len(dates) == len(variances) == len(ivs) == 20, "Exactly 20 aligned prior inputs required")
        require(forecast["anchor_date"] == dates[-1] and forecast["reference_price"] == forecast["P"], "Frozen anchor metadata inconsistent")
        rows = []
        for prior_day, variance, iv in zip(dates, variances, ivs):
            snapshot = options.get(prior_day)
            if snapshot is not None:
                close_number(snapshot["annual_iv_decimal"], iv, "prior option IV")
            rows.append({"date": prior_day, "normalized_cash_log_return_variance_390": variance,
                         "annual_atm_iv_decimal": iv, "availability_assumed_at": stamp(prior_day, "17:15:00").isoformat(),
                         "option_snapshot_status": "available_frozen_snapshot" if snapshot else "missing_snapshot"})
        minutes = forecast["cash_minutes"]
        latest = options.get(dates[-1])
        pair = {"status": "available_frozen_snapshot" if latest else "missing_snapshot", "prior_session_date": dates[-1],
                "annual_iv_decimal": ivs[-1], "contracts": copy.deepcopy(latest["contracts"]) if latest else None,
                "source_file": latest["source_file"] if latest else None,
                "source_sha256": latest["source_sha256"] if latest else None,
                "prior_cash_open": latest["prior_cash_open"] if latest else None,
                "prior_cash_close": latest["prior_cash_close"] if latest else None,
                "raw_rows": latest.get("raw_rows") if latest else None,
                "eligible_rows": latest.get("eligible_rows") if latest else None,
                "selection_and_quality_definition": QUALITY_DEFINITION,
                "selection_status": "copied_frozen_selection_not_reselected" if latest else "missing",
                "units": {"bid_ask_strike_underlying_price": "USD_per_share", "implied_vol": "annual_decimal",
                          "delta": "dimensionless", "iv_error": "provider_raw_decimal_field"}}
        p = forecast["P"]
        calculation = {"scale_reference_price": forecast["scale_reference_price"],
                       "mean_normalized_cash_variance_390": mean(variances), "target_duration_factor": minutes / 390,
                       "target_cash_log_return_variance": mean(variances) * minutes / 390,
                       "historical_cash_sigma_dollars": forecast["history_cash_sigma_dollars"],
                       "latest_annual_atm_iv_decimal": ivs[-1], "mean_20_annual_atm_iv_decimal": mean(ivs),
                       "iv_ratio": forecast["iv_ratio"], "adjusted_cash_sigma_dollars": forecast["candidate_cash_sigma_dollars"],
                       "formula": "scale_reference_price * sqrt(mean_20_normalized_variance * cash_minutes / 390) * latest_IV / mean_20_IV",
                       "rounding": "Baseline prices rounded to cents with Decimal ROUND_HALF_UP, then Decimal translation by P-baseline_P; no second cent rounding.",
                       "baseline_levels": copy.deepcopy(forecast["baseline_levels"]),
                       "translation_delta_dollars": float(Decimal(str(p)) - Decimal(str(forecast["baseline_levels"]["P"])))}
        prices = {name: {"price": forecast["levels"][name],
                         "signed_offset_dollars": float(Decimal(str(forecast["levels"][name])) - Decimal(str(p))),
                         "multiplier": MULTIPLIERS.get(name),
                         "definition": "Observed opening anchor; automatic by definition, not a forecast target." if name == "P" else
                         "Fixed upper cash touch target." if name.startswith("R") else "Fixed lower cash touch target."}
                  for name in NAMES}
        record = {"schema_version": SCHEMA_VERSION, "model_version": VERSION,
                  "instrument": {"symbol": "QQQ", "asset_class": "equity_ETF", "currency": "USD"},
                  "session": {"date": day, "timezone": "America/New_York", "cash_start": start.isoformat(),
                              "cash_end": (start + dt.timedelta(minutes=minutes)).isoformat(),
                              "cash_end_exclusive": True, "cash_minutes": minutes},
                  "timing": {"forecast_as_of": forecast["as_of"], "prior_distances_as_of": forecast["prior_distances_as_of"],
                             "exported_at_utc": aware_time(generated_at).astimezone(dt.timezone.utc).isoformat(),
                             "forecast_publication_status": "reconstructed_not_historically_published",
                             "prior_input_availability_status": "assumed_17_15_Eastern_not_observed_receipt",
                             "archive_vintage": "retrospective_latest"},
                  "forecast": {"anchor": {"name": "P", "price": p, "observed_cash_open": forecast["observed_cash_open"],
                                          "definition": "First cash-minute opening price proxy, normalized to eight decimal places.",
                                          "timestamp_kind": forecast.get("opening_source", {}).get("timestamp_kind", "unknown")},
                               "levels": prices, "price_units": "USD_per_share",
                               "inputs": {"lookback_sessions": 20, "prior_sessions": rows,
                                          "variance_definition": VARIANCE_DEFINITION, "variance_units": "squared_log_return",
                                          "iv_definition": "Arithmetic mean annual decimal IV of frozen qualified near-ATM call and put.",
                                          "iv_units": "annual_decimal", "current_session_input": "opening_price_only",
                                          "inputs_not_used": ["volume", "other_Greeks", "profile_levels", "VWAP", "ADX", "RVOL", "composite_features"]},
                               "calculation": calculation, "latest_option_pair": pair,
                               "probabilities": {"empirical_touch_probability": None,
                                                 "empirical_probability_status": "not_calibrated",
                                                 "either_side_touch_probability": None,
                                                 "theoretical_single_side_touch": {"formula": "2 * (1 - Phi(z)) = erfc(z/sqrt(2))",
                                                                                   "scope": "one_upper_or_one_lower_fixed_level",
                                                                                   "assumptions": "Continuous zero-drift constant-volatility Brownian motion starts at P; sigma treated as cash-session standard deviation; ignores discreteness and market dynamics.",
                                                                                   "z_definition": "Frozen multiplier label, before cent rounding; not a calibrated market probability.",
                                                                                   "rates": {name: math.erfc(z / math.sqrt(2)) for name, z in MULTIPLIERS.items()}}}},
                  "research_evidence": summarize_evidence(evidence),
                  "data_quality": {"archive_status": "frozen_retrospective_ThetaData", "live_freshness": "unverified",
                                   "event_age_seconds": None, "historical_publication_vintage": "unknown",
                                   "exact_tick_execution": "unverified", "missing_information": ["historical_receipt_time", "certified_quote_event_age", "live_freshness", "calibrated_market_touch_probability"] +
                                   (["latest_option_quote_snapshot"] if latest is None else [])},
                  "provenance": copy.deepcopy(provenance),
                  "llm_guidance": ["Explain these deterministic prices and offsets without recomputing, moving or optimizing levels.",
                                   "P is an observed cash-open anchor, automatically reached by definition; do not score it as predictive success.",
                                   "All evidence cohorts are posthoc and were not available at the original forecast clock; keep them separate.",
                                   "Theory describes one upper OR one lower line separately; never turn it into either-side odds or calibrated market probability.",
                                   "A bar touch does not establish an exact execution, reversal, containment, profit or live readiness.",
                                   "Use explicit missing and unknown states; do not invent quote freshness, omitted composite inputs or probabilities."]}
        validate_record(record)
        return record
    except (KeyError, TypeError, IndexError, OverflowError) as exc:
        raise ValueError("Missing or malformed core source input: " + str(exc)) from exc


def _validate_record(record):
    check_finite(record)
    require(record["schema_version"] == SCHEMA_VERSION and record["model_version"] == VERSION, "Wrong record/model version")
    require(record["instrument"] == {"symbol": "QQQ", "asset_class": "equity_ETF", "currency": "USD"}, "Wrong instrument")
    session, timing, forecast = (record[k] for k in ("session", "timing", "forecast"))
    day = session["date"]
    start = stamp(day, "09:30:00")
    minutes = session["cash_minutes"]
    require(type(minutes) is int and minutes in (210, 390), "Unsupported cash duration")
    require(session["timezone"] == "America/New_York" and session["cash_end_exclusive"] is True, "Wrong cash-window definition")
    require(session["cash_start"] == start.isoformat() and session["cash_end"] ==
            (start + dt.timedelta(minutes=minutes)).isoformat(), "Cash session timing mismatch")
    as_of = aware_time(timing["forecast_as_of"])
    require(start <= as_of < start + dt.timedelta(minutes=1) and as_of.utcoffset() == start.utcoffset(), "Wrong opening observation time")
    distances_at = aware_time(timing["prior_distances_as_of"])
    require(distances_at == stamp(day, "09:25:00") and distances_at.utcoffset() == start.utcoffset(), "Wrong prior distance clock")
    export_time = aware_time(timing["exported_at_utc"])
    require(export_time.utcoffset() == dt.timedelta(0) and export_time >= as_of, "Wrong actual export clock")
    require(timing["forecast_publication_status"] == "reconstructed_not_historically_published" and
            timing["prior_input_availability_status"] == "assumed_17_15_Eastern_not_observed_receipt" and
            timing["archive_vintage"] == "retrospective_latest", "Historical availability must remain explicit")
    require(set(forecast) == {"anchor", "levels", "price_units", "inputs", "calculation", "latest_option_pair", "probabilities"}, "Unexpected or missing forecast fields")
    require(forecast["price_units"] == "USD_per_share", "Wrong price units")
    p = number(forecast["anchor"]["price"], positive=True)
    observed = number(forecast["anchor"]["observed_cash_open"], positive=True)
    require(forecast["anchor"]["name"] == "P" and p == float(Decimal(str(observed)).quantize(Decimal(".00000001"))), "P does not match normalized opening price")
    inputs = forecast["inputs"]
    require(inputs["lookback_sessions"] == 20 and inputs["current_session_input"] == "opening_price_only", "Wrong input scope")
    require(inputs["variance_units"] == "squared_log_return" and inputs["iv_units"] == "annual_decimal", "Wrong input units")
    rows = inputs["prior_sessions"]
    require(isinstance(rows, list) and len(rows) == 20, "Exactly 20 prior sessions required")
    dates = [row["date"] for row in rows]
    require(dates == sorted(set(dates)) and all(date_value(d) < date_value(day) for d in dates), "Prior chronology invalid")
    for row in rows:
        require(row["availability_assumed_at"] == stamp(row["date"], "17:15:00").isoformat() and
                aware_time(row["availability_assumed_at"]) < distances_at, "Prior availability assumption invalid")
        require(row["option_snapshot_status"] in ("available_frozen_snapshot", "missing_snapshot"), "Wrong prior snapshot state")
    variances = [number(row["normalized_cash_log_return_variance_390"], positive=True) for row in rows]
    ivs = [number(row["annual_atm_iv_decimal"], positive=True) for row in rows]
    require(all(iv < 5 for iv in ivs), "Annual decimal IV outside frozen quality range")
    c = forecast["calculation"]
    reference = number(c["scale_reference_price"], positive=True)
    variance = mean(variances) * minutes / 390
    history_sigma = reference * math.sqrt(variance)
    ratio = ivs[-1] / mean(ivs)
    sigma = history_sigma * ratio
    for key, expected in {"mean_normalized_cash_variance_390": mean(variances), "target_duration_factor": minutes / 390,
                          "target_cash_log_return_variance": variance, "historical_cash_sigma_dollars": history_sigma,
                          "latest_annual_atm_iv_decimal": ivs[-1], "mean_20_annual_atm_iv_decimal": mean(ivs),
                          "iv_ratio": ratio, "adjusted_cash_sigma_dollars": sigma}.items():
        close_number(c[key], expected, key)
    baseline = c["baseline_levels"]
    require(set(baseline) == set(NAMES) and baseline["P"] == cents(reference), "Invalid baseline anchor")
    delta = Decimal(str(p)) - Decimal(str(baseline["P"]))
    require(c["translation_delta_dollars"] == float(delta), "Wrong translation delta")
    levels = forecast["levels"]
    require(set(levels) == set(NAMES), "Exactly seven levels required")
    for name in NAMES:
        level = levels[name]
        number(level["price"], positive=True)
        number(baseline[name], positive=True)
        if name != "P":
            number(level["multiplier"], positive=True)
        if name != "P":
            require(baseline[name] == cents(reference + (1 if name.startswith("R") else -1) * MULTIPLIERS[name] * sigma), "Baseline rounding/scale inconsistent")
        require(level["price"] == float(Decimal(str(baseline[name])) + delta), "Translated frozen level inconsistent")
        require(level["signed_offset_dollars"] == float(Decimal(str(level["price"])) - Decimal(str(p))) and
                level["multiplier"] == MULTIPLIERS.get(name), "Wrong offset or multiplier")
    require(all(levels[a]["price"] < levels[b]["price"] for a, b in zip(NAMES, NAMES[1:])), "Levels collide or are unordered")
    probabilities = forecast["probabilities"]
    require(probabilities["empirical_touch_probability"] is None and probabilities["either_side_touch_probability"] is None and
            probabilities["empirical_probability_status"] == "not_calibrated", "Uncalibrated probabilities must be null")
    theory = probabilities["theoretical_single_side_touch"]
    require(theory["scope"] == "one_upper_or_one_lower_fixed_level" and set(theory["rates"]) == set(TARGETS), "Wrong theoretical probability scope")
    for name, multiplier in MULTIPLIERS.items():
        close_number(theory["rates"][name], math.erfc(multiplier / math.sqrt(2)), "single-side theoretical probability")
    pair = forecast["latest_option_pair"]
    require(pair["prior_session_date"] == dates[-1] and pair["annual_iv_decimal"] == ivs[-1], "Wrong latest option input")
    if pair["status"] == "missing_snapshot":
        require(all(pair[key] is None for key in ("contracts", "source_file", "source_sha256", "raw_rows", "eligible_rows", "prior_cash_open", "prior_cash_close")), "Missing snapshot cannot contain invented details")
    else:
        require(pair["status"] == "available_frozen_snapshot", "Unknown option state")
        prior_open, prior_close = aware_time(pair["prior_cash_open"]), aware_time(pair["prior_cash_close"])
        expected_open = stamp(dates[-1], "09:30:00")
        require(prior_open == expected_open and prior_open.utcoffset() == expected_open.utcoffset() and
                prior_close.utcoffset() == expected_open.utcoffset() and
                (prior_close - prior_open).total_seconds() / 60 in (210, 390), "Invalid authoritative prior cash window")
        final_minute = prior_close - dt.timedelta(minutes=1)
        contracts = pair["contracts"]
        require(isinstance(contracts, list) and len(contracts) == 2 and {q["right"] for q in contracts} == {"CALL", "PUT"}, "Qualifying CALL/PUT pair required")
        require(contracts[0]["strike"] == contracts[1]["strike"] and contracts[0]["timestamp"] == contracts[1]["timestamp"], "Option pair is inconsistent")
        require(type(pair["raw_rows"]) is int and type(pair["eligible_rows"]) is int and pair["raw_rows"] >= pair["eligible_rows"] >= 2, "Invalid snapshot row counts")
        for q in contracts:
            for key in ("bid", "ask", "implied_vol", "strike", "underlying_price"):
                number(q[key], positive=True)
            number(q["delta"])
            number(q["iv_error"])
            quote_at, underlying_at = aware_time(q["timestamp"]), aware_time(q["underlying_timestamp"])
            require(quote_at == final_minute and prior_open <= underlying_at <= quote_at, "Option quote must equal authoritative final cash minute")
            prior_offset = stamp(dates[-1], "09:30:00").utcoffset()
            require(quote_at.utcoffset() == underlying_at.utcoffset() == prior_offset and
                    q["symbol"] == "QQQ" and q["expiration"] == day, "Option date/symbol mismatch")
            require(0 < q["bid"] <= q["ask"] and q["implied_vol"] < 5 and .10 <= abs(q["delta"]) <= .90 and
                    abs(q["iv_error"]) <= .01 and (q["ask"] - q["bid"]) / ((q["ask"] + q["bid"]) / 2) <= .20 and
                    abs(q["underlying_price"] / reference - 1) <= .01, "Frozen option quality failed")
        close_number(mean(q["implied_vol"] for q in contracts), ivs[-1], "latest pair IV average")
    evidence = record["research_evidence"]
    require(evidence["timing_status"] == "POSTHOC_NOT_AVAILABLE_AT_FORECAST" and
            evidence["available_at_original_forecast"] is False and evidence["pooled_calibrated_probability"] is None, "Evidence chronology/calibration invalid")
    require([c["id"] for c in evidence["cohorts"]] == list(COHORTS), "Evidence cohort scope changed")
    for cohort in evidence["cohorts"]:
        scope, consumed, size = COHORTS[cohort["id"]]
        daily = cohort["daily"]
        require(cohort["provider"] == "ThetaData" and cohort["scope"] == scope and cohort["consumed_status"] == consumed, "Cohort definition changed")
        require(cohort["available_at_original_forecast"] is False and cohort["timing_status"] == evidence["timing_status"] and
                cohort["counts_independently_recounted"] is True and cohort["count_crosscheck"] == "passed", "Evidence availability invalid")
        require(len(daily) == cohort["sample_size"] == size and cohort["evaluated_dates"] == [r["date"] for r in daily], "Evidence denominator/dates invalid")
        require(cohort["evaluated_dates"] == sorted(set(cohort["evaluated_dates"])), "Evidence dates unordered")
        for row in daily:
            require(type(row["cash_minutes"]) is int and row["cash_minutes"] in (210, 390) and row["cash_end"] ==
                    (stamp(row["date"], "09:30:00") + dt.timedelta(minutes=row["cash_minutes"])).isoformat(), "Evidence cash timing invalid")
            outcomes = row["outcomes"]
            require(set(outcomes) == set(METRICS) and all(type(v) is bool for v in outcomes.values()), "Evidence outcomes invalid")
            r1, s1 = outcomes["R1"], outcomes["S1"]
            require(outcomes["either_R1_S1"] == (r1 or s1) and outcomes["both_R1_S1"] == (r1 and s1) and
                    outcomes["neither_R1_S1"] == (not (r1 or s1)), "Evidence aggregate logic invalid")
        require(set(cohort["measured_rates"]) == set(METRICS), "Missing evidence metrics")
        for name in METRICS:
            metric = cohort["measured_rates"][name]
            count = sum(row["outcomes"][name] for row in daily)
            require(type(metric["count"]) is int and type(metric["denominator"]) is int and
                    metric["count"] == count and metric["denominator"] == size and size > 0, "Evidence numerator/denominator invalid")
            close_number(metric["rate"], count / size, "measured descriptive rate")
    quality = record["data_quality"]
    require(quality["live_freshness"] == "unverified" and quality["event_age_seconds"] is None and
            quality["historical_publication_vintage"] == "unknown" and quality["exact_tick_execution"] == "unverified", "Unsupported data freshness/execution claim")
    sources = record["provenance"]["sources"]
    require(isinstance(sources, list) and sources, "Source provenance required")
    paths = []
    for source in sources:
        safe_path(ROOT, source["path"])
        digest = source["sha256"]
        require(isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest), "Invalid SHA-256")
        paths.append(source["path"])
    require(len(paths) == len(set(paths)), "Duplicate source provenance")
    if pair["status"] == "available_frozen_snapshot":
        require(any(s["path"] == pair["source_file"] and s["sha256"] == pair["source_sha256"] for s in sources), "Latest option file must be hash bound")


def validate_record(record):
    """Semantic validation with no I/O or third-party runtime dependency."""
    try:
        _validate_record(record)
    except (KeyError, TypeError, IndexError, OverflowError) as exc:
        raise ValueError("Missing or malformed record field: " + str(exc)) from exc
    return None


def load_source_bundle():
    forecasts_path = ROOT / "outputs" / VERSION / "forecasts.json"
    inputs_path = ROOT / "outputs/cash_touch_targets_v1/inputs.json"
    forecasts, inputs = read_json(forecasts_path), read_json(inputs_path)
    evidence = {key: read_json(ROOT / "outputs" / key / "report.json") for key in COHORTS}
    source_paths = {}
    def bind(path, role, expected=None):
        relative = Path(path).as_posix()
        actual = sha256_file(safe_path(ROOT, relative))
        require(expected is None or actual == expected, "Frozen source hash changed: " + relative)
        source_paths[relative] = {"path": relative, "sha256": actual, "role": role}
    for folder in ("cash_touch_targets_v1", "cash_touch_targets_composite_p_v2", *COHORTS):
        for filename in ("forecasts.json", "report.json", "inputs.json", "contract.json", "candidate_freeze.json", "run_test.py", "run_month.py"):
            relative = f"outputs/{folder}/{filename}"
            if (ROOT / relative).is_file():
                bind(relative, "frozen_model_or_evidence")
    manifest = {entry["file"]: entry["sha256"] for entry in inputs["source_manifest"]}
    roster_path = "outputs/qqq_range/sessions.json"
    require(roster_path in manifest, "Frozen session roster hash missing")
    bind(roster_path, "frozen_session_roster", manifest[roster_path])
    roster_rows = read_json(ROOT / roster_path)
    roster = {row["date"]: row for row in roster_rows}
    require(len(roster) == len(roster_rows), "Duplicate authoritative session date")
    options = copy.deepcopy(inputs["options"])
    dates = sorted({day for f in forecasts for day in f["input_dates"]})
    for day in dates:
        option = options.get(day)
        if option:
            require(day in roster, "Authoritative prior session clock missing: " + day)
            option["prior_cash_open"] = stamp(day, roster[day]["open"]).isoformat()
            option["prior_cash_close"] = stamp(day, roster[day]["close"]).isoformat()
            bind(option["source_file"], "frozen_option_archive", option["source_sha256"])
    for path, digest in manifest.items():
        if "prices_" in path or path.endswith("cash_option_collection.json"):
            bind(path, "frozen_price_archive_or_collection", digest)
    for forecast in forecasts:
        path = forecast.get("opening_source", {}).get("file")
        if path:
            bind(path, "frozen_opening_price_archive", manifest.get(path))
    for filename in ("export_daily.py", "schema.json"):
        bind((HERE / filename).relative_to(ROOT), "exporter_code_or_schema")
    return forecasts, options, evidence, {"sources": [source_paths[p] for p in sorted(source_paths)],
                                                    "hash_algorithm": "SHA-256",
                                                    "path_base": "project_workspace_root",
                                                    "source_binding_status": "verified_at_export"}


def atomic_json(path, value):
    serialized = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def verify_outputs(out_dir, selected_date=None):
    """Verify manifest, current source hashes, schema and semantics without writes."""
    from jsonschema import Draft202012Validator, FormatChecker
    manifest = read_json(out_dir / "manifest.json")
    require(manifest["schema_version"] == SCHEMA_VERSION and manifest["model_version"] == VERSION, "Wrong manifest version")
    require(manifest["exports"], "Empty export manifest")
    for source in manifest["sources"]:
        require(sha256_file(safe_path(ROOT, source["path"])) == source["sha256"], "Source/code hash mismatch: " + source["path"])
    schema = read_json(HERE / "schema.json")
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    forecasts, options, evidence, provenance = load_source_bundle()
    original = {f["target_date"]: f for f in forecasts}
    require(manifest["sources"] == provenance["sources"], "Manifest source binding differs from current sources")
    exported_dates = []
    for entry in manifest["exports"]:
        path = safe_path(out_dir, entry["path"])
        require(entry["path"] == f"daily/QQQ_{entry['date']}.json", "Unexpected export path")
        require(entry["date"] in original and entry["date"] not in exported_dates, "Unknown or duplicate export date")
        exported_dates.append(entry["date"])
        require(sha256_file(path) == entry["sha256"], "Export hash mismatch: " + entry["path"])
        record = read_json(path)
        validator.validate(record)
        validate_record(record)
        expected = build_daily_record(original[entry["date"]], options, evidence, provenance, record["timing"]["exported_at_utc"])
        require(record == expected, "Record differs from authoritative frozen input/evidence")
    require(selected_date is None or selected_date in exported_dates, "Requested date absent from manifest")
    preserved = HERE / "preservation_before.json"
    if preserved.exists():
        for path, digest in read_json(preserved)["files"].items():
            require(sha256_file(safe_path(ROOT, path)) == digest, "Original artifact changed: " + path)
    return len(exported_dates)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="One of the five frozen YYYY-MM-DD sessions")
    parser.add_argument("--out-dir", type=Path, default=HERE)
    parser.add_argument("--verify", action="store_true", help="Read-only integrity, schema and semantic checks")
    args = parser.parse_args(argv)
    if args.date:
        date_value(args.date)
    out_dir = args.out_dir.resolve()
    if args.verify:
        count = verify_outputs(out_dir, args.date)
        print(json.dumps({"status": "VERIFIED", "verified_exports": count, "mutation": False}))
        return 0
    protected = [(ROOT / "outputs" / key).resolve() for key in (*COHORTS, "cash_touch_targets_v1", "cash_touch_targets_composite_p_v2")]
    require(not any(out_dir == p or out_dir.is_relative_to(p) for p in protected), "Output directory must be separate from frozen source folders")
    forecasts, options, evidence, provenance = load_source_bundle()
    selected = [f for f in forecasts if args.date is None or f["target_date"] == args.date]
    require(selected, "Requested date is not one of the frozen V3 sessions")
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    records = [build_daily_record(f, options, evidence, provenance, generated_at) for f in selected]
    exports = []
    for record in records:
        day = record["session"]["date"]
        relative = f"daily/QQQ_{day}.json"
        path = safe_path(out_dir, relative)
        atomic_json(path, record)
        exports.append({"date": day, "path": relative, "sha256": sha256_file(path)})
    atomic_json(out_dir / "manifest.json", {"schema_version": SCHEMA_VERSION, "model_version": VERSION,
                                           "generated_at_utc": generated_at, "exports": exports,
                                           "sources": provenance["sources"], "scope": "selected_frozen_V3_sessions"})
    print(json.dumps({"exported": len(exports), "out_dir": str(out_dir)}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        raise SystemExit("Export failed: " + str(error)) from error
