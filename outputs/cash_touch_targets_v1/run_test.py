"""Five reconstructed preopen cash-touch forecasts; no trades or fitting."""
import csv
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
import html
import importlib.util
import json
import math
from pathlib import Path
from statistics import NormalDist, mean

HERE = Path(__file__).resolve().parent
OUTPUTS = HERE.parent
PROJECT = OUTPUTS.parent
spec = importlib.util.spec_from_file_location('cash_week_helpers', OUTPUTS/'composite_pivots/week_test_2026-09-28_2026-10-02/run_week_test.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
require, digest, NY = helpers.require, helpers.digest, helpers.NY
stamp = helpers.engine.stamp
NAMES = ('S3', 'S2', 'S1', 'P', 'R1', 'R2', 'R3')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def cents(value):
    return float(Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def cash_variance(rows):
    previous = rows[0]['open']
    squared = []
    for row in rows:
        squared.append(math.log(row['close']/previous)**2)
        previous = row['close']
    return sum(squared)*390/len(rows)


def levels(reference, sigma, multipliers):
    require(math.isfinite(sigma) and sigma > 0, 'Invalid projected cash sigma')
    result = {name:cents(reference + (1 if name.startswith('R') else -1)*multipliers[name]*sigma)
              for name in multipliers}
    result['P'] = cents(reference)
    require(all(result[a] < result[b] for a,b in zip(NAMES, NAMES[1:])), 'Rounded levels collide')
    return result


def score(rows, price, name):
    touches = [i for i,r in enumerate(rows) if r['low']-1e-8 <= price <= r['high']+1e-8]
    direction = 1 if name.startswith('R') else -1 if name.startswith('S') else 0
    opened_beyond = direction*(rows[0]['open']-price) > 1e-8 if direction else False
    reached = max(r['high'] for r in rows) >= price-1e-8 if direction == 1 else min(r['low'] for r in rows) <= price+1e-8 if direction == -1 else bool(touches)
    crossed = any((a['close']-price)*(b['open']-price) < 0 for a,b in zip(rows,rows[1:]))
    return dict(touched=bool(touches), first_touch_minute=rows[touches[0]]['timestamp'].isoformat() if touches else None,
                opened_beyond=opened_beyond, threshold_reached=reached,
                crossed_between_cash_bars=crossed, status='bar_touch' if touches else 'opened_beyond_untouched' if opened_beyond else 'crossed_between_bars_unconfirmed' if crossed else 'not_touched')


def self_check():
    normal = NormalDist()
    require(abs(2*(1-normal.cdf(normal.inv_cdf(.65)))-.70) < 1e-12, 'First-passage math check failed')
    require(abs(2*(1-normal.cdf(.38))-.7039454151516744) < 1e-12, 'Rounded multiplier check failed')
    rows = [dict(timestamp=stamp('2026-09-28','09:30:00'),open=103.,high=104.,low=102.,close=103.)]
    gap = score(rows,101.,'R1')
    require(gap['opened_beyond'] and gap['threshold_reached'] and not gap['touched'], 'Opening gap falsely counted as touch')
    require(score(rows,102.,'S1')['touched'], 'Exact low touch missed')
    require(not score(rows,105.,'R1')['touched'], 'Non-touch falsely counted')
    require(score(rows,103.,'P')['touched'], 'Pivot touch missed')
    a = cash_variance(rows)
    rows[0]['open'] = 1030.
    require(cash_variance(rows) > a, 'First cash bar return omitted')
    require(cents(1.005) == 1.01, 'Cent rounding check failed')
    require(levels(100.,10.,{'R1':.38,'R2':.67,'R3':1.,'S1':.38,'S2':.67,'S3':1.})['R1'] == 103.8, 'Level construction failed')


def load_inputs(contract):
    import polars as pl
    manifest = {}
    def bind(path, expected=None):
        actual = digest(path)
        require(expected is None or actual == expected, 'Source hash changed: '+str(path))
        manifest[path.relative_to(PROJECT).as_posix()] = actual
    base_path = OUTPUTS/'composite_pivots/report.json'
    base = read(base_path)
    bind(base_path)
    roster_path = OUTPUTS/'qqq_range/sessions.json'
    bind(roster_path,base['session_roster_sha256'])
    roster = sorted(read(roster_path), key=lambda r:r['date'])
    session = {r['date']:r for r in roster}
    dates = contract['sessions']
    require(len(dates) == 5 and dates == sorted(set(dates)), 'Expected five distinct ordered sessions')
    require(contract['forecast_time'] < '09:30:00', 'Forecast must be before cash open')
    prior = {day:[r['date'] for r in roster if r['date'] < day][-contract['lookback_sessions']:] for day in dates}
    require(all(len(days) == contract['lookback_sessions'] for days in prior.values()), 'Insufficient prior history')
    needed = sorted({day for days in prior.values() for day in days})
    source_hashes = {row['file']:row['sha256'] for row in base['source_manifest']}
    paths = {}
    for month in sorted({d[:7].replace('-','_') for d in needed+dates}):
        path = OUTPUTS/'qqq_range/data'/('prices_'+month+'.parquet')
        bind(path,source_hashes[path.relative_to(OUTPUTS).as_posix()])
        paths[month] = path
    def cash(day):
        opening,closing = [stamp(day,session[day][k]) for k in ('open','close')]
        rows = pl.read_parquet(paths[day[:7].replace('-','_')]).filter(
            (pl.col('timestamp') >= opening)&(pl.col('timestamp') < closing)).sort('timestamp').to_dicts()
        helpers.validate_cash(rows,opening,closing)
        return rows
    # Load only causal predictor sessions here; target outcomes scored after forecast freeze.
    history = {day:cash(day) for day in needed}
    collection_path = OUTPUTS/'composite_pivots_context_v3/cash_option_collection.json'
    bind(collection_path)
    requests = {row['parameters']['date']:row for row in read(collection_path)['requests']}
    next_session = {a['date']:b['date'] for a,b in zip(roster,roster[1:])}
    options = {}
    for day in needed:
        request = requests[day]
        require(request['target_date'] == next_session[day], 'Wrong option expiration request')
        path = PROJECT/request['file']
        bind(path,request['sha256'])
        quotes = pl.read_parquet(path).to_dicts()
        opening,closing = [stamp(day,session[day][k]) for k in ('open','close')]
        snapshot = closing-dt.timedelta(minutes=1)
        reference = history[day][-1]['close']
        eligible = {}
        for q in quotes:
            if q['timestamp'] != snapshot or not opening <= q['underlying_timestamp'] <= snapshot:
                continue
            nums = [q[key] for key in ('bid','ask','implied_vol','delta','iv_error','underlying_price','strike')]
            if not all(isinstance(v,(float,int)) and math.isfinite(v) for v in nums):
                continue
            if not (q['symbol'] == 'QQQ' and q['expiration'] == next_session[day] and q['right'] in ('CALL','PUT') and
                    0 < q['bid'] <= q['ask'] and 0 < q['implied_vol'] < 5 and .10 <= abs(q['delta']) <= .90 and
                    abs(q['iv_error']) <= .01 and (q['ask']-q['bid'])/((q['ask']+q['bid'])/2) <= .20 and
                    abs(q['underlying_price']/reference-1) <= .01):
                continue
            key = (q['strike'],q['right'])
            require(key not in eligible, 'Duplicate qualifying option contract')
            eligible[key] = q
        strikes = {k[0] for k in eligible if (k[0],'CALL') in eligible and (k[0],'PUT') in eligible}
        require(bool(strikes), 'No qualified ATM pair on '+day)
        strike = min(strikes,key=lambda k:(abs(k-reference),k))
        pair = [eligible[(strike,right)] for right in ('CALL','PUT')]
        options[day] = dict(annual_iv_decimal=mean(q['implied_vol'] for q in pair),
                           contracts=[{key:(q[key].isoformat() if isinstance(q[key],dt.datetime) else q[key]) for key in
                                       ('symbol','expiration','strike','right','timestamp','underlying_timestamp','bid','ask','delta','implied_vol','iv_error','underlying_price')} for q in pair],
                           raw_rows=len(quotes),eligible_rows=len(eligible),source_file=request['file'],source_sha256=request['sha256'])
    return dict(prior=prior,history=history,options=options,session=session,cash=cash,manifest=manifest,bind=bind)


def predict(day, contract, data):
    days = data['prior'][day]
    require(all(d < day for d in days), 'Future session used in forecast')
    cutoff = stamp(day,contract['forecast_time'])
    require(all(stamp(d,'17:15:00') < cutoff for d in days), 'Prior input not yet available')
    reference = data['history'][days[-1]][-1]['close']
    opening,closing = [stamp(day,data['session'][day][k]) for k in ('open','close')]
    minutes = int((closing-opening).total_seconds()/60)
    require(cutoff < opening and minutes > 0, 'Wrong forecast/session timing')
    variances = [cash_variance(data['history'][d]) for d in days]
    variance = mean(variances)*minutes/390
    annual_iv = [data['options'][d]['annual_iv_decimal'] for d in days]
    history_sigma = reference*math.sqrt(variance)
    iv_ratio = annual_iv[-1]/mean(annual_iv)
    return dict(target_date=day,as_of=cutoff.isoformat(),anchor_date=days[-1],reference_price=reference,
                input_dates=days,cash_minutes=minutes,prior_cash_variances_390=variances,prior_atm_ivs=annual_iv,
                history_cash_sigma_dollars=history_sigma,iv_ratio=iv_ratio,candidate_cash_sigma_dollars=history_sigma*iv_ratio,
                levels=levels(reference,history_sigma*iv_ratio,contract['multipliers']),
                benchmark_levels=levels(reference,history_sigma,contract['multipliers']))


def plot_day(ax, forecast, rows, events):
    import matplotlib.dates as mdates
    times = [r['timestamp'] for r in rows]
    ax.fill_between(times,[r['low'] for r in rows],[r['high'] for r in rows],color='#b8c6da',alpha=.55)
    ax.plot(times,[r['close'] for r in rows],color='#263e67',linewidth=.8,label='QQQ cash minutes')
    ax.scatter(times[0],rows[0]['open'],s=18,color='#d48806',zorder=6)
    for name in NAMES:
        price = forecast['levels'][name]
        hit = events[name]['touched']
        color = '#087f5b' if name.startswith('S') else '#c03944' if name.startswith('R') else '#7655a2'
        ax.axhline(price,color=color,linewidth=1.,linestyle='-' if hit else '--',alpha=.85)
        theoretical = '' if name == 'P' else {'1':'~70%','2':'~50%','3':'~32%'}[name[-1]]
        ax.text(1.008,price,f'{name}  {price:.2f}  {theoretical}  {"touched" if hit else "miss"}',
                transform=ax.get_yaxis_transform(),fontsize=8,color=color,va='center')
        if hit:
            when = dt.datetime.fromisoformat(events[name]['first_touch_minute'])
            ax.scatter(when,price,s=12,color=color,zorder=6)
    ax.set_xlim(stamp(forecast['target_date'],'09:30:00'),stamp(forecast['target_date'],'16:00:00'))
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[10,12,14,16],tz=NY))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M',tz=NY))
    ax.set_title(f"{forecast['target_date']} | fixed 09:25 ET | prior close {forecast['reference_price']:.2f}",loc='left',fontsize=11)
    ax.set_ylabel('QQQ ($)')
    ax.grid(alpha=.15)
    ax.tick_params(labelsize=8)


def artifacts(forecasts, cash, daily, report):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes = plt.subplots(5,1,figsize=(12,15))
    sections = []
    for ax,f,result in zip(axes,forecasts,daily):
        day = f['target_date']
        plot_day(ax,f,cash[day],result['candidate'])
        small,sax = plt.subplots(figsize=(12,4))
        plot_day(sax,f,cash[day],result['candidate'])
        small.subplots_adjust(left=.07,right=.72,bottom=.17,top=.87)
        small.savefig(HERE/(day+'.png'),dpi=160)
        plt.close(small)
        lines = ''.join(f'<tr><td>{name}</td><td>{f["levels"][name]:.2f}</td><td>{html.escape(result["candidate"][name]["status"])}</td><td>{(result["candidate"][name]["first_touch_minute"] or "")[11:16]}</td><td>{"Yes" if result["candidate"][name]["opened_beyond"] else "No"}</td></tr>' for name in reversed(NAMES))
        sections.append(f'<article id="{day}"><h2>{day}</h2><img src="{day}.png" alt="QQQ cash price path and fixed preopen target lines for {day}"><p>Cash open {result["cash_open"]:.2f}; high {result["cash_high"]:.2f}; low {result["cash_low"]:.2f}; close {result["cash_close"]:.2f}. Opening gap from reference: {result["opening_gap"]:+.2f}.</p><table><thead><tr><th>Line</th><th>Price</th><th>Cash touch result</th><th>First minute ET</th><th>Opened beyond</th></tr></thead><tbody>{lines}</tbody></table></article>')
    fig.subplots_adjust(left=.08,right=.73,top=.94,bottom=.04,hspace=.48)
    fig.suptitle('QQQ: prior-cash-only touch projections | September 28–October 2, 2026',fontsize=14,y=.98)
    fig.savefig(HERE/'five_days.png',dpi=160)
    plt.close(fig)
    summaries = ''.join(f'<tr><td>{name}</td><td>{report["candidate_touch_counts"][name]}/5</td><td>{report["history_only_touch_counts"][name]}/5</td></tr>' for name in NAMES)
    document = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>QQQ preopen cash touch test</title><style>body{font:16px system-ui;margin:24px auto;max-width:1200px;padding:0 16px;color:#203047}table{border-collapse:collapse;margin:16px 0}th,td{text-align:left;padding:8px 20px 8px 0;border-bottom:1px solid #ccd4de}img{max-width:100%;height:auto}article{margin:40px 0}nav a{margin-right:16px}</style><h1>Five cash-session touch projections</h1><p>QQQ, fixed 09:25 Eastern. Inputs: only completed prior cash sessions. Solid lines were touched; dashed lines were missed. Orange dot = cash open. First-touch times identify minute bars, not exact executions.</p><p>The ~70%, ~50%, ~32% labels are Brownian-model assumptions, not demonstrated market probabilities. Opening gaps change the starting point. This week was previously used in development, and these historical forecasts are reconstructed.</p><nav>' + ''.join(f'<a href="#{f["target_date"]}">{f["target_date"]}</a>' for f in forecasts) + '</nav><h2>Touch scoreboard</h2><table><thead><tr><th>Line</th><th>IV + history</th><th>History only</th></tr></thead><tbody>'+summaries+'</tbody></table>' + ''.join(sections) + '<p>Rules and evidence: <a href="contract.json">contract</a>, <a href="forecasts.json">frozen projections</a>, <a href="report.json">full results</a>, <a href="daily.csv">daily CSV</a>. <a href="'+report['math_source']+'">MIT reflection principle</a>.</p></html>'
    (HERE/'view.html').write_text(document,encoding='utf-8')


def main():
    self_check()
    contract = read(HERE/'contract.json')
    data = load_inputs(contract)
    original_records = read(OUTPUTS/'composite_pivots_context_v3/original_hashes.json')
    originals = {str(PROJECT/row['file']):row['sha256'] for row in original_records}
    require(all(digest(Path(path)) == h for path,h in originals.items()), 'Original inventory hash mismatch')
    forecasts = [predict(day,contract,data) for day in contract['sessions']]
    write(HERE/'forecasts.json',forecasts)
    write(HERE/'inputs.json',dict(options=data['options'],source_manifest=[dict(file=k,sha256=v) for k,v in sorted(data['manifest'].items())]))
    frozen = dict(created_utc=dt.datetime.now(dt.timezone.utc).isoformat(),contract_sha256=digest(HERE/'contract.json'),
                  code_sha256=digest(Path(__file__)),forecasts_sha256=digest(HERE/'forecasts.json'),inputs_sha256=digest(HERE/'inputs.json'),
                  outcome_scoring_started=False,parameter_searches=0)
    write(HERE/'candidate_freeze.json',frozen)
    # Outcomes enter only after prediction/code/input hashes are saved.
    cash = {day:data['cash'](day) for day in contract['sessions']}
    daily = []
    for f in forecasts:
        rows = cash[f['target_date']]
        require(len(rows) == 390, 'Test session not complete/full length')
        result = dict(date=f['target_date'],as_of=f['as_of'],reference=f['reference_price'],cash_open=rows[0]['open'],
                      cash_high=max(r['high'] for r in rows),cash_low=min(r['low'] for r in rows),cash_close=rows[-1]['close'],
                      opening_gap=rows[0]['open']-f['reference_price'],candidate_sigma=f['candidate_cash_sigma_dollars'],
                      history_sigma=f['history_cash_sigma_dollars'],iv_ratio=f['iv_ratio'],
                      candidate={name:score(rows,f['levels'][name],name) for name in NAMES},
                      history_only={name:score(rows,f['benchmark_levels'][name],name) for name in NAMES})
        daily.append(result)
    for name in ('contract','forecasts','inputs'):
        require(digest(HERE/(name+'.json')) == frozen[name+'_sha256'], 'Frozen artifact changed')
    require(digest(Path(__file__)) == frozen['code_sha256'], 'Frozen code changed')
    require(all(digest(Path(path)) == h for path,h in originals.items()), 'Original artifact modified')
    for path,hash_value in data['manifest'].items():
        require(digest(PROJECT/path) == hash_value, 'Input changed during evaluation')
    report = dict(version=contract['version'],symbol='QQQ',sessions=contract['sessions'],cash_minutes=sum(len(r) for r in cash.values()),
                  target_cutoff='09:25 America/New_York',candidate_freeze=frozen,daily=daily,
                  candidate_touch_counts={name:sum(d['candidate'][name]['touched'] for d in daily) for name in NAMES},
                  history_only_touch_counts={name:sum(d['history_only'][name]['touched'] for d in daily) for name in NAMES},
                  candidate_either_R1_S1=sum(d['candidate']['R1']['touched'] or d['candidate']['S1']['touched'] for d in daily),
                  candidate_both_R1_S1=sum(d['candidate']['R1']['touched'] and d['candidate']['S1']['touched'] for d in daily),
                  mathematical_probabilities={str(z):2*(1-NormalDist().cdf(z)) for z in (.38,.67,1.)},
                  qualified_inputs='prior cash OHLC and final-cash-minute near-ATM options IV, bid/ask and delta quality filters',
                  checks=dict(self_checks='passed',source_hashes='passed',cash_coverage='passed',prior_only_cutoffs='passed',
                              freeze_unchanged='passed',original_artifacts_unchanged='passed',probability_calibration='not_run',out_of_sample='not_run',trading_profitability='not_applicable'),
                  original_files_preserved=len(originals),source_files_bound=len(data['manifest']),
                  limitations=[contract['holdout_state'],contract['data_vintage'],contract['sigma_interpretation'],
                               contract['probability_definition'],contract['primary_outcome'],'No reversal, containment or profitability conclusion.'],
                  math_source=contract['math_source'])
    write(HERE/'report.json',report)
    with (HERE/'daily.csv').open('w',newline='',encoding='utf-8') as stream:
        fields = ['date','as_of','reference','candidate_sigma','iv_ratio','cash_open','cash_high','cash_low','cash_close','opening_gap']
        fields += [name+suffix for name in NAMES for suffix in ('_price','_touched','_first_minute','_opened_beyond','_history_price','_history_touched')]
        writer = csv.DictWriter(stream,fieldnames=fields)
        writer.writeheader()
        for f,d in zip(forecasts,daily):
            row = {k:d[k] for k in fields if k in d}
            for name in NAMES:
                row.update({name+'_price':f['levels'][name],name+'_touched':d['candidate'][name]['touched'],
                            name+'_first_minute':d['candidate'][name]['first_touch_minute'],name+'_opened_beyond':d['candidate'][name]['opened_beyond'],
                            name+'_history_price':f['benchmark_levels'][name],name+'_history_touched':d['history_only'][name]['touched']})
            writer.writerow(row)
    artifacts(forecasts,cash,daily,report)
    print(json.dumps({k:report[k] for k in ('cash_minutes','candidate_touch_counts','history_only_touch_counts','checks')}))


if __name__ == '__main__':
    main()
