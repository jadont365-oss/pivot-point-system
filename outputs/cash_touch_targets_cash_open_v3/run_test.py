"""P = observed cash open; immutable prior-session offsets; no later input."""
import csv
import datetime as dt
from decimal import Decimal
import importlib.util
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTPUTS = HERE.parent
PROJECT = OUTPUTS.parent
V1 = OUTPUTS/'cash_touch_targets_v1'
V2 = OUTPUTS/'cash_touch_targets_composite_p_v2'
spec = importlib.util.spec_from_file_location('prior_p_translation',V2/'run_test.py')
previous = importlib.util.module_from_spec(spec)
spec.loader.exec_module(previous)
touch = previous.touch
read,write,digest,require,stamp = touch.read,touch.write,touch.digest,touch.require,touch.stamp
TARGETS = tuple(name for name in touch.NAMES if name != 'P')


def project(prior, opening_price, opening_at):
    """The only target-session argument is its observed opening price/time."""
    day = prior['target_date']
    require(isinstance(opening_price,(float,int)) and math.isfinite(opening_price) and opening_price > 0,'Invalid cash open')
    start = stamp(day,'09:30:00')
    require(isinstance(opening_at,dt.datetime) and opening_at.tzinfo is not None and
            start <= opening_at < start+dt.timedelta(minutes=1),'Expected opening observation in the 09:30 cash minute')
    require(dt.datetime.fromisoformat(prior['as_of']) < opening_at,'Distances must precede cash open')
    require(all(date < day for date in prior['input_dates']),'Current/future session used for distances')
    normalized = float(Decimal(str(opening_price)).quantize(Decimal('.00000001')))
    result = dict(prior)
    result.update(version='cash_touch_targets_cash_open_v3',as_of=opening_at.isoformat(),
                  prior_distances_as_of=prior['as_of'],P=normalized,observed_cash_open=opening_price,
                  reference_price=normalized,scale_reference_price=prior['reference_price'],
                  levels=previous.translated_levels(prior['levels'],normalized),baseline_levels=prior['levels'],
                  current_input='opening_price_only',projection_kind='retrospective_first_cash_open_proxy')
    require(abs(result['levels']['P']-opening_price) <= 1e-8,'P differs from opening price')
    return result


def self_check(prior):
    at = stamp(prior['target_date'],'09:30:00')
    opened = project(prior,100.005,at)
    require(opened['levels']['P'] == 100.005,'Sub-cent opening price lost')
    require(project(prior,100.005,at+dt.timedelta(milliseconds=500))['P'] == 100.005,'Subsecond opening event rejected')
    for bad_price,bad_time in ((float('nan'),at),(0.,at),(100.,at-dt.timedelta(minutes=1)),(100.,at+dt.timedelta(minutes=1))):
        try:
            project(prior,bad_price,bad_time)
        except ValueError:
            pass
        else:
            raise AssertionError('Invalid opening input admitted')
    bad = dict(prior,input_dates=prior['input_dates']+[prior['target_date']])
    try:
        project(bad,100.,at)
    except ValueError:
        pass
    else:
        raise AssertionError('Target-session distance input admitted')


def render(forecasts,cash,daily):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.patches import Rectangle
    fig,axes = plt.subplots(5,1,figsize=(12,15))
    for ax,f,d in zip(axes,forecasts,daily):
        touch.plot_day(ax,f,cash[f['target_date']],d['cash_open_anchored'])
        for label in ax.texts:
            name = label.get_text().split()[0]
            label.set_text(f'{name}  {f["levels"][name]:.3f}  {"open" if name == "P" else "touched" if d["cash_open_anchored"][name]["touched"] else "miss"}')
        ax.set_title(f'{f["target_date"]} | fixed at cash open 09:30 ET | P = opening price {f["P"]:.3f}',loc='left',fontsize=11)
    fig.suptitle('QQQ: cash-open P | unchanged prior-cash volatility/IV distances',fontsize=14,y=.98)
    fig.subplots_adjust(left=.08,right=.73,top=.94,bottom=.04,hspace=.48)
    fig.savefig(HERE/'five_days.png',dpi=150)
    plt.close(fig)
    f = next(f for f in forecasts if f['target_date'] == '2026-10-01')
    rows = cash[f['target_date']]
    fig,ax = plt.subplots(figsize=(14,8))
    bg,text = '#111a27','#d9e2ef'
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)
    ax.tick_params(colors=text)
    ax.grid(color='#29374a',alpha=.55,linewidth=.6)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_color('#29374a')
    width = 3.4/(24*60)
    for i in range(0,len(rows),5):
        group = rows[i:i+5]
        o,c = group[0]['open'],group[-1]['close']
        x = mdates.date2num(group[0]['timestamp']+dt.timedelta(minutes=2.5))
        color = '#35c795' if c >= o else '#ed637b'
        ax.vlines(x,min(r['low'] for r in group),max(r['high'] for r in group),color=color,linewidth=1.05)
        ax.add_patch(Rectangle((x-width/2,min(o,c)),width,max(abs(c-o),.012),color=color,zorder=3))
    for name in reversed(touch.NAMES):
        price = f['levels'][name]
        color = '#48d5ac' if name.startswith('S') else '#ff8090' if name.startswith('R') else '#dfbd71'
        ax.axhline(price,color=color,linewidth=1.05,alpha=.85)
        ax.text(1.008,price,f'{name}  ${price:.2f}'+('  CASH OPEN' if name == 'P' else ''),
                transform=ax.get_yaxis_transform(),color=color,va='center',fontsize=11)
    ax.scatter(stamp(f['target_date'],'09:30:00'),f['P'],color='#dfbd71',s=38,zorder=5)
    ax.set_xlim(stamp(f['target_date'],'09:30:00'),stamp(f['target_date'],'16:00:00'))
    ax.xaxis.set_major_locator(mdates.MinuteLocator(byminute=[0,30],tz=touch.NY))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M',tz=touch.NY))
    ax.set_ylabel('QQQ price ($)',color=text)
    ax.set_xlabel('Cash session · Eastern Time',color=text,labelpad=12)
    fig.text(.07,.94,'QQQ | October 1, 2026 | P = cash-session open',color=text,fontsize=20,weight='bold')
    fig.text(.07,.892,'5-minute candles · Lines fixed at the 9:30 opening price · Distances from prior cash-session volatility and IV',color='#93a7c0',fontsize=10)
    fig.text(.07,.03,'Historical reconstruction. Gold dot marks the cash open at P. R/S spacing is unchanged from the prior-close test.',color='#93a7c0',fontsize=10)
    fig.subplots_adjust(left=.07,right=.77,top=.85,bottom=.13)
    fig.savefig(HERE/'2026-10-01_candles.png',dpi=180,facecolor=bg)
    plt.close(fig)


def main():
    import polars as pl
    contract = read(HERE/'contract.json')
    baseline = read(V1/'forecasts.json')
    require([f['target_date'] for f in baseline] == contract['sessions'],'Wrong five-day baseline')
    self_check(baseline[0])
    touch.self_check()
    old_freeze = read(V1/'candidate_freeze.json')
    manifest = read(V1/'inputs.json')['source_manifest']
    for name in ('forecasts','inputs','contract'):
        require(digest(V1/(name+'.json')) == old_freeze[name+'_sha256'],'Original candidate changed')
    require(digest(V1/'run_test.py') == old_freeze['code_sha256'],'Original helper changed')
    require(digest(V2/'run_test.py') == read(V2/'candidate_freeze.json')['hashes']['run_test.py'],'Translation helper changed')
    for item in manifest:
        require(digest(PROJECT/item['file']) == item['sha256'],'Bound source changed')
    preserved = {str(path):digest(path) for folder in (V1,V2) for path in folder.iterdir() if path.is_file()}
    distances = [dict(date=f['target_date'],prepared_as_of=f['as_of'],prior_input_dates=f['input_dates'],
                      sigma=f['candidate_cash_sigma_dollars'],offsets={name:float(Decimal(str(value))-Decimal(str(f['levels']['P']))) for name,value in f['levels'].items()}) for f in baseline]
    write(HERE/'prepared_distances.json',distances)
    preparation_freeze = dict(created_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                              files={name:digest(HERE/name) for name in ('contract.json','run_test.py','prepared_distances.json')},
                              prior_forecasts_sha256=digest(V1/'forecasts.json'),sources=manifest)
    write(HERE/'distances_freeze.json',preparation_freeze)
    # Admit opening price only; no current-day H/L/C/volume enter project().
    forecasts = []
    for prior in baseline:
        day = prior['target_date']
        path = OUTPUTS/'qqq_range/data'/('prices_'+day[:7].replace('-','_')+'.parquet')
        opening = pl.read_parquet(path).filter(pl.col('timestamp') == stamp(day,'09:30:00')).select('timestamp','open').to_dicts()
        require(len(opening) == 1,'Missing/duplicate cash opening observation')
        f = project(prior,opening[0]['open'],opening[0]['timestamp'])
        f['opening_source'] = dict(file=path.relative_to(PROJECT).as_posix(),timestamp_kind='one_minute_bucket_label',event_age='unknown')
        forecasts.append(f)
    write(HERE/'forecasts.json',forecasts)
    frozen = dict(created_utc=dt.datetime.now(dt.timezone.utc).isoformat(),forecasts_sha256=digest(HERE/'forecasts.json'),
                  distances_freeze_sha256=digest(HERE/'distances_freeze.json'),state='after opening-price admission, before cash high/low scoring')
    write(HERE/'candidate_freeze.json',frozen)
    cash,daily = {},[]
    for f in forecasts:
        day = f['target_date']
        path = OUTPUTS/'qqq_range/data'/('prices_'+day[:7].replace('-','_')+'.parquet')
        rows = pl.read_parquet(path).filter((pl.col('timestamp') >= stamp(day,'09:30:00'))&(pl.col('timestamp') < stamp(day,'16:00:00'))).sort('timestamp').to_dicts()
        touch.helpers.validate_cash(rows,stamp(day,'09:30:00'),stamp(day,'16:00:00'))
        require(len(rows) == 390 and abs(rows[0]['open']-f['P']) <= 1e-8,'Cash coverage/anchor mismatch')
        cash[day] = rows
        events = {name:touch.score(rows,f['levels'][name],name) for name in touch.NAMES}
        require(not any(events[name]['opened_beyond'] for name in TARGETS),'Opening already beyond translated R/S line')
        daily.append(dict(date=day,P=f['P'],cash_open=rows[0]['open'],prior_P=f['baseline_levels']['P'],
                          cash_high=max(r['high'] for r in rows),cash_low=min(r['low'] for r in rows),
                          cash_open_anchored=events,baseline={name:touch.score(rows,f['baseline_levels'][name],name) for name in touch.NAMES}))
    require(digest(HERE/'forecasts.json') == frozen['forecasts_sha256'],'Frozen predictions changed')
    require(all(digest(HERE/name) == expected for name,expected in preparation_freeze['files'].items()),'Prepared rule/distance changed')
    require(all(digest(Path(path)) == expected for path,expected in preserved.items()),'Original file modified')
    report = dict(version=contract['version'],cash_minutes=sum(len(r) for r in cash.values()),sessions=contract['sessions'],daily=daily,
                  target_touch_counts={variant:{name:sum(d[variant][name]['touched'] for d in daily) for name in TARGETS} for variant in ('cash_open_anchored','baseline')},
                  any_R_or_S_days={variant:sum(any(d[variant][name]['touched'] for name in TARGETS) for d in daily) for variant in ('cash_open_anchored','baseline')},
                  either_R1_S1_days=sum(d['cash_open_anchored']['R1']['touched'] or d['cash_open_anchored']['S1']['touched'] for d in daily),
                  both_R1_S1_days=sum(d['cash_open_anchored']['R1']['touched'] and d['cash_open_anchored']['S1']['touched'] for d in daily),
                  P_is_automatic_anchor_not_prediction=True,calibrated_probabilities=False,
                  checks=dict(P_equals_cash_open='passed',prior_only_distances='passed',unchanged_R_S_offsets='passed',invalid_open_rejected='passed',
                              premarket_rejected='passed',target_session_distance_input_rejected='passed',complete_cash_coverage='passed',originals_preserved='passed',
                              probability_calibration='not_run',out_of_sample='not_run',live_open_receipt_timing='unverified'),
                  preservation_files=len(preserved),financial_validation='Consumed five-day historical diagnostic, not live proof or calibrated odds.')
    render(forecasts,cash,daily)
    with (HERE/'daily.csv').open('w',newline='',encoding='utf-8') as stream:
        fields = ['date','P','prior_P']+[name+suffix for name in TARGETS for suffix in ('_price','_touch','_first_minute')]
        writer = csv.DictWriter(stream,fieldnames=fields)
        writer.writeheader()
        for f,d in zip(forecasts,daily):
            row = {key:d[key] for key in ('date','P','prior_P')}
            for name in TARGETS:
                row.update({name+'_price':f['levels'][name],name+'_touch':d['cash_open_anchored'][name]['touched'],name+'_first_minute':d['cash_open_anchored'][name]['first_touch_minute']})
            writer.writerow(row)
    write(HERE/'report.json',report)
    print(json.dumps({key:report[key] for key in ('cash_minutes','target_touch_counts','either_R1_S1_days','both_R1_S1_days','checks')}))


if __name__ == '__main__':
    main()
