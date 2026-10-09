"""Recorded QQQ parity and instrument/clock/data negative controls."""
import copy
import datetime as dt
import unittest

import polars as pl

import algorithm as alg


class AlgorithmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = alg.touch.load_inputs(alg.read(alg.model.V1/'contract.json'))
        cls.data['symbol'] = 'QQQ'
        cls.original = alg.read(alg.model.HERE/'forecasts.json')

    def test_all_35_original_qqq_prices_and_forecasts_are_exact(self):
        for original in self.original:
            day = original['target_date']
            prepared = alg.prepare('QQQ', day, self.data)
            rows = self.data['cash'](day)
            actual = alg.at_cash_open(prepared, rows[0]['open'], rows[0]['timestamp'])
            # The old archive's extra opening_source points to its original
            # file; new runs bind their own source manifest separately.
            self.assertEqual(actual['forecast'], {k: v for k, v in original.items() if k != 'opening_source'})

    def test_all_24_original_atm_pairs_are_identical(self):
        for day, old in self.data['options'].items():
            rows = pl.read_parquet(alg.ROOT/old['source_file']).to_dicts()
            pair = alg.closing_pair('QQQ', rows, day, old['contracts'][0]['expiration'], self.data['session'][day], self.data['history'][day][-1]['close'])
            self.assertEqual(pair['contracts'], old['contracts'])
            self.assertEqual(pair['annual_iv_decimal'], old['annual_iv_decimal'])

    def test_ticker_is_explicit_and_limited(self):
        self.assertEqual(alg.symbol('spy'), 'SPY')
        self.assertEqual(alg.symbol('QQQ'), 'QQQ')
        for value in ('SPX', '../QQQ', '', None):
            with self.assertRaises(ValueError):
                alg.symbol(value)

    def test_wrong_candle_instrument_is_rejected(self):
        with self.assertRaises(ValueError):
            alg.prepare('SPY', self.original[0]['target_date'], self.data)

    def test_spy_cannot_use_qqq_options(self):
        data = dict(self.data, symbol='SPY')
        with self.assertRaises(ValueError):
            alg.prepare('SPY', self.original[0]['target_date'], data)

    def test_mixed_option_chain_is_rejected(self):
        day = self.original[0]['input_dates'][0]
        old = self.data['options'][day]
        rows = pl.read_parquet(alg.ROOT/old['source_file']).to_dicts()
        rows.append(dict(rows[0], symbol='SPY'))
        with self.assertRaises(ValueError):
            alg.closing_pair('QQQ', rows, day, old['contracts'][0]['expiration'], self.data['session'][day], self.data['history'][day][-1]['close'])

    def test_incomplete_cash_history_and_short_lookback_are_rejected(self):
        day = self.original[0]['target_date']
        for gap in (True, False):
            data = copy.deepcopy(self.data)
            if gap:
                del data['history'][data['prior'][day][0]][3]
            else:
                data['prior'][day] = data['prior'][day][1:]
            with self.assertRaises(ValueError):
                alg.prepare('QQQ', day, data)

    def test_wrong_expiration_or_negative_iv_is_rejected(self):
        day = self.original[0]['target_date']
        for wrong_expiry in (True, False):
            data = copy.deepcopy(self.data)
            pair = data['options'][data['prior'][day][0]]
            for q in pair['contracts']:
                if wrong_expiry:
                    q['expiration'] = day
                else:
                    q['implied_vol'] = -.2
            if not wrong_expiry:
                pair['annual_iv_decimal'] = -.2
            with self.assertRaises(ValueError):
                alg.prepare('QQQ', day, data)

    def test_current_session_outcomes_do_not_change_distances(self):
        day = self.original[0]['target_date']
        before = alg.prepare('QQQ', day, self.data)
        data = copy.deepcopy(self.data)
        data['history'][day] = [dict(r, high=99999., low=.01, close=99999., volume=0.) for r in data['history'][day]]
        self.assertEqual(before, alg.prepare('QQQ', day, data))

    def test_opening_input_guards(self):
        day = self.original[0]['target_date']
        prepared = alg.prepare('QQQ', day, self.data)
        at = alg.model.stamp(day, '09:30:00')
        for price, clock in ((0., at), (float('nan'), at), (100., at-dt.timedelta(minutes=1)), (100., at+dt.timedelta(minutes=1))):
            with self.assertRaises(ValueError):
                alg.at_cash_open(prepared, price, clock)

    def test_early_close_option_clock_is_1259(self):
        day = self.original[0]['input_dates'][0]
        old = self.data['options'][day]
        q = old['contracts'][0]
        rows = [dict(x, timestamp=alg.model.stamp(day, '12:59:00'), underlying_timestamp=alg.model.stamp(day, '12:59:00')) for x in old['contracts']]
        pair = alg.closing_pair('QQQ', rows, day, q['expiration'], dict(open='09:30:00', close='13:00:00'), q['underlying_price'])
        self.assertEqual(pair['annual_iv_decimal'], old['annual_iv_decimal'])
        with self.assertRaises(ValueError):
            alg.closing_pair('QQQ', rows, day, q['expiration'], dict(open='09:30:00', close='16:00:00'), q['underlying_price'])

    def test_daily_json_has_explicit_instrument_and_unknown_probability(self):
        day = self.original[0]['target_date']
        prepared = alg.prepare('QQQ', day, self.data)
        rows = self.data['cash'](day)
        projected = alg.at_cash_open(prepared, rows[0]['open'], rows[0]['timestamp'])
        record = alg.daily_record(projected, self.data['options'], self.data['session'][day], self.data['manifest'])
        self.assertEqual(record['instrument']['symbol'], 'QQQ')
        self.assertEqual(record['forecast']['levels'], self.original[0]['levels'])
        self.assertIsNone(record['probabilities']['calibrated_touch_probability'])
        self.assertEqual(len(record['calculation']['prior_option_pairs']), 20)
        projected['symbol'] = 'SPY'
        with self.assertRaises(ValueError):
            alg.daily_record(projected, self.data['options'], self.data['session'][day], {})


if __name__ == '__main__':
    unittest.main()
