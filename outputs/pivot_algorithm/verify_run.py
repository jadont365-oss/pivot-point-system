"""Independent recorded-data arithmetic, touches, clocks and symbol audit."""
import argparse
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import polars as pl

ROOT = Path(__file__).resolve().parents[2]
NY = ZoneInfo('America/New_York')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def check(folder):
    frozen, receipts, report = [read(folder/name) for name in ('rules_freeze.json', 'collection.json', 'report.json')]
    prepared, forecasts, options = [read(folder/name) for name in ('prepared.json', 'forecasts.json', 'qualified_options.json')]
    distances, candidate = [read(folder/name) for name in ('distances_freeze.json', 'candidate_freeze.json')]
    ticker = frozen['symbol']
    assert ticker in ('QQQ', 'SPY') and report['symbol'] == receipts['symbol'] == ticker
    for name, expected in {**frozen['files'], **{r['file']: r['sha256'] for r in receipts['requests']}}.items():
        path = (ROOT/name).resolve()
        assert path.is_relative_to(ROOT) and sha(path) == expected
    assert all(r['parameters']['symbol'] == ticker for r in receipts['requests'])
    assert frozen['created_utc'] < min(r['recorded_utc'] for r in receipts['requests'])
    assert distances['created_utc'] <= candidate['created_utc'] <= report['created_utc']
    for name, expected in (('rules_freeze.json', receipts['rules_freeze_sha256']), ('prepared.json', distances['prepared_sha256']),
                           ('qualified_options.json', distances['qualified_options_sha256']), ('forecasts.json', candidate['forecasts_sha256']),
                           ('distances_freeze.json', candidate['distances_freeze_sha256'])):
        assert sha(folder/name) == expected
    sessions = {r['date']: r for r in frozen['roster']}
    following = {a['date']: b['date'] for a, b in zip(frozen['roster'], frozen['roster'][1:])}
    frames = {a[:7]: pl.read_parquet(folder/'data'/('prices_'+a[:7].replace('-', '_')+'.parquet')) for a, b in frozen['price_windows']}
    def cash(day):
        opening, closing = [dt.datetime.fromisoformat(day+'T'+sessions[day][k]).replace(tzinfo=NY) for k in ('open', 'close')]
        rows = frames[day[:7]].filter((pl.col('timestamp') >= opening) & (pl.col('timestamp') < closing)).sort('timestamp').to_dicts()
        assert [r['timestamp'] for r in rows] == [opening+dt.timedelta(minutes=i) for i in range(int((closing-opening).total_seconds()/60))]
        for r in rows:
            assert all(math.isfinite(r[k]) and r[k] > 0 for k in ('open', 'high', 'low', 'close'))
            assert r['low'] <= min(r['open'], r['close'])+1e-8 and r['high'] >= max(r['open'], r['close'])-1e-8
        return rows
    history = {d: cash(d) for ds in frozen['prior_dates'].values() for d in ds}
    assert [p['forecast']['target_date'] for p in forecasts] == frozen['targets'] == [p['prior_forecast']['target_date'] for p in prepared]
    counts = dict(R1_touch=0, S1_touch=0, either_touch=0, both_touch=0)
    for prior, projected, daily in zip(prepared, forecasts, report['daily']):
        assert projected['symbol'] == prior['symbol'] == ticker
        f, old = projected['forecast'], prior['prior_forecast']
        day = f['target_date']
        dates = sorted(d for d in sessions if d < day)[-20:]
        assert len(dates) == 20 and dates == frozen['prior_dates'][day] == f['input_dates']
        variances = []
        for d in dates:
            rows = history[d]
            prices = [rows[0]['open']]+[r['close'] for r in rows]
            variances.append(sum(math.log(b/a)**2 for a, b in zip(prices, prices[1:]))*390/len(rows))
            pair = options[d]
            assert pair['annual_iv_decimal'] == mean(q['implied_vol'] for q in pair['contracts'])
            snapshot = dt.datetime.fromisoformat(d+'T'+sessions[d]['close']).replace(tzinfo=NY)-dt.timedelta(minutes=1)
            assert all(q['symbol'] == ticker and q['expiration'] == following[d] and dt.datetime.fromisoformat(q['timestamp']) == snapshot for q in pair['contracts'])
        rows = cash(day)
        reference = history[dates[-1]][-1]['close']
        scale = reference*math.sqrt(mean(variances)*len(rows)/390)*options[dates[-1]]['annual_iv_decimal']/mean(options[d]['annual_iv_decimal'] for d in dates)
        assert math.isclose(scale, f['candidate_cash_sigma_dollars'], rel_tol=1e-12)
        assert math.isclose(scale, old['candidate_cash_sigma_dollars'], rel_tol=1e-12)
        opening = Decimal(str(rows[0]['open'])).quantize(Decimal('.00000001'))
        reference_p = Decimal(str(reference)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        assert Decimal(str(f['P'])) == opening and f['cash_minutes'] == len(rows)
        assert f['as_of'] == rows[0]['timestamp'].isoformat()
        for name, z in frozen['multipliers'].items():
            baseline = Decimal(str(reference+(1 if name[0] == 'R' else -1)*z*scale)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            price = opening+baseline-reference_p
            assert Decimal(str(f['levels'][name])) == price
            hit = [r['timestamp'].isoformat() for r in rows if r['low']-1e-8 <= float(price) <= r['high']+1e-8]
            assert daily['events'][name]['touched'] == bool(hit)
            assert daily['events'][name]['first_touch_minute'] == (hit[0] if hit else None)
        r, s = daily['events']['R1']['touched'], daily['events']['S1']['touched']
        for name, flag in dict(R1_touch=r, S1_touch=s, either_touch=r or s, both_touch=r and s).items():
            assert daily[name] == flag
            counts[name] += flag
        filename = ticker+'_'+day+'.json'
        record = read(folder/'daily'/filename)
        assert sha(folder/'daily'/filename) == candidate['daily_files'][filename]
        assert record['instrument']['symbol'] == ticker and record['forecast'] == f
        assert record['probabilities']['calibrated_touch_probability'] is None
        assert all(q['symbol'] == ticker for pair in record['calculation']['prior_option_pairs'].values() for q in pair['contracts'])
    assert len(forecasts) == report['sessions'] and counts == report['counts']
    assert report['prior_cash_minutes'] == sum(len(r) for r in history.values())
    assert report['P_excluded_from_prediction_scores'] and report['calibrated_probability'] is None and report['parameter_searches'] == 0
    print(f'PASS: {ticker}; {len(forecasts)} recorded forecasts, {len(forecasts)*6} independent target checks, {len(options)} own-symbol closing pairs, prior/open-only arithmetic, chronology, daily JSON and bound sources.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    check(parser.parse_args().folder.resolve())
