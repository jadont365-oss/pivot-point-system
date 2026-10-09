"""Per-symbol ThetaData archives and daily JSON for the shared pivot algorithm."""
import argparse
import calendar
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import importlib.util
import logging
from pathlib import Path

import polars as pl

import algorithm as alg

spec = importlib.util.spec_from_file_location('existing_secure_theta_collector', alg.ROOT/'outputs/qqq_range/collect.py')
secure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(secure)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def bind(paths):
    return {p.relative_to(alg.ROOT).as_posix(): alg.digest(p) for p in paths}


def verify(files):
    for name, sha in files.items():
        alg.require(alg.digest(alg.ROOT/name) == sha, 'Changed bound source: '+name)


def rules(ticker, start, end, folder):
    alg.require(start <= end < dt.datetime.now(alg.touch.NY).date(), 'Historical run requires ordered dates before today')
    first = start-dt.timedelta(days=60)
    receipts = alg.read(alg.ROOT/'outputs/qqq_range/collection_status.json')['requests']
    specials, calendars = {}, {}
    for year in range(first.year, end.year+1):
        receipt = next(r for r in receipts if r['name'] == 'calendar_'+str(year))
        path = alg.ROOT/'outputs/qqq_range/data'/('calendar_'+str(year)+'.parquet')
        alg.require(alg.digest(path) == receipt['sha256'], 'Provider calendar hash changed')
        calendars[path.relative_to(alg.ROOT).as_posix()] = receipt['sha256']
        specials.update({r['date']: r for r in pl.read_parquet(path).to_dicts()})
    roster = []
    day = first
    while day <= end:
        event = specials.get(str(day), {})
        if day.weekday() < 5 and event.get('type') != 'full_close':
            roster.append(dict(date=str(day), open=event.get('open') or '09:30:00', close=event.get('close') or '16:00:00'))
        day += dt.timedelta(days=1)
    targets = [r['date'] for r in roster if str(start) <= r['date'] <= str(end)]
    alg.require(bool(targets), 'No cash sessions in requested range')
    prior = {d: [r['date'] for r in roster if r['date'] < d][-20:] for d in targets}
    alg.require(all(len(ds) == 20 for ds in prior.values()), 'Insufficient prior cash history')
    needed = sorted({d for ds in prior.values() for d in ds})
    price_windows = []
    cursor = dt.date.fromisoformat(needed[0])
    while cursor <= end:
        last = min(dt.date(cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1]), end)
        price_windows.append([str(cursor), str(last)])
        cursor = last+dt.timedelta(days=1)
    modules = [Path(alg.__file__), Path(__file__), Path(secure.__file__), Path(alg.model.__file__), Path(alg.touch.__file__),
               Path(alg.model.previous.__file__), Path(alg.touch.helpers.__file__), Path(alg.touch.helpers.engine.__file__), alg.model.V1/'contract.json']
    value = dict(created_utc=now(), symbol=ticker, targets=targets, prior_dates=prior, roster=roster, price_windows=price_windows,
                 lookback=20, multipliers=alg.MULTIPLIERS, P='observed cash open', distance_inputs='completed prior cash sessions only',
                 purpose='functional historical development check; not pristine unseen data or calibrated SPY accuracy', parameter_searches=0,
                 missing_data_policy='Fail; no omitted sessions or interpolated prices', files={**calendars, **bind(modules)})
    path = folder/'rules_freeze.json'
    if path.exists():
        previous = alg.read(path)
        alg.require(all(previous[k] == value[k] for k in value if k != 'created_utc'), 'Run rules changed')
        return previous
    alg.require(not (folder/'data').exists(), 'Data predates new rules')
    secure.atomic_json(path, value)
    return value


def collect(folder, frozen):
    from thetadata import ThetaClient
    ticker = frozen['symbol']
    sessions = {r['date']: r for r in frozen['roster']}
    following = {a['date']: b['date'] for a, b in zip(frozen['roster'], frozen['roster'][1:])}
    jobs = []
    for start, end in frozen['price_windows']:
        jobs.append(('prices_'+start[:7].replace('-', '_'), 'stock_history_ohlc', dict(symbol=ticker, interval='1m', start_date=start, end_date=end,
                     start_time='09:30:00', end_time='16:00:00', venue='utp_cta')))
    for day in sorted({d for ds in frozen['prior_dates'].values() for d in ds}):
        clock = (dt.datetime.fromisoformat(day+'T'+sessions[day]['close'])-dt.timedelta(minutes=1)).time().isoformat()
        jobs.append(('greeks_'+day, 'option_history_greeks_first_order', dict(symbol=ticker, expiration=following[day], date=day, interval='1m', strike='*', right='both',
                     start_time=clock, end_time=clock, annual_dividend=0.0, rate_type='sofr', version='latest')))
    data = folder/'data'
    data.mkdir(exist_ok=True)
    manifest = folder/'collection.json'
    state = alg.read(manifest) if manifest.exists() else dict(state='collecting', symbol=ticker, requests=[])
    alg.require(state['symbol'] == ticker, 'Wrong cached instrument')
    cache = {r['name']: r for r in state['requests']}
    pending = []
    for name, endpoint, params in jobs:
        path = data/(name+'.parquet')
        if path.exists():
            r = cache.get(name)
            alg.require(r is not None and r['endpoint'] == endpoint and r['parameters'] == params and r['sha256'] == alg.digest(path), 'Changed or mismatched input cache')
        else:
            pending.append((name, endpoint, params))
    key = ''
    logging.disable(logging.CRITICAL)
    try:
        key = secure.load_key()
        client = ThetaClient(api_key=key, dataframe_type='polars')
        def receive(job):
            name, endpoint, params = job
            actual = {k: dt.date.fromisoformat(v) if k in ('date', 'expiration', 'start_date', 'end_date') else v for k, v in params.items()}
            return job, getattr(client, endpoint)(**actual)
        with ThreadPoolExecutor(max_workers=3) as pool:
            for future in as_completed([pool.submit(receive, j) for j in pending]):
                (name, endpoint, params), frame = future.result()
                alg.require(frame.height > 0, 'Empty required provider receipt: '+name)
                path = data/(name+'.parquet')
                temporary = path.with_suffix('.tmp')
                frame.write_parquet(temporary)
                temporary.replace(path)
                cache[name] = dict(name=name, endpoint=endpoint, parameters=params, file=path.relative_to(alg.ROOT).as_posix(), rows=frame.height,
                                   sha256=alg.digest(path), schema={k: str(v) for k, v in frame.schema.items()}, recorded_utc=now())
                state.update(requests=[r for _, r in sorted(cache.items())])
                secure.atomic_json(manifest, state)
                if len(cache) % 8 == 0 or len(cache) == len(jobs):
                    print(f'{ticker} receipts: {len(cache)}/{len(jobs)}; no touch scoring.', flush=True)
        alg.require(set(cache) == {j[0] for j in jobs}, 'Incomplete provider receipts')
        state.update(state='received', rules_freeze_sha256=alg.digest(folder/'rules_freeze.json'), total_rows=sum(r['rows'] for r in cache.values()), completed_utc=now())
        secure.atomic_json(manifest, state)
        print(f'{ticker} received {len(cache)} requests / {state["total_rows"]} raw rows.', flush=True)
        return state
    except Exception as error:
        state.update(state='failed', error=secure.safe_error(error, key) if key else 'Credential initialization failed')
        secure.atomic_json(folder/'collection_failure.json', state)
        raise RuntimeError(state['error']) from None


def compute(folder, frozen, receipts):
    alg.require(receipts['state'] == 'received' and receipts['symbol'] == frozen['symbol'] and receipts['rules_freeze_sha256'] == alg.digest(folder/'rules_freeze.json'), 'Unbound receipt manifest')
    alg.require(all(r['parameters']['symbol'] == frozen['symbol'] for r in receipts['requests']), 'Cross-instrument provider receipt')
    sources = {r['file']: r['sha256'] for r in receipts['requests']}
    verify({**frozen['files'], **sources})
    alg.require(frozen['created_utc'] < min(r['recorded_utc'] for r in receipts['requests']), 'Requests precede rules')
    sessions = {r['date']: r for r in frozen['roster']}
    following = {a['date']: b['date'] for a, b in zip(frozen['roster'], frozen['roster'][1:])}
    frames = {a[:7]: pl.read_parquet(folder/'data'/('prices_'+a[:7].replace('-', '_')+'.parquet')) for a, b in frozen['price_windows']}
    def cash(day):
        opening, closing = [alg.model.stamp(day, sessions[day][k]) for k in ('open', 'close')]
        rows = frames[day[:7]].filter((pl.col('timestamp') >= opening) & (pl.col('timestamp') < closing)).sort('timestamp').to_dicts()
        alg.touch.helpers.validate_cash(rows, opening, closing)
        return rows
    needed = sorted({d for ds in frozen['prior_dates'].values() for d in ds})
    history = {d: cash(d) for d in needed}
    options = {d: alg.closing_pair(frozen['symbol'], pl.read_parquet(folder/'data'/('greeks_'+d+'.parquet')).to_dicts(), d, following[d], sessions[d], history[d][-1]['close']) for d in needed}
    data = dict(symbol=frozen['symbol'], prior=frozen['prior_dates'], history=history, options=options, session=sessions)
    prepared = [alg.prepare(frozen['symbol'], day, data) for day in frozen['targets']]
    alg.write(folder/'prepared.json', prepared)
    alg.write(folder/'qualified_options.json', options)
    alg.write(folder/'distances_freeze.json', dict(created_utc=now(), prepared_sha256=alg.digest(folder/'prepared.json'), qualified_options_sha256=alg.digest(folder/'qualified_options.json'),
              rules_freeze_sha256=alg.digest(folder/'rules_freeze.json'), sources=sources))
    projections = []
    for p in prepared:
        day = p['prior_forecast']['target_date']
        opening = frames[day[:7]].filter(pl.col('timestamp') == alg.model.stamp(day, '09:30:00')).select('timestamp', 'open').to_dicts()
        alg.require(len(opening) == 1, 'Missing/duplicate observed cash opening')
        projections.append(alg.at_cash_open(p, opening[0]['open'], opening[0]['timestamp']))
    alg.write(folder/'forecasts.json', projections)
    daily_dir = folder/'daily'
    daily_dir.mkdir(exist_ok=True)
    for p in projections:
        day = p['forecast']['target_date']
        alg.write(daily_dir/(frozen['symbol']+'_'+day+'.json'), alg.daily_record(p, options, sessions[day], sources))
    alg.write(folder/'candidate_freeze.json', dict(created_utc=now(), stage='before target cash touch scoring', forecasts_sha256=alg.digest(folder/'forecasts.json'),
              distances_freeze_sha256=alg.digest(folder/'distances_freeze.json'), daily_files={p.name: alg.digest(p) for p in daily_dir.iterdir()}, parameter_searches=0))
    daily = []
    for p, projected in zip(prepared, projections):
        f = projected['forecast']
        rows = cash(f['target_date'])
        alg.require(alg.at_cash_open(p, rows[0]['open'], rows[0]['timestamp']) == projected, 'Opening-only projection differs')
        events = {name: alg.touch.score(rows, f['levels'][name], name) for name in alg.model.TARGETS}
        frame = pl.DataFrame(rows)
        for name, event in events.items():
            hit = frame.filter((pl.col('low')-1e-8 <= f['levels'][name]) & (pl.col('high')+1e-8 >= f['levels'][name]))
            alg.require(event['touched'] == (hit.height > 0) and event['first_touch_minute'] == (hit['timestamp'][0].isoformat() if hit.height else None), 'Independent touch mismatch')
        r, s = events['R1']['touched'], events['S1']['touched']
        daily.append(dict(date=f['target_date'], cash_minutes=len(rows), events=events, R1_touch=r, S1_touch=s, either_touch=r or s, both_touch=r and s))
    verify({**frozen['files'], **sources})
    counts = {k: sum(d[k] for d in daily) for k in ('R1_touch', 'S1_touch', 'either_touch', 'both_touch')}
    alg.require(counts['either_touch'] == counts['R1_touch']+counts['S1_touch']-counts['both_touch'], 'Count reconciliation failed')
    report = dict(created_utc=now(), symbol=frozen['symbol'], purpose=frozen['purpose'], sessions=len(daily), target_cash_minutes=sum(d['cash_minutes'] for d in daily),
                  prior_cash_sessions=len(needed), prior_cash_minutes=sum(len(r) for r in history.values()), qualified_option_pairs=len(options), received_requests=len(receipts['requests']),
                  counts=counts, daily=daily, parameter_searches=0, calibrated_probability=None, live_ready=False, P_excluded_from_prediction_scores=True,
                  checks=dict(source_hashes='passed', full_cash_coverage='passed', own_symbol_options='passed', past_only_inputs='passed', independent_touches='passed'))
    alg.write(folder/'report.json', report)
    print(f'{frozen["symbol"]} VERIFIED: {len(daily)} sessions, {len(options)} prior pairs, {sum(len(r) for r in history.values())} prior cash minutes; {counts}. Functional development check only.', flush=True)


def main():
    parser = argparse.ArgumentParser(description='QQQ/SPY historical cash-open pivots; separate ThetaData inputs and detailed daily JSON')
    parser.add_argument('--symbol', required=True, choices=alg.SUPPORTED)
    parser.add_argument('--start', required=True, type=dt.date.fromisoformat)
    parser.add_argument('--end', required=True, type=dt.date.fromisoformat)
    args = parser.parse_args()
    folder = alg.HERE/'runs'/(args.symbol+'_'+str(args.start)+'_'+str(args.end))
    folder.mkdir(parents=True, exist_ok=True)
    alg.require(not (folder/'report.json').exists(), 'Recorded run already completed; preserve its first result')
    frozen = rules(args.symbol, args.start, args.end, folder)
    print(f'{args.symbol} RULES_FROZEN: {len(frozen["targets"])} targets before provider requests.', flush=True)
    compute(folder, frozen, collect(folder, frozen))


if __name__ == '__main__':
    main()
