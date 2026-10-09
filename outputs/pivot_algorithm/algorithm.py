"""QQQ/SPY data adapter around the existing immutable cash-open pivot math."""
import copy
import datetime as dt
from decimal import Decimal
import importlib.util
import math
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('frozen_cash_open_model', ROOT/'outputs/cash_touch_targets_cash_open_v3/run_test.py')
model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)
touch = model.touch
require, read, write, digest = model.require, model.read, model.write, model.digest
SUPPORTED = ('QQQ', 'SPY')
MULTIPLIERS = read(model.V1/'contract.json')['multipliers']


def symbol(value):
    require(isinstance(value, str), 'Ticker must be text')
    value = value.strip().upper()
    require(value in SUPPORTED, 'Supported tickers: QQQ, SPY')
    return value


def closing_pair(ticker, rows, day, expiration, session, reference):
    """Same QQQ ATM filters, applied to the requested instrument's own chain."""
    ticker = symbol(ticker)
    require(bool(rows) and all(q.get('symbol') == ticker for q in rows), 'Mixed or wrong option symbol')
    require(expiration > day, 'Option expiry must follow the prior cash session')
    opening, closing = [model.stamp(day, session[k]) for k in ('open', 'close')]
    snapshot = closing-dt.timedelta(minutes=1)
    eligible = {}
    for q in rows:
        if q['timestamp'] != snapshot or not opening <= q['underlying_timestamp'] <= snapshot:
            continue
        nums = [q[k] for k in ('bid', 'ask', 'implied_vol', 'delta', 'iv_error', 'underlying_price', 'strike')]
        if not all(type(v) in (float, int) and math.isfinite(v) for v in nums):
            continue
        if not (q['expiration'] == expiration and q['right'] in ('CALL', 'PUT') and
                0 < q['bid'] <= q['ask'] and 0 < q['implied_vol'] < 5 and .10 <= abs(q['delta']) <= .90 and
                abs(q['iv_error']) <= .01 and (q['ask']-q['bid'])/((q['ask']+q['bid'])/2) <= .20 and
                abs(q['underlying_price']/reference-1) <= .01):
            continue
        key = (q['strike'], q['right'])
        require(key not in eligible, 'Duplicate qualified option contract')
        eligible[key] = q
    strikes = {k[0] for k in eligible if (k[0], 'CALL') in eligible and (k[0], 'PUT') in eligible}
    require(bool(strikes), 'No qualified closing ATM pair on '+day)
    strike = min(strikes, key=lambda k: (abs(k-reference), k))
    pair = [eligible[(strike, right)] for right in ('CALL', 'PUT')]
    fields = ('symbol', 'expiration', 'strike', 'right', 'timestamp', 'underlying_timestamp', 'bid', 'ask', 'delta', 'implied_vol', 'iv_error', 'underlying_price')
    return dict(annual_iv_decimal=mean(q['implied_vol'] for q in pair), strike=strike, source_rows=len(rows), eligible_rows=len(eligible),
                contracts=[{k: q[k].isoformat() if isinstance(q[k], dt.datetime) else q[k] for k in fields} for q in pair])


def prepare(ticker, day, data):
    """Prepare distances using only the exact 20 completed prior cash sessions."""
    ticker = symbol(ticker)
    require(data['symbol'] == ticker, 'Wrong candle-data instrument')
    dates = data['prior'][day]
    calendar_dates = sorted(d for d in data['session'] if d < day)[-20:]
    require(len(dates) == 20 and dates == sorted(set(dates)) == calendar_dates, 'Expected exact prior 20 cash sessions')
    for i, date in enumerate(dates):
        opening, closing = [model.stamp(date, data['session'][date][k]) for k in ('open', 'close')]
        touch.helpers.validate_cash(data['history'][date], opening, closing)
        pair = data['options'][date]
        contracts = pair['contracts']
        expected_expiry = dates[i+1] if i+1 < len(dates) else day
        require(len(contracts) == 2 and {q['right'] for q in contracts} == {'CALL', 'PUT'} and contracts[0]['strike'] == contracts[1]['strike'], 'Invalid ATM pair')
        for q in contracts:
            require(q['symbol'] == ticker and q['expiration'] == expected_expiry, 'Wrong option instrument/next-session expiry')
            require(dt.datetime.fromisoformat(q['timestamp']) == closing-dt.timedelta(minutes=1), 'Wrong prior cash option clock')
            require(opening <= dt.datetime.fromisoformat(q['underlying_timestamp']) <= closing-dt.timedelta(minutes=1), 'Underlying timestamp outside prior cash')
        require(pair['annual_iv_decimal'] == mean(q['implied_vol'] for q in contracts), 'Closing IV differs from its pair')
        decoded = [dict(q, timestamp=dt.datetime.fromisoformat(q['timestamp']), underlying_timestamp=dt.datetime.fromisoformat(q['underlying_timestamp'])) for q in contracts]
        checked = closing_pair(ticker, decoded, date, expected_expiry, data['session'][date], data['history'][date][-1]['close'])
        require(checked['contracts'] == contracts and checked['annual_iv_decimal'] == pair['annual_iv_decimal'], 'Unqualified prepared option pair')
    forecast = touch.predict(day, read(model.V1/'contract.json'), data)
    return dict(symbol=ticker, prior_forecast=forecast)


def at_cash_open(prepared, opening_price, opening_at):
    ticker = symbol(prepared['symbol'])
    projected = model.project(prepared['prior_forecast'], opening_price, opening_at)
    return dict(symbol=ticker, forecast=projected)


def daily_record(projected, options, session, sources):
    """Detailed JSON; no cross-instrument probabilities or invented live data."""
    ticker, f = symbol(projected['symbol']), projected['forecast']
    require(all(q['symbol'] == ticker for d in f['input_dates'] for q in options[d]['contracts']), 'Cross-instrument JSON inputs')
    return dict(schema_version='pivot-algorithm-daily-v1', generated_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                instrument=dict(symbol=ticker, asset_class='equity_ETF', currency='USD'),
                session=dict(date=f['target_date'], timezone='America/New_York', open=model.stamp(f['target_date'], session['open']).isoformat(),
                             close=model.stamp(f['target_date'], session['close']).isoformat(), cash_minutes=f['cash_minutes']),
                forecast=copy.deepcopy(f),
                definitions=dict(P='Observed first cash-minute opening price; automatic anchor.', R='R1-R3 are above P.', S='S1-S3 are below P.',
                                 freeze='Distances prepared at reconstructed 09:25 ET; admit cash open at 09:30 ET once; levels then fixed.'),
                calculation=dict(formula='prior_cash_close * sqrt(mean(prior_20_variances_390) * cash_minutes/390) * latest_prior_closing_IV/mean(prior_20_closing_IV)',
                                 variance='Sum squared log returns from first cash bar open through successive cash-minute closes, normalized by 390/bar_count.',
                                 multipliers=MULTIPLIERS, rounding='Original Decimal ROUND_HALF_UP cent levels, shifted onto the eight-decimal cash-open anchor.',
                                 prior_option_pairs={d: copy.deepcopy(options[d]) for d in f['input_dates']},
                                 offsets_dollars={k: float(Decimal(str(v))-Decimal(str(f['P']))) for k, v in f['levels'].items()}),
                probabilities=dict(calibrated_touch_probability=None, status='not_calibrated_for_this_instrument',
                                   note='Nominal Brownian multipliers do not establish the chance of either R1 or S1 touching. QQQ rates are not SPY rates.'),
                data_quality=dict(source='ThetaData', archive='retrospective_latest', historical_publication_vintage='unverified', live_freshness='unverified',
                                  input_scope='Only completed prior cash sessions for spacing; current cash opening price only for P.',
                                  availability_convention='Prior cash records assumed available by 17:15 ET; actual archived receipt times unknown.',
                                  unused_inputs=['volume', 'other Greeks', 'VWAP', 'value areas', 'ADX', 'RVOL'], sources=copy.deepcopy(sources)),
                system_status=dict(mode='historical_reconstruction', live_open_collector=False, orders=False, fitting=False, parameter_searches=0))
