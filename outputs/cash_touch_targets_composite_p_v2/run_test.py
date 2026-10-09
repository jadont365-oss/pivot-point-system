"""Reuse the frozen full composite P; change only the center of the touch set."""
import csv
import datetime as dt
from decimal import Decimal
import importlib.util
import json
import math
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
OUTPUTS = HERE.parent
PROJECT = OUTPUTS.parent
V1 = OUTPUTS/'cash_touch_targets_v1'
CONTEXT = OUTPUTS/'composite_pivots_context_v3'


def module(name,path):
    spec = importlib.util.spec_from_file_location(name,path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


touch = module('original_touch_model',V1/'run_test.py')
context = module('existing_composite_context',CONTEXT/'context.py')
rebuild = module('existing_cash_rebuild',CONTEXT/'cash_rebuild.py')
engine = touch.helpers.engine
read,write,digest,require = touch.read,touch.write,touch.digest,touch.require


def translated_levels(old,new_p):
    shift = Decimal(str(new_p))-Decimal(str(old['P']))
    result = {name:float(Decimal(str(value))+shift) for name,value in old.items()}
    require(result['P'] == new_p and result['S3'] > 0, 'Invalid translated center')
    require(all(result[a] < result[b] for a,b in zip(touch.NAMES,touch.NAMES[1:])), 'Translated ordering changed')
    for name in touch.NAMES:
        require(Decimal(str(result[name]))-Decimal(str(new_p)) == Decimal(str(old[name]))-Decimal(str(old['P'])), 'Spacing changed')
    return result


def load_composite(contract):
    import polars as pl
    source = read(CONTEXT/'candidate_freeze.json')
    source_manifest = {}
    def bind(path,expected=None):
        actual = digest(path)
        require(expected is None or actual == expected,'Source changed: '+str(path))
        source_manifest[path.relative_to(PROJECT).as_posix()] = actual
    # Existing model, source data and code retain their original freeze.
    for item in source['code_manifest']+source['source_manifest']:
        bind(PROJECT/item['file'],item['sha256'])
    for name,expected in source['frozen_artifacts'].items():
        bind(CONTEXT/name,expected)
    bind(CONTEXT/'candidate_freeze.json')
    bind(V1/'candidate_freeze.json')
    old_freeze = read(V1/'candidate_freeze.json')
    for name in ('forecasts','inputs','contract'):
        bind(V1/(name+'.json'),old_freeze[name+'_sha256'])
    bind(V1/'run_test.py',old_freeze['code_sha256'])
    for item in read(V1/'inputs.json')['source_manifest']:
        bind(PROJECT/item['file'],item['sha256'])
    fitted = read(CONTEXT/'model.json')
    require(fitted['fit_end'] == '2026-09-25' and fitted['observation_count'] == 223 and fitted['alpha'] == 10, 'Unexpected frozen model')
    require(all(day < min(contract['sessions']) for day in fitted['training_dates']), 'Test dates used in fit')
    sources = {row['target_date']:row for row in read(CONTEXT/'forecasts.json')}
    originals = read(CONTEXT/'original_hashes.json')
    for item in originals:
        bind(PROJECT/item['file'],item['sha256'])
    old = read(V1/'forecasts.json')
    require([r['target_date'] for r in old] == contract['sessions'], 'Matched five-day baseline missing')
    candidates,records = [],[]
    for baseline in old:
        day = baseline['target_date']
        existing = sources[day]
        base = existing['base']
        options = base['inputs']['options']
        option_file = next(p for p in options['source_hashes'] if 'greeks_' in p)
        oi_file = next(p for p in options['source_hashes'] if 'open_interest_' in p)
        quotes = pl.read_parquet(PROJECT/option_file).to_dicts()
        interests = pl.read_parquet(PROJECT/oi_file).to_dicts()
        history = [read(OUTPUTS/'composite_pivots/features'/(d+'.json')) for d in base['inputs']['hv_close_dates']]
        require(all(r['date'] < day for r in history), 'Target/future feature input')
        cash_base = rebuild.rebuild(base,quotes,interests,history[-1]['session_calendar'],'16:00:00',engine)
        cash_base['inputs']['options'].update(source_hashes=options['source_hashes'],source_parameters=options['source_parameters'])
        assembled = engine.assembled_inputs(history,cash_base['inputs']['options'])
        require(assembled == cash_base['inputs'], 'Prior profile/volatility/ADX/RVOL rebuild mismatch')
        recomputed = engine.calculate(assembled)
        require(recomputed['levels'] == cash_base['levels'] and cash_base == base, 'Raw cash option/base reproduction failed')
        predicted = context.predict(cash_base,fitted)
        require(predicted['center'] == existing['center'] and predicted['levels'] == existing['levels'] and predicted['features'] == existing['features'], 'Frozen composite center reproduction failed')
        new_p = touch.cents(predicted['center'])
        candidate = dict(baseline)
        candidate.update(version=contract['version'],levels=translated_levels(baseline['levels'],new_p),
                         baseline_levels=baseline['levels'],pivot_price=new_p,
                         pivot_calculation=dict(base_P=cash_base['levels']['P'],base_P_unrounded=cash_base['pivot_unrounded'],
                           base_contributions=cash_base['pivot_contributions'],context_center_offset=predicted['center']-cash_base['levels']['P'],
                           unrounded_P=predicted['center'],center_raw_scales=predicted['center_raw_scales'],center_capped_scales=predicted['center_capped_scales'],
                           inherited_adjusted_scale=cash_base['adjusted_scale'],features=predicted['features'],feature_contributions=predicted['feature_contributions'],
                           model_fit_end=fitted['fit_end'],source_as_of=base['as_of'],model_target='next cash VWAP center',
                           source_forecast='outputs/composite_pivots_context_v3/forecasts.json',gamma_source=options['gamma_source']))
        require(candidate['as_of'] == engine.stamp(day,'09:25:00').isoformat(), 'Wrong new preopen cutoff')
        candidates.append(candidate)
        records.append(dict(date=day,profile_dates=assembled['profile_dates'],hv_close_dates=assembled['hv_close_dates'],
                            rvol_reference_dates=assembled['rvol_reference_dates'],option_source_hashes=options['source_hashes'],
                            quote_window=options['quote_window'],oi_cutoff=options['oi_cutoff'],
                            input_timing=base['input_timing'],eligible_contracts=len(options['eligible_contracts'])))
    return candidates,dict(records=records,model_file='outputs/composite_pivots_context_v3/model.json',
                           source_manifest=[dict(file=k,sha256=v) for k,v in sorted(source_manifest.items())])


def render(forecasts,cash,daily):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes = plt.subplots(5,1,figsize=(12,15))
    for ax,f,result in zip(axes,forecasts,daily):
        touch.plot_day(ax,f,cash[f['target_date']],result['composite'])
        for label in ax.texts:
            name = label.get_text().split()[0]
            label.set_text(f'{name}  {f["levels"][name]:.2f}  {"touched" if result["composite"][name]["touched"] else "miss"}')
        ax.axhline(f['baseline_levels']['P'],color='#666666',linestyle=':',linewidth=.9)
        ax.set_title(f'{f["target_date"]} | fixed 09:25 ET | composite P {f["pivot_price"]:.2f} | prior-close P {f["reference_price"]:.2f} (dotted)',loc='left',fontsize=10)
    fig.suptitle('QQQ: composite P with unchanged touch-line spacing',fontsize=14,y=.98)
    fig.subplots_adjust(left=.08,right=.74,top=.94,bottom=.04,hspace=.48)
    fig.savefig(HERE/'comparison.png',dpi=150)
    plt.close(fig)


def main():
    touch.self_check()
    sample = {'S3':90.,'S2':93.3,'S1':96.2,'P':100.,'R1':103.8,'R2':106.7,'R3':110.}
    require(translated_levels(sample,101.23)['S1'] == 97.43,'Cent translation check failed')
    contract = read(HERE/'contract.json')
    preserved = {str(path):digest(path) for folder in (V1,CONTEXT) for path in folder.iterdir() if path.is_file()}
    forecasts,inputs = load_composite(contract)
    write(HERE/'forecasts.json',forecasts)
    write(HERE/'inputs.json',inputs)
    frozen = dict(created_utc=dt.datetime.now(dt.timezone.utc).isoformat(),parameter_searches=0,
                  hashes={name:digest(HERE/name) for name in ('contract.json','run_test.py','forecasts.json','inputs.json')},
                  state='new predictions saved before cash touch scoring; week already consumed')
    write(HERE/'candidate_freeze.json',frozen)
    # Cash outcomes are read only after the new candidate is saved.
    cash_data = touch.load_inputs(read(V1/'contract.json'))
    cash = {day:cash_data['cash'](day) for day in contract['sessions']}
    parent = read(OUTPUTS/'composite_pivots/report.json')
    feature_hashes = {r['file']:r['sha256'] for r in parent['feature_manifest']}
    outcomes,daily = [],[]
    for f in forecasts:
        day = f['target_date']
        path = OUTPUTS/'composite_pivots/features'/(day+'.json')
        expected = feature_hashes['features/'+day+'.json']
        require(digest(path) == expected, 'Outcome feature changed')
        value = read(path)
        require(not value['invalid_reasons'] and value['cash_low'] <= value['vwap'] <= value['cash_high'], 'Invalid cash VWAP outcome')
        require(len(cash[day]) == 390,'Incomplete cash outcome')
        outcomes.append(dict(file=path.relative_to(PROJECT).as_posix(),sha256=expected,role='outcome_only'))
        daily.append(dict(date=day,old_P=f['baseline_levels']['P'],new_P=f['levels']['P'],P_shift=f['levels']['P']-f['baseline_levels']['P'],
                          cash_open=cash[day][0]['open'],cash_VWAP=value['vwap'],
                          baseline_distance_to_VWAP=abs(f['baseline_levels']['P']-value['vwap']),composite_distance_to_VWAP=abs(f['levels']['P']-value['vwap']),
                          composite={name:touch.score(cash[day],f['levels'][name],name) for name in touch.NAMES},
                          baseline={name:touch.score(cash[day],f['baseline_levels'][name],name) for name in touch.NAMES}))
    counts = {variant:{name:sum(d[variant][name]['touched'] for d in daily) for name in touch.NAMES} for variant in ('baseline','composite')}
    require(counts['baseline'] == read(V1/'report.json')['candidate_touch_counts'],'Original touch baseline did not reproduce')
    for f,d in zip(forecasts,daily):
        # Independent dataframe scorer audits all levels and first-touch minutes.
        import polars as pl
        frame = pl.DataFrame(cash[f['target_date']])
        for name,price in f['levels'].items():
            hit = frame.filter((pl.col('low')-1e-8 <= price)&(pl.col('high')+1e-8 >= price))
            require(d['composite'][name]['touched'] == (hit.height > 0),'Independent touch check failed')
            require(d['composite'][name]['first_touch_minute'] == (hit['timestamp'][0].isoformat() if hit.height else None),'First minute check failed')
    require(all(digest(HERE/name) == expected for name,expected in frozen['hashes'].items()),'Frozen candidate changed')
    require(all(digest(Path(path)) == expected for path,expected in preserved.items()),'Existing artifact modified')
    require(all(digest(PROJECT/item['file']) == item['sha256'] for item in inputs['source_manifest']),'Bound source changed')
    report = dict(version=contract['version'],sessions=contract['sessions'],cash_minutes=sum(len(rows) for rows in cash.values()),
                  daily=daily,touch_counts=counts,mean_P_distance_to_cash_VWAP={variant:mean(d[variant+'_distance_to_VWAP'] for d in daily) for variant in ('baseline','composite')},
                  any_level_touched_days={variant:sum(any(d[variant][name]['touched'] for name in touch.NAMES) for d in daily) for variant in ('baseline','composite')},
                  outcome_sources=outcomes,candidate_freeze=frozen,
                  checks=dict(raw_cash_options_OI_rebuild='passed',prior_profile_HV_ADX_RVOL_rebuild='passed',frozen_model_reproduction='passed',
                              prior_only_inputs='passed',unchanged_line_spacing='passed',cash_coverage='passed',baseline_reproduction='passed',
                              independent_touch_audit='passed',source_freeze_hashes='passed',original_files_preserved='passed',
                              probability_calibration='not_run',out_of_sample='not_run',profitability='not_applicable'),
                  preserved_root_files=len(preserved),calibrated_probabilities=False,financial_validation='Consumed five-day diagnostic; more inputs do not establish better accuracy.')
    render(forecasts,cash,daily)
    with (HERE/'daily.csv').open('w',newline='',encoding='utf-8') as stream:
        fields = ['date','old_P','new_P','P_shift','cash_open','cash_VWAP','baseline_distance_to_VWAP','composite_distance_to_VWAP']
        fields += [name+suffix for name in touch.NAMES for suffix in ('_old_price','_new_price','_old_touch','_new_touch')]
        writer = csv.DictWriter(stream,fieldnames=fields)
        writer.writeheader()
        for f,d in zip(forecasts,daily):
            row = {key:d[key] for key in fields if key in d}
            for name in touch.NAMES:
                row.update({name+'_old_price':f['baseline_levels'][name],name+'_new_price':f['levels'][name],
                            name+'_old_touch':d['baseline'][name]['touched'],name+'_new_touch':d['composite'][name]['touched']})
            writer.writerow(row)
    write(HERE/'report.json',report)
    print(json.dumps({key:report[key] for key in ('cash_minutes','touch_counts','mean_P_distance_to_cash_VWAP','checks')}))


if __name__ == '__main__':
    main()
