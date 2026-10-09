"""Offline calculation and input-safety checks, not trading validation."""
import copy
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
import json
import math
import unittest
from zoneinfo import ZoneInfo

from outputs.pivot_algorithm import algorithm as alg
from scripts.check_repository import sensitive


NY = ZoneInfo('America/New_York')
NAMES = ('S3', 'S2', 'S1', 'P', 'R1', 'R2', 'R3')
MULTIPLIERS = {'R1': .38, 'S1': .38, 'R2': .67, 'S2': .67, 'R3': 1., 'S3': 1.}


def stamp(day, clock):
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time.fromisoformat(clock), NY)


def cash_rows(day, session, opening_price, index):
    opening, closing = (stamp(day, session[k]) for k in ('open', 'close'))
    rows, previous = [], opening_price
    for minute in range(int((closing-opening).total_seconds()/60)):
        # Nonzero first return and varying intraday returns/session amplitudes.
        move = (.0004 + index*.00001)*(1 if minute % 2 == 0 else -1)
        move += ((minute % 7)-3)*.000008 + .000002
        close = previous*math.exp(move)
        rows.append(dict(timestamp=opening+dt.timedelta(minutes=minute), open=previous,
                         high=max(previous, close)+.02, low=min(previous, close)-.02,
                         close=close, volume=1000+minute))
        previous = close
    return rows


def synthetic_data(ticker, *, prior_half_day=False, target_half_day=False):
    """A synthetic session roster; no files, provider calls or receipt claims."""
    cursor, dates = dt.date(2026, 3, 2), []
    while len(dates) < 21:
        if cursor.weekday() < 5:
            dates.append(cursor.isoformat())
        cursor += dt.timedelta(days=1)
    prior, target = dates[:-1], dates[-1]
    sessions = {d: dict(open='09:30:00', close='16:00:00') for d in dates}
    if prior_half_day:
        sessions[prior[-1]]['close'] = '13:00:00'
    if target_half_day:
        sessions[target]['close'] = '13:00:00'
    base = 425. if ticker == 'QQQ' else 555.
    history = {d: cash_rows(d, sessions[d], base*(1+i*.001), i)
               for i, d in enumerate(dates)}
    options = {}
    for i, day in enumerate(prior):
        reference = history[day][-1]['close']
        snapshot = stamp(day, sessions[day]['close'])-dt.timedelta(minutes=1)
        iv = .18+i*.002+(.01 if ticker == 'SPY' else 0.)
        contracts = [dict(symbol=ticker, expiration=dates[i+1], strike=float(round(reference)),
                          right=right, timestamp=snapshot.isoformat(),
                          underlying_timestamp=(snapshot-dt.timedelta(seconds=30)).isoformat(),
                          bid=1.95, ask=2.05, delta=delta, implied_vol=iv+extra,
                          iv_error=.001, underlying_price=reference)
                     for right, delta, extra in (('CALL', .5, 0.), ('PUT', -.5, .02))]
        options[day] = dict(annual_iv_decimal=(contracts[0]['implied_vol']+contracts[1]['implied_vol'])/2,
                            strike=contracts[0]['strike'], source_rows=2, eligible_rows=2,
                            contracts=contracts)
    return target, dict(symbol=ticker, prior={target: prior}, history=history,
                        options=options, session=sessions)


def decoded_pair(pair):
    return [dict(q, timestamp=dt.datetime.fromisoformat(q['timestamp']),
                 underlying_timestamp=dt.datetime.fromisoformat(q['underlying_timestamp']))
            for q in pair['contracts']]


def independent_calculation(day, data):
    """Calculate from successive prices, with no model math/helper calls."""
    dates = data['prior'][day]
    variances, ivs = [], []
    for date in dates:
        rows = data['history'][date]
        prices = [rows[0]['open']]+[r['close'] for r in rows]
        squared_returns = [math.log(b/a)**2 for a, b in zip(prices, prices[1:])]
        variances.append(sum(squared_returns)*390/len(rows))
        ivs.append(sum(q['implied_vol'] for q in data['options'][date]['contracts'])/2)
    minutes = int((stamp(day, data['session'][day]['close'])-
                   stamp(day, data['session'][day]['open'])).total_seconds()/60)
    reference = data['history'][dates[-1]][-1]['close']
    history_sigma = reference*math.sqrt(sum(variances)/20*minutes/390)
    iv_ratio = ivs[-1]/(sum(ivs)/20)
    sigma = history_sigma*iv_ratio
    cents = lambda value: float(Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
    levels = {name: cents(reference+(1 if name.startswith('R') else -1)*multiple*sigma)
              for name, multiple in MULTIPLIERS.items()}
    levels['P'] = cents(reference)
    return dict(variances=variances, ivs=ivs, minutes=minutes, history_sigma=history_sigma,
                iv_ratio=iv_ratio, sigma=sigma, levels=levels)


class SyntheticCalculationTests(unittest.TestCase):
    def assert_calculation(self, ticker, day, data):
        expected = independent_calculation(day, data)
        prepared = alg.prepare(ticker, day, data)
        forecast = prepared['prior_forecast']
        self.assertEqual(alg.MULTIPLIERS, MULTIPLIERS)
        self.assertEqual(forecast['input_dates'], data['prior'][day])
        self.assertEqual(len(forecast['input_dates']), 20)
        self.assertEqual(forecast['cash_minutes'], expected['minutes'])
        for actual, value in zip(forecast['prior_cash_variances_390'], expected['variances']):
            self.assertAlmostEqual(actual, value, places=14)
        for actual, value in zip(forecast['prior_atm_ivs'], expected['ivs']):
            self.assertAlmostEqual(actual, value, places=14)
        self.assertAlmostEqual(forecast['history_cash_sigma_dollars'], expected['history_sigma'], places=12)
        self.assertAlmostEqual(forecast['iv_ratio'], expected['iv_ratio'], places=14)
        self.assertAlmostEqual(forecast['candidate_cash_sigma_dollars'], expected['sigma'], places=12)
        self.assertEqual(forecast['levels'], expected['levels'])
        opening_price = 430.005 if ticker == 'QQQ' else 560.005
        projected = alg.at_cash_open(prepared, opening_price, stamp(day, '09:30:00'))['forecast']
        pivot = Decimal(str(opening_price)).quantize(Decimal('.00000001'))
        shift = pivot-Decimal(str(expected['levels']['P']))
        translated = {name: float(Decimal(str(value))+shift) for name, value in expected['levels'].items()}
        self.assertEqual(projected['levels'], translated)
        self.assertEqual(set(projected['levels']), set(NAMES))
        for name in NAMES:
            self.assertEqual(Decimal(str(projected['levels'][name]))-pivot,
                             Decimal(str(expected['levels'][name]))-Decimal(str(expected['levels']['P'])))
        self.assertTrue(all(projected['levels'][a] < projected['levels'][b] for a, b in zip(NAMES, NAMES[1:])))
        return prepared

    def test_independent_variance_iv_and_all_seven_levels_for_each_ticker(self):
        forecasts = {}
        for ticker in ('QQQ', 'SPY'):
            with self.subTest(ticker=ticker):
                day, data = synthetic_data(ticker)
                self.assertTrue(all(len(data['history'][d]) == 390 for d in data['prior'][day]))
                forecasts[ticker] = self.assert_calculation(ticker, day, data)['prior_forecast']
                self.assertGreater(forecasts[ticker]['iv_ratio'], 1.)
        self.assertNotEqual(forecasts['QQQ']['candidate_cash_sigma_dollars'], forecasts['SPY']['candidate_cash_sigma_dollars'])

    def test_decimal_half_up_ties_are_preserved_before_translation(self):
        for value, expected in ((1.005, 1.01), (1.015, 1.02), (100.005, 100.01)):
            with self.subTest(value=value):
                self.assertEqual(alg.touch.cents(value), expected)
        levels = alg.touch.levels(100.005, 1., MULTIPLIERS)
        self.assertEqual(levels['P'], 100.01)
        # The frozen rule converts the float arithmetic result to Decimal;
        # independent_calculation reproduces that boundary for all six lines.

    def test_exact_latest_twenty_prior_sessions_are_required(self):
        for variant in ('short', 'reversed', 'duplicate', 'target', 'future', 'omitted_latest'):
            with self.subTest(variant=variant):
                day, data = synthetic_data('QQQ')
                dates = data['prior'][day]
                if variant == 'short':
                    dates.pop()
                elif variant == 'reversed':
                    dates.reverse()
                elif variant == 'duplicate':
                    dates[1] = dates[0]
                elif variant == 'target':
                    dates[-1] = day
                elif variant == 'future':
                    dates[-1] = (dt.date.fromisoformat(day)+dt.timedelta(days=1)).isoformat()
                else:
                    data['session']['2026-03-28'] = dict(open='09:30:00', close='16:00:00')
                with self.assertRaises(ValueError):
                    alg.prepare('QQQ', day, data)

    def test_cash_minutes_and_prices_are_validated(self):
        for variant in ('missing', 'duplicate', 'misordered', 'future_clock', 'nan', 'zero', 'bad_ohlc'):
            with self.subTest(variant=variant):
                day, data = synthetic_data('QQQ')
                rows = data['history'][data['prior'][day][0]]
                if variant == 'missing':
                    rows.pop(3)
                elif variant == 'duplicate':
                    rows[3] = dict(rows[2])
                elif variant == 'misordered':
                    rows[2], rows[3] = rows[3], rows[2]
                elif variant == 'future_clock':
                    rows[3]['timestamp'] = stamp(day, '09:30:00')
                elif variant == 'nan':
                    rows[3]['close'] = float('nan')
                elif variant == 'zero':
                    rows[3]['open'] = 0.
                else:
                    rows[3]['high'] = rows[3]['low']
                with self.assertRaises(ValueError):
                    alg.prepare('QQQ', day, data)

    def test_tickers_and_cross_instrument_inputs_are_rejected(self):
        self.assertEqual(alg.symbol(' spy '), 'SPY')
        for value in ('SPX', '../QQQ', '', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                alg.symbol(value)
        day, qqq = synthetic_data('QQQ')
        with self.assertRaises(ValueError):
            alg.prepare('SPY', day, qqq)
        _, spy = synthetic_data('SPY')
        spy['options'] = copy.deepcopy(qqq['options'])
        with self.assertRaises(ValueError):
            alg.prepare('SPY', day, spy)
        prior = qqq['prior'][day][-1]
        rows = decoded_pair(qqq['options'][prior])
        rows.append(dict(rows[0], symbol='SPY'))
        with self.assertRaises(ValueError):
            alg.closing_pair('QQQ', rows, prior, day, qqq['session'][prior], rows[0]['underlying_price'])

    def test_closing_chain_selects_nearest_common_qualified_strike(self):
        for ticker in ('QQQ', 'SPY'):
            with self.subTest(ticker=ticker):
                day, data = synthetic_data(ticker)
                prior = data['prior'][day][-1]
                template = decoded_pair(data['options'][prior])
                rows = [dict(q, strike=strike, underlying_price=200.) for strike in (199., 201.) for q in template]
                rows.append(dict(template[0], strike=200., underlying_price=200.))  # No matching put.
                rows.extend(dict(q, strike=200.5, underlying_price=200., implied_vol=-.2) for q in template)
                result = alg.closing_pair(ticker, rows, prior, day, data['session'][prior], 200.)
                self.assertEqual(result['strike'], 199.)
                self.assertEqual([q['right'] for q in result['contracts']], ['CALL', 'PUT'])
                self.assertEqual(result['eligible_rows'], 5)
                self.assertEqual(result['source_rows'], 7)
                self.assertTrue(all(q['symbol'] == ticker for q in result['contracts']))
                with self.assertRaises(ValueError):
                    alg.closing_pair(ticker, rows+[rows[0]], prior, day, data['session'][prior], 200.)

    def test_invalid_option_iv_and_quality_filters_reject_the_pair(self):
        invalid = [('implied_vol', v) for v in (0., -.2, 5., float('nan'), float('inf'))]
        invalid += [('bid', 0.), ('bid', 3.), ('bid', True), ('ask', 0.), ('ask', 3.),
                    ('delta', .09), ('delta', .91), ('iv_error', .02), ('strike', float('nan'))]
        for key, value in invalid+[('underlying_price', 1.)]:
            with self.subTest(field=key, value=value):
                day, data = synthetic_data('QQQ')
                prior = data['prior'][day][-1]
                rows = decoded_pair(data['options'][prior])
                rows[0][key] = value
                with self.assertRaises(ValueError):
                    alg.closing_pair('QQQ', rows, prior, day, data['session'][prior], data['history'][prior][-1]['close'])

    def test_prepared_pairs_require_next_expiry_matching_contracts_and_iv(self):
        for variant in ('expiry', 'strike', 'right', 'missing', 'cached_iv', 'negative_iv'):
            with self.subTest(variant=variant):
                day, data = synthetic_data('QQQ')
                prior = data['prior'][day][0]
                pair = data['options'][prior]
                if variant == 'expiry':
                    pair['contracts'][0]['expiration'] = day
                elif variant == 'strike':
                    pair['contracts'][0]['strike'] += 1.
                elif variant == 'right':
                    pair['contracts'][0]['right'] = 'PUT'
                elif variant == 'missing':
                    pair['contracts'].pop()
                elif variant == 'cached_iv':
                    pair['annual_iv_decimal'] += .01
                else:
                    for q in pair['contracts']:
                        q['implied_vol'] = -.2
                    pair['annual_iv_decimal'] = -.2
                with self.assertRaises(ValueError):
                    alg.prepare('QQQ', day, data)
        day, data = synthetic_data('QQQ')
        prior = data['prior'][day][-1]
        with self.assertRaises(ValueError):
            alg.closing_pair('QQQ', decoded_pair(data['options'][prior]), prior, prior,
                             data['session'][prior], data['history'][prior][-1]['close'])

    def test_option_and_underlying_quote_clocks_are_prior_cash_only(self):
        for variant in ('quote_early', 'quote_late', 'quote_target', 'underlying_early', 'underlying_late'):
            with self.subTest(variant=variant):
                day, data = synthetic_data('QQQ')
                prior = data['prior'][day][-1]
                q = data['options'][prior]['contracts'][0]
                snapshot = stamp(prior, '15:59:00')
                if variant == 'quote_early':
                    q['timestamp'] = (snapshot-dt.timedelta(minutes=1)).isoformat()
                elif variant == 'quote_late':
                    q['timestamp'] = (snapshot+dt.timedelta(minutes=1)).isoformat()
                elif variant == 'quote_target':
                    q['timestamp'] = stamp(day, '09:30:00').isoformat()
                elif variant == 'underlying_early':
                    q['underlying_timestamp'] = (stamp(prior, '09:30:00')-dt.timedelta(seconds=1)).isoformat()
                else:
                    q['underlying_timestamp'] = (snapshot+dt.timedelta(seconds=1)).isoformat()
                with self.assertRaises(ValueError):
                    alg.prepare('QQQ', day, data)

    def test_half_day_prior_and_target_normalization_and_1259_quote(self):
        for prior_half, target_half in ((True, False), (False, True), (True, True)):
            with self.subTest(prior_half=prior_half, target_half=target_half):
                day, data = synthetic_data('QQQ', prior_half_day=prior_half, target_half_day=target_half)
                self.assert_calculation('QQQ', day, data)
                prior = data['prior'][day][-1]
                self.assertEqual(len(data['history'][prior]), 210 if prior_half else 390)
                if prior_half:
                    rows = decoded_pair(data['options'][prior])
                    self.assertEqual(rows[0]['timestamp'], stamp(prior, '12:59:00'))
                    self.assertEqual(alg.closing_pair('QQQ', rows, prior, day, data['session'][prior],
                                                    data['history'][prior][-1]['close'])['contracts'],
                                     data['options'][prior]['contracts'])
                    for q in rows:
                        q['timestamp'] = stamp(prior, '15:59:00')
                    with self.assertRaises(ValueError):
                        alg.closing_pair('QQQ', rows, prior, day, data['session'][prior], data['history'][prior][-1]['close'])

    def test_opening_observation_timing_and_subcent_anchor(self):
        day, data = synthetic_data('QQQ')
        prepared = alg.prepare('QQQ', day, data)
        opening = stamp(day, '09:30:00')
        for at in (opening, opening+dt.timedelta(milliseconds=500),
                   opening+dt.timedelta(minutes=1, microseconds=-1), opening.astimezone(dt.timezone.utc)):
            with self.subTest(at=at):
                forecast = alg.at_cash_open(prepared, 430.005, at)['forecast']
                self.assertEqual(forecast['P'], 430.005)
                self.assertEqual(forecast['levels']['P'], 430.005)
        rounded = alg.at_cash_open(prepared, 430.123456789, opening)['forecast']
        self.assertEqual(rounded['P'], 430.12345679)
        for price, at in ((0., opening), (-1., opening), (float('nan'), opening),
                          (float('inf'), opening), ('430', opening), (430., None),
                          (430., opening.replace(tzinfo=None)),
                          (430., opening-dt.timedelta(microseconds=1)),
                          (430., opening+dt.timedelta(minutes=1))):
            with self.subTest(price=price, at=at), self.assertRaises(ValueError):
                alg.at_cash_open(prepared, price, at)

        original = copy.deepcopy(prepared)
        for variant in ('at_open', 'target_input', 'future_input'):
            with self.subTest(variant=variant):
                prepared = copy.deepcopy(original)
                if variant == 'at_open':
                    prepared['prior_forecast']['as_of'] = opening.isoformat()
                else:
                    extra = day if variant == 'target_input' else (dt.date.fromisoformat(day)+dt.timedelta(days=1)).isoformat()
                    prepared['prior_forecast']['input_dates'].append(extra)
                with self.assertRaises(ValueError):
                    alg.at_cash_open(prepared, 430.005, opening)

    def test_target_session_outcomes_cannot_change_prepared_distances(self):
        for ticker in ('QQQ', 'SPY'):
            with self.subTest(ticker=ticker):
                day, data = synthetic_data(ticker)
                before = alg.prepare(ticker, day, data)
                for row in data['history'][day]:
                    row.update(high=99999., low=.01, close=99999., volume=0.)
                self.assertEqual(before, alg.prepare(ticker, day, data))
                del data['history'][day]
                self.assertEqual(before, alg.prepare(ticker, day, data))

    def test_score_exact_low_high_non_touches_and_unconfirmed_gaps(self):
        day = '2026-03-30'
        rows = [dict(timestamp=stamp(day, '09:30:00'), open=100., high=101., low=99., close=100.),
                dict(timestamp=stamp(day, '09:31:00'), open=100., high=102., low=98., close=100.)]
        for name, price, minute in (('S1', 99., 0), ('R1', 101., 0), ('S2', 98., 1), ('R2', 102., 1), ('P', 100., 0)):
            with self.subTest(name=name):
                result = alg.touch.score(rows, price, name)
                self.assertTrue(result['touched'])
                self.assertEqual(result['first_touch_minute'], rows[minute]['timestamp'].isoformat())
                self.assertEqual(result['status'], 'bar_touch')
        for name, price in (('S3', 97.), ('R3', 103.)):
            with self.subTest(name=name):
                result = alg.touch.score(rows, price, name)
                self.assertFalse(result['touched'])
                self.assertIsNone(result['first_touch_minute'])
                self.assertFalse(result['threshold_reached'])
                self.assertEqual(result['status'], 'not_touched')
        beyond = [dict(rows[0], open=103., high=104., low=102., close=103.)]
        result = alg.touch.score(beyond, 101., 'R1')
        self.assertFalse(result['touched'])
        self.assertTrue(result['opened_beyond'])
        self.assertTrue(result['threshold_reached'])
        self.assertEqual(result['status'], 'opened_beyond_untouched')
        gap = [dict(rows[0], high=100.5), dict(rows[1], open=102., high=103., low=102., close=102.)]
        result = alg.touch.score(gap, 101., 'R1')
        self.assertFalse(result['touched'])
        self.assertTrue(result['crossed_between_cash_bars'])
        self.assertEqual(result['status'], 'crossed_between_bars_unconfirmed')

    def test_detailed_json_is_finite_complete_copied_and_uncalibrated(self):
        for ticker in ('QQQ', 'SPY'):
            with self.subTest(ticker=ticker):
                day, data = synthetic_data(ticker)
                prepared = alg.prepare(ticker, day, data)
                projected = alg.at_cash_open(prepared, 430.005, stamp(day, '09:30:00'))
                sources = {'synthetic': 'in_memory_only'}
                record = alg.daily_record(projected, data['options'], data['session'][day], sources)
                decoded = json.loads(json.dumps(record, allow_nan=False))
                self.assertEqual(decoded['instrument']['symbol'], ticker)
                self.assertEqual(decoded['forecast']['levels'], projected['forecast']['levels'])
                self.assertEqual(decoded['calculation']['prior_option_pairs'], data['options'])
                self.assertEqual(len(decoded['calculation']['prior_option_pairs']), 20)
                self.assertEqual(sum(len(p['contracts']) for p in decoded['calculation']['prior_option_pairs'].values()), 40)
                self.assertIsNone(decoded['probabilities']['calibrated_touch_probability'])
                self.assertEqual(decoded['probabilities']['status'], 'not_calibrated_for_this_instrument')
                self.assertFalse(decoded['system_status']['orders'])
                self.assertFalse(decoded['system_status']['live_open_collector'])
                self.assertFalse(decoded['system_status']['fitting'])
                data['options'][data['prior'][day][0]]['contracts'][0]['implied_vol'] = .99
                projected['forecast']['levels']['P'] = .01
                sources['synthetic'] = 'changed_after_record'
                self.assertEqual(record, decoded)
                projected['symbol'] = 'SPY' if ticker == 'QQQ' else 'QQQ'
                with self.assertRaises(ValueError):
                    alg.daily_record(projected, data['options'], data['session'][day], sources)


class RepositoryControlTests(unittest.TestCase):
    def test_secret_detector_has_positive_and_negative_synthetic_controls(self):
        # Construct fake markers so this source file itself is safe to publish.
        for prefix in ('td1_', 'ghp_', 'github_pat_'):
            with self.subTest(prefix=prefix):
                self.assertTrue(sensitive(prefix+'x'*24))
        self.assertTrue(sensitive('-----BEGIN '+'PRIVATE KEY-----'))
        for text in ('synthetic inputs only', 'td1_short', 'ghp_short', 'github_pat_short',
                     '-----BEGIN CERTIFICATE-----'):
            with self.subTest(text=text):
                self.assertFalse(sensitive(text))


if __name__ == '__main__':
    unittest.main()
