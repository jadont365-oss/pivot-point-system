"""Fixed, retrospective QQQ pivot-reaction comparison; no trades or fitting."""
import collections
import csv
import datetime as dt
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import statistics
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
PIVOTS = HERE.parent
OUTPUTS = PIVOTS.parent
PROJECT = OUTPUTS.parent
NY = ZoneInfo('America/New_York')
NAMES = ('S3', 'S2', 'S1', 'P', 'R1', 'R2', 'R3')
STATUSES = ('bounce_first', 'break_first', 'ambiguous', 'neither', 'censored', 'no_touch')
spec = importlib.util.spec_from_file_location('frozen_pivot_engine', PIVOTS/'engine.py')
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)

def require(condition, message):
    if not condition:
        raise ValueError(message)

def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False)+'\n', encoding='utf-8')

def validate_cash(rows, opening, closing):
    expected = [opening+dt.timedelta(minutes=i) for i in range(int((closing-opening).total_seconds()/60))]
    require([r['timestamp'] for r in rows] == expected, 'Incomplete, duplicate or misordered cash minutes')
    for r in rows:
        values = [r[k] for k in ('open', 'high', 'low', 'close')]
        require(all(math.isfinite(v) and v > 0 for v in values), 'Invalid cash prices')
        require(r['low'] <= min(r['open'],r['close'])+1e-8 and r['high'] >= max(r['open'],r['close'])-1e-8,
                'Invalid cash OHLC')

def score_level(rows, level, threshold, horizon=30):
    require(threshold > 0 and horizon > 0, 'Invalid reaction settings')
    result = dict(status='no_touch', touch_index=None, touch_time=None, approach_side=None,
                  event_time=None, favorable_dollars=None, adverse_dollars=None, available_future_minutes=None)
    for i, r in enumerate(rows):
        previous = rows[i-1]['close'] if i else r['open']
        if abs(previous-level) < .01-1e-9 or not r['low']-1e-9 <= level <= r['high']+1e-9:
            continue
        side = 1 if previous > level else -1
        future = rows[i+1:i+1+horizon]
        result.update(touch_index=i, touch_time=r['timestamp'].isoformat(),
                      approach_side='above' if side == 1 else 'below', available_future_minutes=len(future))
        if len(future) < horizon:
            result['status'] = 'censored'
            return result
        favorable = [b['high']-level if side == 1 else level-b['low'] for b in future]
        adverse = [level-b['low'] if side == 1 else b['high']-level for b in future]
        result.update(status='neither', favorable_dollars=max(0.,max(favorable)), adverse_dollars=max(0.,max(adverse)))
        for b, f, a in zip(future, favorable, adverse):
            bounced, broken = f >= threshold-1e-9, a >= threshold-1e-9
            if bounced or broken:
                result.update(status='ambiguous' if bounced and broken else 'bounce_first' if bounced else 'break_first',
                              event_time=b['timestamp'].isoformat())
                break
        return result
    return result

def load_data(before_outcomes=None):
    import polars as pl
    contract = json.loads((HERE/'contract.json').read_text())
    dates = contract['sessions']
    require(len(dates) == 5 and all(dt.date.fromisoformat(d).weekday() == i for i,d in enumerate(dates)), 'Expected full Monday-Friday week')
    base_report = json.loads((PIVOTS/'report.json').read_text())
    manifest = {}
    def bind(path, expected=None):
        key = path.relative_to(PROJECT).as_posix()
        actual = digest(path)
        require(expected is None or actual == expected, 'Hash mismatch: '+key)
        manifest[key] = actual
    bind(PIVOTS/'report.json')
    bind(PIVOTS/'engine.py', base_report['engine_sha256'])
    bind(PIVOTS/'method.json', base_report['method_sha256'])
    bind(PIVOTS/'forecasts.json', base_report['forecast_sha256'])
    bind(OUTPUTS/'qqq_range'/'sessions.json', base_report['session_roster_sha256'])
    all_forecasts = json.loads((PIVOTS/'forecasts.json').read_text())
    forecasts = [f for f in all_forecasts if f['target_date'] in dates]
    require([f['target_date'] for f in forecasts] == dates, 'Incomplete matched forecast week')
    feature_hashes = {r['file']:r['sha256'] for r in base_report['feature_manifest']}
    features = {}
    def feature(day):
        if day not in features:
            path = PIVOTS/'features'/(day+'.json')
            bind(path, feature_hashes['features/'+day+'.json'])
            features[day] = json.loads(path.read_text())
            require(not features[day]['invalid_reasons'], 'Invalid session feature '+day)
        return features[day]
    for f in forecasts:
        require(f['mode'] == 'full_composite_v1' and f['anchor_date'] < f['target_date'], 'Invalid forecast chronology')
        require(f['engine_sha256'] == base_report['engine_sha256'] and f['method_sha256'] == base_report['method_sha256'], 'Forecast model mismatch')
        require(f['as_of'] == engine.stamp(f['target_date'],'08:00:00').isoformat(), 'Wrong forecast cutoff')
        inp = f['inputs']
        for name in ('profile_dates', 'hv_close_dates', 'rvol_reference_dates'):
            require(all(d < f['target_date'] for d in inp[name]), 'Target/future session leaked into '+name)
        require(len(inp['profile_dates']) == 5 and len(inp['hv_close_dates']) == 21 and len(inp['rvol_reference_dates']) == 20, 'Wrong prior-history counts')
        require(inp['options']['greek_publication_state'] == 'reconstructed_retrospective_latest', 'Unknown Greek vintage')
        cutoff = dt.datetime.fromisoformat(f['as_of'])
        for row in inp['options']['eligible_contracts']:
            require(dt.datetime.fromisoformat(row['quote_timestamp']).date().isoformat() == f['anchor_date'], 'Future option quote')
            require(dt.datetime.fromisoformat(row['oi_timestamp']) <= cutoff, 'Future OI')
        require(engine.calculate(inp)['levels'] == f['levels'], 'Composite calculation changed')
        require(engine.classic_pivots(feature(f['anchor_date'])) == f['classic_floor_reference'], 'Floor pivot reference mismatch')
    # Freeze exact selected predictions before reading target outcome prices/features.
    if before_outcomes:
        before_outcomes(forecasts)
    roster = {r['date']:r for r in json.loads((OUTPUTS/'qqq_range'/'sessions.json').read_text())}
    source_hashes = {r['file']:r['sha256'] for r in base_report['source_manifest']}
    frames = []
    for month in sorted({d[:7].replace('-','_') for d in dates}):
        path = OUTPUTS/'qqq_range'/'data'/('prices_'+month+'.parquet')
        bind(path, source_hashes[path.relative_to(OUTPUTS).as_posix()])
        frames.append(pl.read_parquet(path))
    records = pl.concat(frames).sort('timestamp').to_dicts()
    cash = {}
    for day in dates:
        session = roster[day]
        opening, closing = [engine.stamp(day,session[k]) for k in ('open','close')]
        rows = [r for r in records if opening <= r['timestamp'] < closing]
        validate_cash(rows, opening, closing)
        require(len(rows) == 390, 'Requested week must have full cash sessions')
        target = feature(day)
        observed = (rows[0]['open'],max(r['high'] for r in rows),min(r['low'] for r in rows),rows[-1]['close'])
        stored = tuple(target['cash_'+k] for k in ('open','high','low','close'))
        require(all(math.isclose(a,b,abs_tol=1e-8,rel_tol=0) for a,b in zip(observed,stored)), 'Cached outcome OHLC mismatch')
        require(observed[2] <= target['vwap'] <= observed[1], 'Invalid target VWAP')
        cash[day] = rows
    return dict(contract=contract, forecasts=forecasts, cash=cash, features=features,
                manifest=[dict(file=k,sha256=v) for k,v in sorted(manifest.items())])

def summarize(events):
    counts = {s:sum(e['status'] == s for e in events) for s in STATUSES}
    full = sum(counts[s] for s in STATUSES[:4])
    return dict(counts=counts, touches=full+counts['censored'], full_window_touches=full,
                clear_bounce_rate=counts['bounce_first']/full if full else None)

def evaluate(data):
    daily, events = [], []
    horizon = data['contract']['target']['horizon_minutes']
    fraction = data['contract']['target']['movement_threshold_fraction_of_cash_open']
    for forecast in data['forecasts']:
        day = forecast['target_date']
        rows = data['cash'][day]
        vwap = data['features'][day]['vwap']
        threshold = fraction*rows[0]['open']
        entry = dict(date=day, cash_open=rows[0]['open'], cash_high=max(r['high'] for r in rows),
                     cash_low=min(r['low'] for r in rows), cash_close=rows[-1]['close'], cash_vwap=vwap,
                     movement_threshold=threshold, models={})
        for model, levels in (('composite',forecast['levels']),('classic',forecast['classic_floor_reference'])):
            day_events = [dict(date=day,model=model,level_name=name,level=levels[name],threshold=threshold,
                               **score_level(rows,levels[name],threshold,horizon)) for name in NAMES]
            events.extend(day_events)
            entry['models'][model] = dict(levels=levels, P_vwap_abs_error=abs(levels['P']-vwap), **summarize(day_events))
        daily.append(entry)
    models = {}
    for model in ('composite','classic'):
        selected = [e for e in events if e['model'] == model]
        models[model] = dict(**summarize(selected), mean_P_vwap_abs_error=statistics.mean(d['models'][model]['P_vwap_abs_error'] for d in daily))
    return dict(sessions=data['contract']['sessions'], cash_minutes=sum(len(r) for r in data['cash'].values()),
                threshold_rule='0.10%of target cash open; next30minutes after first eligible touch; same rules for both sets',
                models=models, daily=daily, events=events)

def make_plot(data, result):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    fig, axes = plt.subplots(5,2,figsize=(14,15),sharex='row',sharey='row',layout='constrained')
    for i, day in enumerate(result['daily']):
        rows = data['cash'][day['date']]
        times = [r['timestamp'] for r in rows]
        for j, model in enumerate(('composite','classic')):
            ax = axes[i,j]
            ax.plot(times,[r['close'] for r in rows],color='#27374c',linewidth=1.,label='QQQ minute close')
            for name, price in day['models'][model]['levels'].items():
                color = '#009c7e' if name.startswith('S') else '#d15a66' if name.startswith('R') else '#b88a12'
                ax.axhline(price,color=color,linewidth=1.2 if name == 'P' else .8,linestyle='-' if model == 'composite' else '--',alpha=.8)
                ax.text(times[-1]+dt.timedelta(minutes=5),price,name,color=color,fontsize=8,va='center')
            ax.axhline(day['cash_vwap'],color='#7158a5',linestyle=':',linewidth=.9,label='Actual full-session VWAP')
            score = day['models'][model]
            title = 'Composite' if model == 'composite' else 'Standard floor pivots'
            ax.set_title(f"{day['date']} | {title} | clear bounces {score['counts']['bounce_first']}/{score['full_window_touches']}",fontsize=10)
            ax.set_xlim(times[0],times[-1]+dt.timedelta(minutes=25))
            ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[10,12,14,16],tz=NY))
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M',tz=NY))
            ax.grid(alpha=.15)
            if j == 0:
                ax.set_ylabel('QQQ dollars')
    axes[0,0].legend(loc='lower left',fontsize=7)
    fig.suptitle('Frozen QQQ pivots: September28-October2,2026\nFive-session price-reaction diagnostic; no fills, profitability or probability claim',fontsize=13)
    fig.savefig(HERE/'week_test.png',dpi=130)
    plt.close(fig)

def main():
    snapshot_time = None
    def freeze(forecasts):
        nonlocal snapshot_time
        write_json(HERE/'frozen_forecasts.json',forecasts)
        snapshot_time = dt.datetime.now(dt.timezone.utc).isoformat()
    data = load_data(freeze)
    result = evaluate(data)
    make_plot(data,result)
    with (HERE/'events.csv').open('w',newline='',encoding='utf-8') as stream:
        writer = csv.DictWriter(stream,fieldnames=list(result['events'][0]))
        writer.writeheader(); writer.writerows(result['events'])
    with (HERE/'daily.csv').open('w',newline='',encoding='utf-8') as stream:
        names = ['date','cash_open','cash_high','cash_low','cash_close','cash_vwap','movement_threshold']
        fields = names+[m+'_'+k for m in ('composite','classic') for k in ('P','P_vwap_abs_error','bounce_first','break_first','ambiguous','neither','censored','no_touch','full_window_touches')]
        writer = csv.DictWriter(stream,fieldnames=fields); writer.writeheader()
        for day in result['daily']:
            flat = {k:day[k] for k in names}
            for m in ('composite','classic'):
                row = day['models'][m]
                flat.update({m+'_P':row['levels']['P'],m+'_P_vwap_abs_error':row['P_vwap_abs_error'],m+'_full_window_touches':row['full_window_touches']})
                flat.update({m+'_'+k:v for k,v in row['counts'].items()})
            writer.writerow(flat)
    report = dict(status='five_session_retrospective_diagnostic_completed', **result,
                  contract_sha256=digest(HERE/'contract.json'), evaluator_sha256=digest(HERE/'run_week_test.py'),
                  checker_sha256=digest(HERE/'check_week_test.py'), source_manifest=data['manifest'],
                  predictions_snapshot_written_before_outcome_read_at=snapshot_time,
                  evaluated_at=dt.datetime.now(dt.timezone.utc).isoformat(), as_seen_vintage_certified=False,
                  sealed_holdout=False, fitting='none', parameter_search='none', profitability_status='not_tested',
                  general_accuracy_status='not_established',
                  limits=['Five dependent sessions and potentially overlapping level events; descriptive comparison only.',
                          'Different pivot sets have different touch opportunities; rates are not paired trade returns.',
                          'Minute OHLC overlaps are touch proxies; intraminute chronology may be unknown.',
                          'Retrospective latest EOD Greeks and reconstructed availability are not certified historical as-seen snapshots.',
                          'No option fills, spreads, slippage, fees or net returns tested; no probability calibration.',
                          'Underlying trade-tape completeness and correction handling remain unqualified.'],
                  artifact_sha256={name:digest(HERE/name) for name in ('frozen_forecasts.json','daily.csv','events.csv','week_test.png')})
    write_json(HERE/'report.json',report)
    write_json(HERE/'evaluation_exposure.json',dict(experiment_id=data['contract']['experiment_id'], consumed_sessions=report['sessions'],
               evaluated_at=report['evaluated_at'], use='retrospective_development_diagnostic',sealed_holdout=False,
               original_range_exposure_ledger='outputs/qqq_range/evaluation_exposure.json'))
    print('WEEK_EVALUATION_COMPLETED '+json.dumps(dict(sessions=5,cash_minutes=result['cash_minutes'],models=result['models'])))

if __name__ == '__main__':
    main()
