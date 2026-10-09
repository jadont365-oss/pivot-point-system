"""Use v1 formula with options measured before the prior cash close."""
from copy import deepcopy
import datetime as dt
import math
from statistics import NormalDist
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
NORMAL = NormalDist()


def gamma_proxy(delta, iv, underlying, years):
    """Zero-dividend Black-Scholes gamma inferred from first-order delta and IV."""
    if not all(math.isfinite(float(value)) for value in (delta,iv,underlying,years)) or not 0 < abs(delta) < 1 or min(iv,underlying,years) <= 0:
        return float('nan')
    d1 = NORMAL.inv_cdf(abs(delta))
    return math.exp(-.5*d1*d1)/math.sqrt(2*math.pi)/(underlying*iv*math.sqrt(years))


def rebuild(base, quotes, interests, session, target_close, engine):
    close = engine.stamp(session['date'],session['close'])
    opening = engine.stamp(session['date'],session['open'])
    snapshot = close-dt.timedelta(minutes=1)
    expiry = engine.stamp(base['target_date'],target_close)
    cash_quotes, counts = [],dict(raw_quotes=len(quotes),outside_final_cash_minute=0,noncash_underlying=0)
    for raw in quotes:
        stamp,under_at = raw.get('timestamp'),raw.get('underlying_timestamp')
        if not isinstance(stamp,dt.datetime) or stamp.tzinfo is None or stamp != snapshot:
            counts['outside_final_cash_minute'] += 1
            continue
        if not isinstance(under_at,dt.datetime) or under_at.tzinfo is None or not opening <= under_at <= snapshot:
            counts['noncash_underlying'] += 1
            continue
        row = dict(raw)
        try:
            years = (expiry.astimezone(dt.timezone.utc)-stamp.astimezone(dt.timezone.utc)).total_seconds()/(365*86400)
            row['gamma'] = gamma_proxy(float(row['delta']),float(row['implied_vol']),float(row['underlying_price']),years)
        except (KeyError,TypeError,ValueError,OverflowError):
            row['gamma'] = float('nan')
        cash_quotes.append(row)
    prior_oi = [row for row in interests if isinstance(row.get('timestamp'),dt.datetime) and row['timestamp'].tzinfo is not None and row['timestamp'] <= close]
    if not cash_quotes or not prior_oi:
        raise ValueError('Cash-only option quotes/OI unavailable for '+base['target_date'])
    options = engine.admit_options(cash_quotes,prior_oi,session,base['target_date'],target_close,base['inputs']['spot'])
    options.update(quote_window=[snapshot.isoformat(),close.isoformat()],oi_cutoff=close.isoformat(),
                   calendar_days=(expiry.astimezone(dt.timezone.utc)-close.astimezone(dt.timezone.utc)).total_seconds()/86400,
                   expiry_assumed_at=expiry.isoformat(),gamma_source='Black-Scholes zero-dividend proxy from provider delta/IV; not provider gamma',
                   snapshot_timestamp_kind='interval_label',event_age_seconds='unknown',
                   cash_filter_counts=dict(**counts,raw_oi=len(interests),oi_after_prior_close_excluded=len(interests)-len(prior_oi)),
                   oi_semantics='latest publication available by prior cash close; represents older completed trading-session inventory')
    for contract in options['eligible_contracts']:
        contract['gamma_source'] = options['gamma_source']
    result = deepcopy(base)
    result['inputs']['options'] = options
    result.update(engine.calculate(result['inputs']))
    result['prior_cash_session'] = dict(date=session['date'],open=opening.isoformat(),close=close.isoformat(),timezone='America/New_York')
    result['input_scope'] = 'completed_prior_cash_sessions_only'
    result['base_variant'] = 'cash_options_rebuilt_v1_formula'
    return result
