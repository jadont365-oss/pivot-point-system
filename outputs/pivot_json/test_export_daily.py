"""Frozen-data acceptance and negative controls for the additive JSON exporter."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import export_daily as exporter


class DailyExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.forecasts, cls.options, cls.evidence, cls.provenance = exporter.load_source_bundle()

    def build(self, forecast=None, options=None, evidence=None):
        return exporter.build_daily_record(
            copy.deepcopy(forecast or self.forecasts[0]),
            copy.deepcopy(self.options if options is None else options),
            copy.deepcopy(self.evidence if evidence is None else evidence),
            copy.deepcopy(self.provenance), "2026-10-09T13:00:00+00:00")

    def test_every_frozen_price_and_decimal_offset_is_preserved(self):
        for forecast in self.forecasts:
            record = self.build(forecast)
            for name, price in forecast["levels"].items():
                self.assertEqual(record["forecast"]["levels"][name]["price"], price)
                self.assertEqual(record["forecast"]["levels"][name]["signed_offset_dollars"],
                                 float(exporter.Decimal(str(price)) - exporter.Decimal(str(forecast["P"]))))
            self.assertEqual(record["forecast"]["anchor"]["price"], forecast["P"])

    def test_build_does_not_mutate_source_objects_or_source_files(self):
        original = copy.deepcopy((self.forecasts, self.options, self.evidence, self.provenance))
        before = {s["path"]: exporter.sha256_file(exporter.ROOT / s["path"])
                  for s in self.provenance["sources"]}
        self.build()
        self.assertEqual(original, (self.forecasts, self.options, self.evidence, self.provenance))
        self.assertEqual(before, {p: exporter.sha256_file(exporter.ROOT / p) for p in before})

    def test_named_prior_inputs_and_calculation_match_frozen_arrays(self):
        record = self.build()
        rows = record["forecast"]["inputs"]["prior_sessions"]
        source = self.forecasts[0]
        self.assertEqual(len(rows), 20)
        self.assertEqual([r["date"] for r in rows], source["input_dates"])
        self.assertEqual([r["normalized_cash_log_return_variance_390"] for r in rows], source["prior_cash_variances_390"])
        self.assertEqual([r["annual_atm_iv_decimal"] for r in rows], source["prior_atm_ivs"])
        self.assertEqual(record["forecast"]["calculation"]["adjusted_cash_sigma_dollars"], source["candidate_cash_sigma_dollars"])

    def test_missing_snapshot_is_explicit_without_inventing_quotes(self):
        options = copy.deepcopy(self.options)
        del options[self.forecasts[0]["input_dates"][-1]]
        record = self.build(options=options)
        snapshot = record["forecast"]["latest_option_pair"]
        self.assertEqual(snapshot["status"], "missing_snapshot")
        self.assertIsNone(snapshot["contracts"])
        self.assertIsNone(snapshot["source_file"])
        self.assertIsNone(snapshot["prior_cash_open"])
        self.assertIsNone(snapshot["prior_cash_close"])
        self.assertEqual(record["data_quality"]["live_freshness"], "unverified")
        self.assertIsNone(record["data_quality"]["event_age_seconds"])

    def test_probabilities_are_single_side_theory_and_empirical_is_null(self):
        probability = self.build()["forecast"]["probabilities"]
        self.assertIsNone(probability["empirical_touch_probability"])
        self.assertIsNone(probability["either_side_touch_probability"])
        theory = probability["theoretical_single_side_touch"]
        self.assertEqual(theory["scope"], "one_upper_or_one_lower_fixed_level")
        self.assertAlmostEqual(theory["rates"]["R3"], 0.31731050786291415)

    def test_four_cohorts_recount_denominators_and_early_close(self):
        record = self.build()
        evidence = record["research_evidence"]
        self.assertFalse(evidence["available_at_original_forecast"])
        self.assertEqual(evidence["timing_status"], "POSTHOC_NOT_AVAILABLE_AT_FORECAST")
        cohorts = {c["id"]: c for c in evidence["cohorts"]}
        self.assertEqual([c["sample_size"] for c in evidence["cohorts"]], [5, 21, 21, 22])
        july = cohorts["cash_touch_targets_holdout_2025_07"]
        early = next(r for r in july["daily"] if r["date"] == "2025-07-03")
        self.assertEqual(early["cash_minutes"], 210)
        self.assertEqual(early["cash_end"], "2025-07-03T13:00:00-04:00")
        self.assertEqual(july["measured_rates"]["either_R1_S1"], {"count": 22, "denominator": 22, "rate": 1.0})
        self.assertNotIn("cash_high", json.dumps(record["forecast"]))
        self.assertNotIn("first_touch", json.dumps(record["forecast"]))

    def test_actual_early_close_forecast_preserves_prices_and_duration_scale(self):
        forecasts = exporter.read_json(exporter.ROOT / "outputs/cash_touch_targets_holdout_2025_07/forecasts.json")
        source = next(f for f in forecasts if f["target_date"] == "2025-07-03")
        record = self.build(source, options={})
        self.assertEqual(record["session"]["cash_end"], "2025-07-03T13:00:00-04:00")
        self.assertEqual(record["forecast"]["calculation"]["target_duration_factor"], 210 / 390)
        self.assertEqual({k: v["price"] for k, v in record["forecast"]["levels"].items()}, source["levels"])

    def test_formal_schema_accepts_all_five_records(self):
        from jsonschema import Draft202012Validator, FormatChecker
        schema = json.loads((Path(exporter.__file__).with_name("schema.json")).read_text())
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        for forecast in self.forecasts:
            validator.validate(self.build(forecast))

    def test_invalid_source_inputs_are_rejected(self):
        for change in (lambda f: f.update(version="other"),
                       lambda f: f.update(as_of="2026-09-28T09:31:00-04:00"),
                       lambda f: f.update(as_of="2026-09-28T09:30:00+00:00"),
                       lambda f: f["input_dates"].__setitem__(1, f["input_dates"][0]),
                       lambda f: f["input_dates"].__setitem__(-1, f["target_date"]),
                       lambda f: f["prior_atm_ivs"].pop(),
                       lambda f: f["prior_atm_ivs"].__setitem__(0, float("nan")),
                       lambda f: f["prior_atm_ivs"].__setitem__(0, True),
                       lambda f: f.pop("P"),
                       lambda f: f.update(candidate_cash_sigma_dollars=100),
                       lambda f: f["levels"].update(R1=f["levels"]["R1"] + .01)):
            forecast = copy.deepcopy(self.forecasts[0])
            change(forecast)
            with self.subTest(forecast=repr(forecast)[:70]), self.assertRaises(ValueError):
                self.build(forecast)

    def test_report_count_disagreement_rejected(self):
        evidence = copy.deepcopy(self.evidence)
        evidence["cash_touch_targets_month_2026_08"]["counts"]["either_touch"] -= 1
        with self.assertRaises(ValueError):
            self.build(evidence=evidence)

    def test_corrupt_present_option_snapshot_is_rejected(self):
        latest_day = self.forecasts[0]["input_dates"][-1]
        for change in (lambda q: q.update(symbol="SPY"),
                       lambda q: q.update(expiration="2026-09-29"),
                       lambda q: q.update(strike=q["strike"] + 1),
                       lambda q: q.update(timestamp="2026-09-28T09:30:00-04:00"),
                       lambda q: q.update(bid=q["ask"] + 1),
                       lambda q: q.update(delta=0),
                       lambda q: q.update(iv_error=.02),
                       lambda q: q.update(implied_vol=float("nan")),
                       lambda q: q.update(underlying_price=q["underlying_price"] * 2)):
            options = copy.deepcopy(self.options)
            change(options[latest_day]["contracts"][0])
            with self.subTest(change=repr(change)), self.assertRaises(ValueError):
                self.build(options=options)

    def test_both_option_timestamps_must_be_the_authoritative_final_minute(self):
        latest_day = self.forecasts[0]["input_dates"][-1]
        for clock in ("12:00:00", "12:59:00"):
            options = copy.deepcopy(self.options)
            for contract in options[latest_day]["contracts"]:
                contract["timestamp"] = contract["underlying_timestamp"] = latest_day + "T" + clock + "-04:00"
            with self.subTest(clock=clock), self.assertRaises(ValueError):
                self.build(options=options)

    def test_present_snapshot_requires_authoritative_prior_cash_clock(self):
        latest_day = self.forecasts[0]["input_dates"][-1]
        for key in ("prior_cash_open", "prior_cash_close"):
            options = copy.deepcopy(self.options)
            del options[latest_day][key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.build(options=options)

    def test_actual_early_prior_pair_accepts_1259_and_rejects_1559(self):
        folder = exporter.ROOT / "outputs/cash_touch_targets_holdout_2025_07"
        source = next(f for f in exporter.read_json(folder / "forecasts.json") if f["target_date"] == "2025-07-07")
        snapshot = exporter.read_json(folder / "qualified_options.json")["2025-07-03"]
        session = next(r for r in exporter.read_json(folder / "rules_freeze.json")["roster"] if r["date"] == "2025-07-03")
        relative = "outputs/cash_touch_targets_holdout_2025_07/data/greeks_2025-07-03.parquet"
        snapshot.update(raw_rows=snapshot["source_rows"], source_file=relative,
                        source_sha256=exporter.sha256_file(exporter.ROOT / relative),
                        prior_cash_open=exporter.stamp(session["date"], session["open"]).isoformat(),
                        prior_cash_close=exporter.stamp(session["date"], session["close"]).isoformat())
        provenance = copy.deepcopy(self.provenance)
        provenance["sources"].append({"path": relative, "sha256": snapshot["source_sha256"], "role": "frozen_option_archive"})
        build = lambda: exporter.build_daily_record(source, {"2025-07-03": snapshot}, self.evidence,
                                                    provenance, "2026-10-09T13:00:00+00:00")
        record = build()
        self.assertEqual(record["forecast"]["latest_option_pair"]["prior_cash_close"], "2025-07-03T13:00:00-04:00")
        self.assertEqual(record["forecast"]["latest_option_pair"]["contracts"][0]["timestamp"], "2025-07-03T12:59:00-04:00")
        self.assertEqual({k: v["price"] for k, v in record["forecast"]["levels"].items()}, source["levels"])
        from jsonschema import Draft202012Validator, FormatChecker
        Draft202012Validator(exporter.read_json(Path(exporter.__file__).with_name("schema.json")),
                             format_checker=FormatChecker()).validate(record)
        for contract in snapshot["contracts"]:
            contract["timestamp"] = contract["underlying_timestamp"] = "2025-07-03T15:59:00-04:00"
        with self.assertRaises(ValueError):
            build()

    def test_semantic_negative_controls(self):
        base = self.build()
        for change in (lambda r: r["forecast"]["inputs"]["prior_sessions"].pop(),
                       lambda r: r["forecast"]["calculation"].update(iv_ratio=2),
                       lambda r: r["forecast"]["levels"]["P"].update(price=0),
                       lambda r: r["research_evidence"].update(available_at_original_forecast=True),
                       lambda r: r["research_evidence"]["cohorts"][0]["measured_rates"]["R1"].update(denominator=0),
                       lambda r: r["forecast"]["probabilities"].update(empirical_touch_probability=.7),
                       lambda r: r["forecast"].update(cash_high=999),
                       lambda r: r["data_quality"].update(event_age_seconds=float("inf"))):
            record = copy.deepcopy(base)
            change(record)
            with self.subTest(change=repr(change)), self.assertRaises(ValueError):
                exporter.validate_record(record)

    def test_cli_manifest_verify_is_read_only_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(exporter.main(["--date", "2026-09-28", "--out-dir", folder]), 0)
            output = Path(folder) / "daily/QQQ_2026-09-28.json"
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(folder).rglob("*.json")}
            self.assertEqual(exporter.main(["--out-dir", folder, "--verify"]), 0)
            self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})
            record = json.loads(output.read_text())
            record["forecast"]["levels"]["R1"]["price"] += 1
            output.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaises(ValueError):
                exporter.main(["--out-dir", folder, "--verify"])


if __name__ == "__main__":
    unittest.main()
