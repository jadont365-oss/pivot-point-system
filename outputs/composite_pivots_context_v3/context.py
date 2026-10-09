"""Pure, prior-information-only contextual adjustments to full-composite v1.

The original v1 record is the input contract. No market data, calendar lookup,
outcome loading, file access, or input mutation occurs in this module.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import date, datetime, time
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
import math
import re
from zoneinfo import ZoneInfo

import numpy as np


NY = ZoneInfo("America/New_York")
FEATURE_NAMES = (
    "value_position_z",
    "value_overlap_fraction", "node_stack_strength", "trend_direction_z",
    "vwap_slope_z", "adx_norm", "log_rvol", "trend_activity_interaction",
    "log_iv_hv_ratio", "put_call_iv_skew", "option_concentration_hhi",
)
LEVEL_NAMES = ("S3", "S2", "S1", "P", "R1", "R2", "R3")
FIT_START = date(2025, 11, 5)
INITIAL_FIT_END = date(2026, 7, 31)
VALIDATION_START = date(2026, 8, 3)
FIT_END = date(2026, 9, 25)
ALPHAS = (1, 10, 100)
LOG_WIDTH_CAP = math.log(2.0)


class ContextError(ValueError):
    """An input fails the frozen causal or numerical contract."""


def _require(condition, message):
    if not condition:
        raise ContextError(message)


def _number(value, name, *, positive=False, nonnegative=False):
    _require(not isinstance(value, (bool, np.bool_)), f"{name}: boolean is not numeric")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContextError(f"{name}: finite number required") from exc
    _require(math.isfinite(result), f"{name}: finite number required")
    _require(not positive or result > 0, f"{name}: positive number required")
    _require(not nonnegative or result >= 0, f"{name}: nonnegative number required")
    return result


def _date(value, name):
    if isinstance(value, datetime):
        raise ContextError(f"{name}: date, not datetime, required")
    if isinstance(value, date):
        return value
    try:
        result = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ContextError(f"{name}: ISO date required") from exc
    _require(result.isoformat() == value, f"{name}: canonical ISO date required")
    return result


def _timestamp(value, name):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ContextError(f"{name}: aware ISO timestamp required") from exc
    _require(result.tzinfo is not None and result.utcoffset() is not None,
             f"{name}: timezone required")
    return result.astimezone(NY)


def _field(mapping, key, name):
    _require(isinstance(mapping, dict) and key in mapping, f"{name}.{key}: required")
    return mapping[key]


def _profile(record, name):
    values = tuple(_number(_field(record, key, name), f"{name}.{key}", positive=True)
                   for key in ("val", "poc", "vah"))
    _require(values[0] <= values[1] <= values[2], f"{name}: unordered value area")
    return values


def _prior_timestamp(value, name, anchor_date):
    timestamp = _timestamp(value, name)
    _require(timestamp.date() <= anchor_date, f"{name}: target/future timestamp")
    return timestamp


def _validate_base(base):
    _require(isinstance(base, dict), "base: record required")
    _require(base.get("symbol") == "QQQ", "base.symbol: QQQ required")
    _require(base.get("mode") == "full_composite_v1", "base.mode: full_composite_v1 required")
    target = _date(_field(base, "target_date", "base"), "base.target_date")
    anchor = _date(_field(base, "anchor_date", "base"), "base.anchor_date")
    as_of = _timestamp(_field(base, "as_of", "base"), "base.as_of")
    _require(as_of.date() == target and as_of.time().replace(tzinfo=None) == time(8),
             "base.as_of: target date 08:00:00 Eastern required")
    _require(anchor < target, "base.anchor_date: prior session required")
    cash_session = _field(base, "prior_cash_session", "base")
    _require(_date(_field(cash_session, "date", "prior_cash_session"), "prior_cash_session.date") == anchor,
             "prior_cash_session.date: anchor date required")
    _require(cash_session.get("timezone") == "America/New_York",
             "prior_cash_session.timezone: America/New_York required")
    cash_open = _timestamp(_field(cash_session, "open", "prior_cash_session"), "prior_cash_session.open")
    cash_close = _timestamp(_field(cash_session, "close", "prior_cash_session"), "prior_cash_session.close")
    _require(cash_open.date() == anchor and cash_close.date() == anchor and
             cash_open.time().replace(tzinfo=None) == time(9, 30) and cash_open < cash_close and
             cash_close.time().replace(tzinfo=None) <= time(16),
             "prior_cash_session: valid completed anchor cash interval required")
    for key in ("method_sha256", "engine_sha256"):
        _require(isinstance(base.get(key), str) and
                 re.fullmatch(r"[0-9a-fA-F]{64}", base[key]) is not None,
                 f"base.{key}: SHA256 required")
    scale = _number(_field(base, "adjusted_scale", "base"), "base.adjusted_scale", positive=True)
    gap = _number(_field(base, "minimum_gap", "base"), "base.minimum_gap", positive=True)
    levels_record = _field(base, "levels", "base")
    _require(isinstance(levels_record, dict) and set(levels_record) == set(LEVEL_NAMES),
             "base.levels: exactly seven original levels required")
    levels = [_number(levels_record[key], f"base.levels.{key}", positive=True)
              for key in LEVEL_NAMES]
    _require(all(left < right for left, right in zip(levels, levels[1:])),
             "base.levels: strict original order required")
    inputs = _field(base, "inputs", "base")
    sessions = _field(inputs, "source_session_features", "base.inputs")
    _require(isinstance(sessions, list) and len(sessions) == 5,
             "source_session_features: five prior sessions required")
    dates = []
    for index, session in enumerate(sessions):
        name = f"source_session_features[{index}]"
        day = _date(_field(session, "date", name), f"{name}.date")
        _require(day < target, f"{name}: target/future session")
        dates.append(day)
        _number(_field(session, "cash_close", name), f"{name}.cash_close", positive=True)
        close_at = _prior_timestamp(_field(session, "cash_close_at", name), f"{name}.cash_close_at", anchor)
        _require(close_at.date() == day, f"{name}.cash_close_at: source date mismatch")
        _require(time(9, 30) <= close_at.time().replace(tzinfo=None) <= time(16) and close_at <= cash_close,
                 f"{name}.cash_close_at: completed cash session required")
        _number(_field(session, "vwap", name), f"{name}.vwap", positive=True)
        _number(_field(session, "sigma", name), f"{name}.sigma", nonnegative=True)
        _profile(_field(session, "profile", name), f"{name}.profile")
    _require(dates == sorted(set(dates)) and dates[-1] == anchor,
             "source_session_features: ascending unique dates ending at anchor required")
    spot = _number(_field(inputs, "spot", "base.inputs"), "inputs.spot", positive=True)
    _require(math.isclose(spot, float(sessions[-1]["cash_close"]), rel_tol=1e-10, abs_tol=1e-8),
             "inputs.spot: must equal prior anchor cash close")
    _number(_field(inputs, "hv", "base.inputs"), "inputs.hv", positive=True)
    _number(_field(inputs, "adx", "base.inputs"), "inputs.adx", nonnegative=True)
    _number(_field(inputs, "rvol", "base.inputs"), "inputs.rvol", nonnegative=True)
    _number(_field(inputs, "recency_vwap", "base.inputs"), "inputs.recency_vwap", positive=True)
    _profile(_field(inputs, "pooled_profile", "base.inputs"), "inputs.pooled_profile")
    nodes = _field(inputs, "profile_nodes", "base.inputs")
    _require(isinstance(nodes, list) and bool(nodes), "inputs.profile_nodes: nodes required")
    for index, node in enumerate(nodes):
        name = f"profile_nodes[{index}]"
        _number(_field(node, "price", name), f"{name}.price", positive=True)
        _number(_field(node, "weight", name), f"{name}.weight", nonnegative=True)
        _require(_date(_field(node, "date", name), f"{name}.date") in dates,
                 f"{name}.date: one of five prior sessions required")
    _number(sum(float(node["weight"]) for node in nodes),
            "inputs.profile_nodes total weight", positive=True)
    vwap_nodes = _field(inputs, "vwap_nodes", "base.inputs")
    _require(isinstance(vwap_nodes, list) and bool(vwap_nodes), "inputs.vwap_nodes: original nodes required")
    for index, node in enumerate(vwap_nodes):
        name = f"vwap_nodes[{index}]"
        _number(_field(node, "price", name), f"{name}.price", positive=True)
        _number(_field(node, "weight", name), f"{name}.weight", nonnegative=True)
        if "date" in node:
            _require(_date(node["date"], f"{name}.date") in dates, f"{name}: non-prior date")
    options = _field(inputs, "options", "base.inputs")
    _number(_field(options, "iv", "inputs.options"), "options.iv", positive=True)
    _number(_field(options, "center", "inputs.options"), "options.center", positive=True)
    _number(_field(options, "straddle", "inputs.options"), "options.straddle", positive=True)
    _number(_field(options, "calendar_days", "inputs.options"), "options.calendar_days", positive=True)
    option_nodes = _field(options, "nodes", "inputs.options")
    _require(isinstance(option_nodes, list) and bool(option_nodes), "options.nodes: nodes required")
    for index, node in enumerate(option_nodes):
        name = f"options.nodes[{index}]"
        _number(_field(node, "price", name), f"{name}.price", positive=True)
        _number(_field(node, "weight", name), f"{name}.weight", nonnegative=True)
    _number(sum(float(node["weight"]) for node in option_nodes),
            "options.nodes unsigned total weight", positive=True)
    contracts = _field(options, "eligible_contracts", "inputs.options")
    _require(isinstance(contracts, list) and bool(contracts), "options.eligible_contracts: required")
    for index, contract in enumerate(contracts):
        name = f"eligible_contracts[{index}]"
        _require(contract.get("right") in ("CALL", "PUT"), f"{name}.right: CALL/PUT required")
        _number(_field(contract, "strike", name), f"{name}.strike", positive=True)
        delta = _number(_field(contract, "delta", name), f"{name}.delta")
        _require(abs(delta) <= 1, f"{name}.delta: outside [-1,1]")
        _number(_field(contract, "implied_vol_decimal", name), f"{name}.implied_vol_decimal", positive=True)
        for key in ("gamma", "vega", "open_interest"):
            _number(_field(contract, key, name), f"{name}.{key}", nonnegative=True)
        for key in ("quote_timestamp", "underlying_timestamp"):
            timestamp = _timestamp(_field(contract, key, name), f"{name}.{key}")
            _require(cash_open <= timestamp <= cash_close,
                     f"{name}.{key}: completed anchor cash session required")
        oi_at = _timestamp(_field(contract, "oi_timestamp", name), f"{name}.oi_timestamp")
        _require(oi_at <= cash_close, f"{name}.oi_timestamp: unavailable at prior cash close")
    _require({contract["right"] for contract in contracts} == {"CALL", "PUT"},
             "options.eligible_contracts: both CALL and PUT required for skew")
    return target, anchor, as_of, scale, gap, levels, inputs


def _clip(value, lower, upper):
    return min(upper, max(lower, value))


def _gaussian(distance):
    # Exp(-0.5*d*d) is effectively zero here; avoid overflow of d*d.
    return 0.0 if abs(distance) > 38 else math.exp(-0.5 * distance * distance)


def _feature_values(base):
    target, anchor, as_of, scale, gap, levels, inputs = _validate_base(base)
    reference_price = float(inputs["spot"])
    sessions = inputs["source_session_features"]
    anchor_session = sessions[-1]
    val, poc, vah = _profile(inputs["pooled_profile"], "inputs.pooled_profile")
    trend = _clip((float(anchor_session["cash_close"]) - float(anchor_session["vwap"])) /
                  max(float(anchor_session["sigma"]), 0.01), -3, 3)
    adx = _clip(float(inputs["adx"]) / 25, 0, 4)
    log_rvol = _clip(math.log(max(float(inputs["rvol"]), 0.05)), -2, 2)
    node_total = sum(float(node["weight"]) for node in inputs["profile_nodes"])
    node_stack = sum(float(node["weight"]) / node_total *
                     _gaussian((float(node["price"]) - reference_price) / (0.35 * scale))
                     for node in inputs["profile_nodes"])
    options = inputs["options"]
    nearest = {}
    for right in ("CALL", "PUT"):
        nearest[right] = min((contract for contract in options["eligible_contracts"] if contract["right"] == right),
                             key=lambda contract: (abs(abs(Decimal(str(contract["delta"]))) - Decimal("0.25")),
                                                   Decimal(str(contract["strike"]))))
    skew = (float(nearest["PUT"]["implied_vol_decimal"]) -
            float(nearest["CALL"]["implied_vol_decimal"])) / float(options["iv"])
    option_total = sum(float(node["weight"]) for node in options["nodes"])
    values = (
        _clip((reference_price - poc) / scale, -3, 3),
        sum(float(session["profile"]["val"]) <= reference_price <= float(session["profile"]["vah"])
            for session in sessions) / 5,
        node_stack, trend,
        _clip((float(sessions[-1]["vwap"]) - float(sessions[0]["vwap"])) / scale, -3, 3),
        adx, log_rvol, _clip(trend * adx * log_rvol, -6, 6),
        _clip(math.log((float(options["iv"]) / math.sqrt(365)) / max(float(inputs["hv"]), 0.000001)), -2, 2),
        _clip(skew, -2, 2),
        sum((float(node["weight"]) / option_total) ** 2 for node in options["nodes"]),
    )
    _require(all(math.isfinite(value) for value in values), "features: nonfinite calculation")
    return OrderedDict(zip(FEATURE_NAMES, values))


def features(base_v1):
    """Return 11 ordered features using only completed prior cash-session inputs."""
    return _feature_values(base_v1)


def _feature_vector(record):
    _require(isinstance(record, dict) and tuple(record) == FEATURE_NAMES,
             "features: exact frozen name order required")
    return [_number(record[name], f"features.{name}") for name in FEATURE_NAMES]


def _validated_rows(rows, fit_end):
    end = _date(fit_end, "fit_end")
    _require(FIT_START <= end <= FIT_END, "fit_end: outside frozen pre-retest fitting period")
    _require(isinstance(rows, (list, tuple)) and bool(rows), "rows: nonempty sequence required")
    result = []
    for index, row in enumerate(rows):
        name = f"rows[{index}]"
        _require(isinstance(row, dict), f"{name}: record required")
        _require(row.get("fit_eligible", True) is True, f"{name}: not fit eligible")
        day = _date(_field(row, "date", name), f"{name}.date")
        _require(FIT_START <= day <= end, f"{name}: date outside fit period")
        available = _timestamp(_field(row, "label_available_at", name), f"{name}.label_available_at")
        _require(available.date() == day and available.time().replace(tzinfo=None) == time(17, 15),
                 f"{name}: label must be available at 17:15 Eastern on same date")
        targets = _field(row, "targets", name)
        _require(isinstance(targets, (list, tuple)) and len(targets) == 2, f"{name}.targets: two outputs required")
        result.append((day, _feature_vector(_field(row, "features", name)),
                       [_number(value, f"{name}.targets[{j}]") for j, value in enumerate(targets)]))
    _require(len({row[0] for row in result}) == len(result), "rows: duplicate target date")
    return sorted(result, key=lambda row: row[0])


def fit_ridge(rows, alpha, fit_end, *, min_observations=1):
    """Fit train-only population scaling and two outputs with a free intercept."""
    regularization = _number(alpha, "alpha", positive=True)
    _require(regularization in ALPHAS, "alpha: frozen roster [1,10,100] required")
    _require(type(min_observations) is int and min_observations > 0, "min_observations: positive integer required")
    validated = _validated_rows(rows, fit_end)
    _require(len(validated) >= min_observations, "fit: insufficient eligible observations")
    x = np.asarray([row[1] for row in validated], dtype=float)
    y = np.asarray([row[2] for row in validated], dtype=float)
    means = x.mean(axis=0)
    scales = x.std(axis=0, ddof=0)
    scales = np.where(scales == 0.0, 1.0, scales)
    _require(np.isfinite(means).all() and np.isfinite(scales).all(),
             "fit: nonfinite train-only scaling")
    z = (x - means) / scales
    intercepts = y.mean(axis=0)
    coefficients = np.linalg.solve(z.T @ z + regularization * np.eye(len(FEATURE_NAMES)),
                                   z.T @ (y - intercepts))
    _require(all(np.isfinite(array).all() for array in (means, scales, intercepts, coefficients)),
             "fit: nonfinite fitted parameters")
    return {"feature_names": list(FEATURE_NAMES), "alpha": regularization,
            "means": means.tolist(), "scales": scales.tolist(),
            "intercepts": intercepts.tolist(), "coefficients": coefficients.tolist(),
            "fit_start": validated[0][0].isoformat(), "fit_end": _date(fit_end, "fit_end").isoformat(),
            "training_dates": [row[0].isoformat() for row in validated],
            "observation_count": len(validated), "feature_count": len(FEATURE_NAMES),
            "output_names": ["normalized_center_residual", "log_width_ratio"],
            "standardization": "training_rows_only_population_std; zero std replaced by 1",
            "label_availability": "17:15 Eastern same session; retrospective reconstructed"}


def _validate_model(model, target_date=None):
    _require(isinstance(model, dict), "model: record required")
    _require(model.get("feature_names") == list(FEATURE_NAMES), "model: feature order mismatch")
    _require(_number(model.get("alpha"), "model.alpha", positive=True) in ALPHAS,
             "model.alpha: frozen alpha required")
    start = _date(model.get("fit_start"), "model.fit_start")
    end = _date(model.get("fit_end"), "model.fit_end")
    _require(FIT_START <= start <= end <= FIT_END, "model: fit dates outside frozen period")
    if target_date is not None:
        _require(end < target_date, "model: fit_end must precede target date")
    dates = [_date(value, "model.training_dates") for value in model.get("training_dates", [])]
    _require(bool(dates) and dates == sorted(set(dates)) and dates[0] == start and
             all(start <= day <= end for day in dates), "model: invalid training chronology")
    _require(type(model.get("observation_count")) is int and model["observation_count"] == len(dates),
             "model: observation count mismatch")
    arrays = []
    count = len(FEATURE_NAMES)
    for name, shape in (("means", (count,)), ("scales", (count,)), ("intercepts", (2,)), ("coefficients", (count, 2))):
        try:
            array = np.asarray(model.get(name), dtype=float)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ContextError(f"model.{name}: numeric shape required") from exc
        _require(array.shape == shape and np.isfinite(array).all(), f"model.{name}: finite shape {shape} required")
        arrays.append(array)
    _require((arrays[1] > 0).all(), "model.scales: strictly positive required")
    return arrays


def _raw_prediction(feature_record, model, target_date=None):
    means, scales, intercepts, coefficients = _validate_model(model, target_date)
    z = (np.asarray(_feature_vector(feature_record)) - means) / scales
    contributions = z[:, None] * coefficients
    raw = intercepts + contributions.sum(axis=0)
    _require(np.isfinite(raw).all() and np.isfinite(contributions).all(), "prediction: nonfinite calculation")
    return raw, contributions


def _validate_method(method):
    expected = {"feature_names": list(FEATURE_NAMES), "alphas": list(ALPHAS),
                "initial_fit_end": INITIAL_FIT_END.isoformat(),
                "validation_start": VALIDATION_START.isoformat(), "fit_end": FIT_END.isoformat(),
                "minimum_initial_fit": 60, "minimum_validation": 15,
                "center_cap_scales": 2, "width_multiplier_bounds": [0.5, 2]}
    _require(isinstance(method, dict), "method: frozen record required")
    for key, value in expected.items():
        _require(method.get(key) == value, f"method.{key}: differs from frozen specification")


def choose_model(rows, method):
    """Select the fixed alpha roster on the earlier validation window, then refit."""
    _validate_method(method)
    _validated_rows(rows, FIT_END.isoformat())
    initial = [row for row in rows if _date(row["date"], "row.date") <= INITIAL_FIT_END]
    validation = [row for row in rows if VALIDATION_START <= _date(row["date"], "row.date") <= FIT_END]
    _require(len(initial) + len(validation) == len(rows), "rows: date outside initial/validation windows")
    _require(len(initial) >= 60, "selection: fewer than 60 initial-fit observations")
    _require(len(validation) >= 15, "selection: fewer than 15 validation observations")
    validation = sorted(validation, key=lambda row: row["date"])
    trials = []
    for alpha in ALPHAS:
        model = fit_ridge(initial, alpha, INITIAL_FIT_END.isoformat(), min_observations=60)
        losses = []
        for row in validation:
            raw, _ = _raw_prediction(row["features"], model, _date(row["date"], "row.date"))
            capped = (_clip(float(raw[0]), -2, 2), _clip(float(raw[1]), -LOG_WIDTH_CAP, LOG_WIDTH_CAP))
            losses.append(abs(capped[0] - float(row["targets"][0])) +
                          0.5 * abs(capped[1] - float(row["targets"][1])))
        loss = math.fsum(losses) / len(losses)
        trials.append({"alpha": alpha, "selection_loss": loss,
                       "initial_fit_count": len(initial), "validation_count": len(validation),
                       "validation_dates": [row["date"] for row in validation],
                       "initial_model": model})
    selected = min(trials, key=lambda trial: (trial["selection_loss"], trial["alpha"]))
    model = fit_ridge(initial + validation, selected["alpha"], FIT_END.isoformat(), min_observations=75)
    model["selection"] = {"selected_alpha": selected["alpha"],
                          "selection_loss": selected["selection_loss"],
                          "initial_fit_count": len(initial), "validation_count": len(validation),
                          "tie_break": "smaller alpha", "retest_labels_used": False}
    return model, trials


def predict(base, model):
    """Transform only original levels; retain every original component verbatim."""
    feature_record = _feature_values(base)
    target = _date(base["target_date"], "base.target_date")
    _require(FIT_END < target, "prediction: target must follow frozen September 25 fit cutoff")
    raw, contributions = _raw_prediction(feature_record, model, target)
    center_scales = _clip(float(raw[0]), -2, 2)
    log_width = _clip(float(raw[1]), -LOG_WIDTH_CAP, LOG_WIDTH_CAP)
    multiplier = math.exp(log_width)
    scale = float(base["adjusted_scale"])
    original_p = float(base["levels"]["P"])
    center = original_p + center_scales * scale
    transformed = [center + (float(base["levels"][key]) - original_p) * multiplier for key in LEVEL_NAMES]
    _require(all(math.isfinite(value) for value in transformed), "levels: nonfinite transformation")
    cents = [int((Decimal(str(value)) * 100).to_integral_value(rounding=ROUND_HALF_UP)) for value in transformed]
    minimum_gap_cents = max(1, int((Decimal(str(base["minimum_gap"])) * Decimal(str(multiplier)) * 100)
                                  .to_integral_value(rounding=ROUND_CEILING)))
    _require(cents[0] > 0, "levels: transformed S3 must remain positive")
    for index in range(1, len(cents)):
        cents[index] = max(cents[index], cents[index - 1] + minimum_gap_cents)
    levels = OrderedDict((key, value / 100) for key, value in zip(LEVEL_NAMES, cents))
    contribution_record = OrderedDict(
        (name, {"center_scales": float(contributions[index, 0]),
                "log_width": float(contributions[index, 1])})
        for index, name in enumerate(FEATURE_NAMES))
    record = {"version": "full_composite_context_v3", "mode": "full_composite_context_v3",
              "symbol": base["symbol"], "target_date": base["target_date"],
              "anchor_date": base["anchor_date"], "as_of": base["as_of"],
              "base": deepcopy(base), "levels": levels, "features": feature_record,
              "feature_contributions": contribution_record,
              "input_scope": "prior_cash_only", "input_session_close": base["prior_cash_session"]["close"],
              "input_timing": deepcopy(base.get("input_timing", {})),
              "center_raw_scales": float(raw[0]), "center_capped_scales": center_scales,
              "center": center, "center_cent_prediction": levels["P"],
              "log_width_raw": float(raw[1]), "log_width_capped": log_width,
              "width_multiplier": multiplier, "minimum_gap_cents": minimum_gap_cents,
              "adjusted_scale": scale * multiplier, "minimum_gap": minimum_gap_cents / 100,
              "model_fit_start": model["fit_start"], "model_fit_end": model["fit_end"],
              "model_alpha": model["alpha"], "model_intercepts": deepcopy(model["intercepts"]),
              "nodes_relocated": False, "calibrated_probabilities": False, "edge_established": False,
              "data_vintage": deepcopy(base.get("data_vintage", "historical latest; not certified as-seen")),
              "tape_completeness_certified": deepcopy(base.get("tape_completeness_certified", False)),
              "financial_validation": "development diagnostic only; no promotion, probability or profitability claim"}
    for key in ("method_sha256", "engine_sha256"):
        record[f"base_{key}"] = base[key]
    return record
