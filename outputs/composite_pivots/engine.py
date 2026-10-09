"""Frozen, prior-session-only QQQ composite pivots. No fitting or outcome scoring."""
import csv
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import statistics
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
OUTPUTS = HERE.parent
RANGE = OUTPUTS/'qqq_range'
DATA = OUTPUTS/'qqq_composite'/'data'
NY = ZoneInfo('America/New_York')
BIN = .05
PROFILE_WEIGHTS = (1., 2., 3., 4., 5.)
FAMILY_WEIGHTS = dict(profiles=.65, vwap=.20, options=.15)


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    return h.hexdigest()


def json_write(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    temporary.replace(path)


def stamp(date, time):
    return dt.datetime.combine(dt.date.fromisoformat(date),dt.time.fromisoformat(time),NY)


def finite(x):
    return isinstance(x,(int,float)) and math.isfinite(x)


def weighted_stats(records):
    if not records or any(not finite(p) or p<=0 or not finite(v) or v<=0 for p,v in records):
        raise ValueError('Invalid price/volume records.')
    volume=math.fsum(v for _,v in records)
    mean=math.fsum(p*v for p,v in records)/volume
    sigma=math.sqrt(math.fsum(v*(p-mean)**2 for p,v in records)/volume)
    return mean,sigma,volume


def volume_profile(bins):
    bins={int(k):float(v) for k,v in bins.items()}
    if not bins or any(not finite(v) or v<=0 for v in bins.values()):
        raise ValueError('Invalid volume profile.')
    total=math.fsum(bins.values())
    poc=min(bins,key=lambda k:(-bins[k],k))
    lo=hi=poc; covered=bins[poc]
    bottom,top=min(bins),max(bins)
    while covered<.70*total and (lo>bottom or hi<top):
        left=bins.get(lo-1,0.) if lo>bottom else -1.
        right=bins.get(hi+1,0.) if hi<top else -1.
        if left>=right: lo-=1; covered+=max(left,0.)
        else: hi+=1; covered+=max(right,0.)
    return dict(poc=round((poc+.5)*BIN,10),val=round(lo*BIN,10),vah=round((hi+1)*BIN,10),
                total_volume=total,va_volume=covered,va_fraction=covered/total,
                poc_bin=poc,va_bin_low=lo,va_bin_high=hi)


def historical_volatility(closes):
    if len(closes)!=21 or any(not finite(x) or x<=0 for x in closes):
        raise ValueError('HV requires 21 valid completed closes.')
    return statistics.stdev(math.log(b/a) for a,b in zip(closes,closes[1:]))


def relative_volume(anchor, references):
    if len(references)!=20 or not anchor:
        raise ValueError('RVOL requires 20 preceding sessions and cash clocks.')
    ratios={}; expected=0.
    for clock,value in anchor.items():
        if not finite(value) or value<0:
            raise ValueError('Invalid anchor cash clock volume for RVOL: '+clock)
        values=[r[clock] for r in references if clock in r]
        if not values: raise ValueError('No matching prior cash clock for RVOL: '+clock)
        if any(not finite(v) or v<0 for v in values): raise ValueError('Invalid reference volume.')
        mean=math.fsum(values)/len(values)
        if mean<=0: raise ValueError('Zero matching reference volume.')
        ratios[clock]=value/mean; expected+=mean
    return math.fsum(anchor.values())/expected,ratios


def wilder_adx(bars, period=14):
    """First DX at bar index 14; first ADX at index 27 (28 complete bars)."""
    values=[None]*len(bars)
    tr=[]; plus=[]; minus=[]; dx=[]
    smooth_tr=smooth_plus=smooth_minus=None; adx=None
    for i,b in enumerate(bars):
        if any(not finite(b[k]) or b[k]<=0 for k in ('high','low','close')) or b['low']>b['high']:
            raise ValueError('Invalid ADX bar.')
        if i==0: continue
        prior=bars[i-1]; up=b['high']-prior['high']; down=prior['low']-b['low']
        tr.append(max(b['high']-b['low'],abs(b['high']-prior['close']),abs(b['low']-prior['close'])))
        plus.append(up if up>down and up>0 else 0.)
        minus.append(down if down>up and down>0 else 0.)
        if len(tr)<period: continue
        if len(tr)==period:
            smooth_tr=math.fsum(tr); smooth_plus=math.fsum(plus); smooth_minus=math.fsum(minus)
        else:
            smooth_tr=smooth_tr-smooth_tr/period+tr[-1]
            smooth_plus=smooth_plus-smooth_plus/period+plus[-1]
            smooth_minus=smooth_minus-smooth_minus/period+minus[-1]
        total=smooth_plus+smooth_minus
        current=100*abs(smooth_plus-smooth_minus)/total if total>0 else 0.
        dx.append(current)
        if len(dx)==period: adx=math.fsum(dx)/period
        elif len(dx)>period: adx=(adx*(period-1)+current)/period
        values[i]=adx
    return values


def normalize_right(value):
    value=str(value).upper()
    return {'C':'CALL','P':'PUT','CALL':'CALL','PUT':'PUT'}.get(value)


def admit_options(quotes, interests, session, target, target_close, spot):
    """Exact contract join; publication filtering precedes latest OI selection."""
    cutoff=stamp(target,'08:00:00'); close=stamp(session['date'],session['close'])
    left=close-dt.timedelta(minutes=15); right=close+dt.timedelta(minutes=15)
    expiry=stamp(target,target_close)+dt.timedelta(minutes=15)
    days=(expiry.astimezone(dt.timezone.utc)-close.astimezone(dt.timezone.utc)).total_seconds()/86400
    if days<=0 or not finite(spot) or spot<=0: raise ValueError('Invalid option expiry/spot.')
    oi={}; oi_counts=dict(raw=len(interests),ineligible_identity_or_publication=0,nonpositive=0)
    def key(row):
        right=normalize_right(row.get('right'))
        strike=row.get('strike')
        if row.get('symbol')!='QQQ' or str(row.get('expiration'))!=target or right is None or not finite(strike) or strike<=0:
            return None
        return ('QQQ',target,float(strike),right)
    for row in interests:
        k=key(row); t=row.get('timestamp')
        if k is None or not isinstance(t,dt.datetime) or t.tzinfo is None or t>cutoff:
            oi_counts['ineligible_identity_or_publication']+=1; continue
        amount=row.get('open_interest')
        if not finite(amount): oi_counts['nonpositive']+=1; continue
        if k not in oi or t>oi[k]['timestamp']: oi[k]=row
        elif t==oi[k]['timestamp'] and amount!=oi[k]['open_interest']:
            raise ValueError('Conflicting OI at the same contract/publication timestamp.')
    # Select latest vintage first. A latest zero OI cannot fall back to older positive OI.
    oi_counts['latest_nonpositive_contracts']=sum(row['open_interest']<=0 for row in oi.values())
    oi={k:row for k,row in oi.items() if row['open_interest']>0}
    selected={}; reasons={}
    for row in quotes:
        k=key(row); t=row.get('timestamp'); reason=None
        if k is None: reason='identity'
        elif not isinstance(t,dt.datetime) or t.tzinfo is None or not left<=t<=right: reason='quote_window'
        else:
            b,a=row.get('bid'),row.get('ask')
            iv,gamma,delta,vega=[row.get(x) for x in ('implied_vol','gamma','delta','vega')]
            under,ut=row.get('underlying_price'),row.get('underlying_timestamp')
            if not all(finite(v) for v in (b,a,iv,gamma,delta,vega)): reason='nonfinite_quote_or_selected_greek'
            elif not 0<b<=a: reason='nonpositive_or_crossed_quote'
            elif (a-b)/((a+b)/2)>.50: reason='relative_spread'
            elif iv<=0 or gamma<0 or vega<0 or not .05<=abs(delta)<=.95: reason='greek_admission'
            elif not finite(under) or under<=0 or not isinstance(ut,dt.datetime) or ut.tzinfo is None: reason='invalid_underlying_reference'
            elif ut>cutoff: reason='future_underlying_timestamp'
            elif k not in oi: reason='no_positive_eligible_oi'
        if reason: reasons[reason]=reasons.get(reason,0)+1; continue
        if k not in selected or t>selected[k]['timestamp']: selected[k]=row
        elif t==selected[k]['timestamp'] and any(row[x]!=selected[k][x] for x in ('bid','ask','implied_vol','gamma','delta','vega')):
            raise ValueError('Conflicting closing Greek records at identical contract/timestamp.')
    admitted=[]; strike_weights={}; pairs={}
    vega_total=iv_sum=gamma_total=center_sum=0.
    for k,row in sorted(selected.items()):
        interest=oi[k]; amount=float(interest['open_interest']); delta=abs(row['delta'])
        weight=row['gamma']*amount*delta*(1-delta)
        vweight=row['vega']*amount; mid=(row['bid']+row['ask'])/2
        gamma_total+=weight; center_sum+=k[2]*weight
        vega_total+=vweight; iv_sum+=row['implied_vol']*vweight
        strike_weights[k[2]]=strike_weights.get(k[2],0.)+weight
        pairs.setdefault(k[2],{})[k[3]]=mid
        under=row.get('underlying_price'); ut=row.get('underlying_timestamp')
        admitted.append(dict(symbol=k[0],expiration=k[1],strike=k[2],right=k[3],bid=row['bid'],ask=row['ask'],mid=mid,
                             quote_timestamp=row['timestamp'].isoformat(),oi_timestamp=interest['timestamp'].isoformat(),
                             open_interest=amount,implied_vol_decimal=row['implied_vol'],gamma=row['gamma'],delta=row['delta'],vega=row['vega'],
                             concentration_weight=weight,iv_weight=vweight,underlying_price=under if finite(under) else None,
                             underlying_timestamp=ut.isoformat() if isinstance(ut,dt.datetime) else None,
                             underlying_minus_cash_close=under-spot if finite(under) else None,
                             underlying_minus_quote_seconds=(ut-row['timestamp']).total_seconds() if isinstance(ut,dt.datetime) else None))
    common=[k for k,p in pairs.items() if set(p)=={'CALL','PUT'}]
    if gamma_total<=0 or vega_total<=0 or not common:
        raise ValueError('Options lack positive gamma/vega concentration or a common eligible straddle.')
    strike=min(common,key=lambda k:(abs(k-spot),k)); premium=sum(pairs[strike].values())
    return dict(center=center_sum/gamma_total,iv=iv_sum/vega_total,straddle=premium,straddle_strike=strike,
                calendar_days=days,expiry_assumed_at=expiry.isoformat(),quote_window=[left.isoformat(),right.isoformat()],
                oi_cutoff=cutoff.isoformat(),greek_publication_assumed_at=stamp(session['date'],'17:15:00').isoformat(),
                greek_publication_state='reconstructed_retrospective_latest',
                concentration_interpretation='unsigned_contract_concentration_not_dealer_positioning',
                nodes=[dict(price=k,weight=v/gamma_total) for k,v in sorted(strike_weights.items()) if v>0],
                eligible_contracts=admitted,filter_counts=dict(raw_quotes=len(quotes),admitted=len(admitted),excluded_by_reason=reasons,oi=oi_counts))


def clip(x,lo,hi): return max(lo,min(hi,x))


def calculate(inputs):
    required=('spot','hv','adx','rvol','pooled_profile','recency_vwap','options','profile_nodes','vwap_nodes')
    if any(k not in inputs for k in required): raise ValueError('Missing composite input family.')
    p=inputs['pooled_profile']; o=inputs['options']; spot=inputs['spot']
    scalar=[spot,inputs['hv'],inputs['adx'],inputs['rvol'],inputs['recency_vwap']]+[p[k] for k in ('poc','val','vah')]+[o[k] for k in ('center','iv','straddle','calendar_days')]
    if not all(finite(x) for x in scalar) or spot<=0 or inputs['hv']<0 or not 0<=inputs['adx']<=100 or inputs['rvol']<0:
        raise ValueError('Invalid composite scalar.')
    if min(inputs['recency_vwap'],p['val'],p['poc'],p['vah'],o['center'],o['iv'],o['straddle'],o['calendar_days'])<=0 or not p['val']<=p['poc']<=p['vah']:
        raise ValueError('Invalid profile/options input.')
    centers=dict(profiles=.5*p['poc']+.25*p['vah']+.25*p['val'],vwap=inputs['recency_vwap'],options=o['center'])
    contributions={k:FAMILY_WEIGHTS[k]*v for k,v in centers.items()}
    pivot=math.fsum(contributions.values())
    scales=dict(hv=.50*(spot*inputs['hv'])**2,iv=.25*(spot*o['iv']/math.sqrt(365))**2,
                straddle=.25*(o['straddle']/math.sqrt(o['calendar_days']))**2)
    scale=math.sqrt(math.fsum(scales.values()))
    adx_adjust=.15*clip((inputs['adx']-25)/25,-.5,1)
    rvol_adjust=.10*clip(inputs['rvol']-1,-.5,1)
    regime=1+adx_adjust+rvol_adjust; adjusted=scale*regime
    if not finite(adjusted) or adjusted<=0: raise ValueError('Invalid adjusted dollar scale.')
    families=dict(profiles=inputs['profile_nodes'],vwap=inputs['vwap_nodes'],options=o['nodes'])
    for name,nodes in families.items():
        if not nodes or any(not finite(n['price']) or n['price']<=0 or not finite(n['weight']) or n['weight']<=0 for n in nodes):
            raise ValueError('Invalid/missing node family: '+name)
    width=.35*adjusted; levels={'P':round(pivot+1e-10,2)}; snaps={}
    for name,mult in [('S1',-.5),('S2',-1.),('S3',-1.5),('R1',.5),('R2',1.),('R3',1.5)]:
        initial=pivot+mult*adjusted; mass=weighted=0.; details={}
        for family,nodes in families.items():
            denominator=math.fsum(n['weight'] for n in nodes); fm=fw=0.; used=[]
            for node in nodes:
                distance=node['price']-initial
                if abs(distance)>3*width: continue
                kernel=math.exp(-.5*(distance/width)**2)
                weight=FAMILY_WEIGHTS[family]*node['weight']/denominator*kernel
                fm+=weight; fw+=weight*node['price']
                used.append(dict(price=node['price'],source_weight=node['weight'],kernel=kernel,proximity_weight=weight))
            mass+=fm; weighted+=fw
            details[family]=dict(kernel_mass=fm,weighted_price_sum=fw,nodes=used)
        nearby=weighted/mass if mass>0 else initial
        proposed=initial+.30*(nearby-initial)
        snaps[name]=dict(initial=initial,nearby_weighted_center=nearby,proposed=proposed,
                         movement=proposed-initial,families=details)
        levels[name]=proposed
    gap=math.ceil(.15*adjusted*100-1e-12)/100
    previous=levels['P']
    for name in ('R1','R2','R3'):
        levels[name]=round(max(previous+gap,round(levels[name]+1e-10,2)),2); previous=levels[name]
    previous=levels['P']
    for name in ('S1','S2','S3'):
        levels[name]=round(min(previous-gap,round(levels[name]+1e-10,2)),2); previous=levels[name]
    if levels['S3']<=0: raise ValueError('Nonpositive support after ordering.')
    for name in snaps:
        snaps[name]['final']=levels[name]; snaps[name]['ordering_rounding_adjustment']=levels[name]-snaps[name]['proposed']
    return dict(levels=levels,pivot_unrounded=pivot,pivot_contributions=contributions,
                scale_squared_contributions=scales,dollar_scale=scale,regime=regime,
                regime_contributions=dict(base=1.,adx=adx_adjust,rvol=rvol_adjust),
                adjusted_scale=adjusted,minimum_gap=gap,proximity_kernel_width=width,node_adjustments=snaps)


def method(qualification):
    return dict(version='frozen_composite_pivots_v1',symbol='QQQ',fitting='none',parameter_search='none',probability_claim='none',
                target='seven price levels for next cash session',as_of='08:00 America/New_York target morning',
                formulas=dict(P='.65*(.5*pooledPOC+.25*pooledVAH+.25*pooledVAL)+.20*recencyVWAP+.15*optionsCenter',
                              dollar_scale='sqrt(.50*(spot*HV)^2+.25*(spot*IV/sqrt(365))^2+.25*(ATMstraddle/sqrt(calendar_days))^2)',
                              regime='1+.15*clip((ADX-25)/25,-.5,1)+.10*clip(RVOL-1,-.5,1)',
                              levels='P +/- [.5,1,1.5]*dollar_scale*regime, then .30 node attraction',
                              options_center='sum(strike*gamma*OI*abs(delta)*(1-abs(delta)))/sum(gamma*OI*abs(delta)*(1-abs(delta)))',
                              iv='sum(decimal_IV*vega*OI)/sum(vega*OI)'),
                profile=dict(sessions=5,recency_weights=[1,2,3,4,5],per_day_normalized_volume=True,price_bin=.05,
                             bin_rule='floor(price/.05 + 1e-9), left-inclusive right-exclusive; POC bin midpoint',
                             va='contiguous greedy >=70%; adjacent larger-volume side; tie left; VA edges',poc_tie='lower price bin'),
                vwap='execution size weighted mean and population weighted sigma; each of 5 sessions contributes VWAP, +/-1,+/-2 sigma',
                hv='sample std of 20 completed close-to-close log returns; 21 cash closes; daily units',
                adx='Wilder14 on complete prior cash 15-minute OHLC; TR/DM 14-period sum seed; first ADX at 28 bars; no direction',
                rvol='anchor volume/sum matching-clock prior20 mean volume; missing shortened-day clocks omitted, never zero-filled; >=1 reference per clock; per-minute ratios and actual reference counts retained; anchor excluded; shortened anchor uses same span',
                attraction=dict(family_weights=FAMILY_WEIGHTS,movement=.30,gaussian_width=.35,cutoff_widths=3.,
                                gaussian_interpretation='proximity weighting only, not a future probability',
                                profile_nodes='prior5 VAH/VAL/POC each recency_weight/3',
                                vwap_nodes='prior5 VWAP and +/-1,+/-2 population sigma each recency_weight/5',
                                options_nodes='unsigned gamma*OI*abs(delta)*(1-abs(delta)) per strike'),
                ordering='P rounded to cents; R ascending and S descending with ceil(.15*adjusted_scale*100)/100 minimum gap; cent rounding',
                option_admission=dict(exact_identity='QQQ, next-session expiry, normalized CALL/PUT, exact floating strike',
                                      quote='positive bid<=ask, (ask-bid)/mid<=.50, anchor cash close-15min through close+15min',
                                      greeks='finite IV>0 gamma>=0 vega>=0 .05<=abs(delta)<=.95; decimal raw IV; vega ratios cancel common units',
                                      oi='positive latest eligible contract OI publication <=08:00 target morning',
                                      spot='prior final completed cash minute close; finite positive Greek underlyer with aware timestamp<=asof required; underlying/quote deviations retained',
                                      straddle='common eligible strike nearest spot; lower strike tie; CALLmid+PUTmid',
                                      calendar_days='UTC-instant elapsed seconds/86400 anchor cash close to target cash close+15min; respects shortened cash sessions and DST',
                                      vintage='latest EOD retrospective Greeks; 17:15 ET publication reconstructed, not an actual archived as-seen vintage'),
                greek_units=dict(iv='raw decimal fraction: .1417 means 14.17%; ATM Greek comparison empirically checked during source qualification',
                                 delta='signed underlying price sensitivity; absolute delta used in unsigned concentration',
                                 gamma='underlying price curvature; nonnegative raw source values',
                                 vega='raw unscaled dV/d(decimal annual sigma) retained, consistent with ATM Greek sample; per one volatility percentage point normalization is raw/100 per provider convention; IV weights use raw relative vega and the common scale cancels'),
                trade_condition_qualification=qualification,
                auction_tail_ingestion='auction_tails/manifest.json requests keyed anchor date: exact file+sha256+params; only explicit closing63/98 in any condition field in close through close+1min; full original-record dedup; assigned last cash-minute clock',
                classic_floor_pivots='prior cash OHLC reference only; separate from composite levels',
                sources=dict(trades='https://thetadata.net/docs/operations/stock_history_trade.html',
                             greeks='https://thetadata.net/docs/operations/option_history_greeks_eod.html',
                             oi='https://thetadata.net/docs/operations/option_history_open_interest.html'),
                limits=['No accuracy, calibrated probability, profitability, executable fill or live readiness claim.',
                        '208m-row source collection is not certified complete consolidated tape.',
                        'Explicit closing63/98 tails are captured only through cash close+1minute; later reports/corrected vintages remain uncertified.',
                        'Quotes may be after cash close; latest Greek vintages/publication are retrospective/reconstructed.',
                        'Gamma/OI measures unsigned contract concentration, not signed dealer positioning.',
                        'All families required; missing/warmup/invalid targets explicitly excluded; no partial fallback.',
                        'Target-day label/outcome records do not enter any forecast; no outcomes scored.'])


def source_manifest():
    status=json.loads((OUTPUTS/'qqq_composite'/'collection_status.json').read_text())
    acquired=json.loads((OUTPUTS/'qqq_composite'/'acquisition.json').read_text())
    rows=[]; byname={}
    for name,r in sorted(status['requests'].items()):
        if r['status']!='received': raise ValueError('Unreceived source: '+name)
        path=DATA/r['file']; actual=digest(path)
        if actual!=r['sha256']: raise ValueError('Source hash mismatch: '+name)
        entry=dict(name=name,file=path.relative_to(OUTPUTS).as_posix(),sha256=actual,rows=r['rows'],parameters=r['parameters'])
        rows.append(entry); byname[name]=entry
    for r in acquired['reused_inputs']:
        path=OUTPUTS/r['file']; actual=digest(path)
        if actual!=r['sha256']: raise ValueError('Reused source hash mismatch: '+r['name'])
        entry=dict(name=r['name'],file=path.relative_to(OUTPUTS).as_posix(),sha256=actual,rows=r['rows'])
        rows.append(entry); byname[r['name']]=entry
    for path in [RANGE/'sessions.json',OUTPUTS/'qqq_composite'/'acquisition.json',OUTPUTS/'qqq_composite'/'collection_status.json']:
        rows.append(dict(name=path.name,file=path.relative_to(OUTPUTS).as_posix(),sha256=digest(path)))
    return rows,byname,acquired


def read_cash_prices(sessions):
    import polars as pl
    frames=[pl.read_parquet(p) for p in sorted((RANGE/'data').glob('prices_*.parquet'))]
    records=pl.concat(frames).sort('timestamp').to_dicts()
    byday={}
    for row in records: byday.setdefault(row['timestamp'].astimezone(NY).date().isoformat(),[]).append(row)
    cash={}; errors={}
    for session in sessions:
        day=session['date']; opening=stamp(day,session['open']); close=stamp(day,session['close'])
        rows=[r for r in byday.get(day,[]) if opening<=r['timestamp']<close]
        expected={opening+dt.timedelta(minutes=i) for i in range(int((close-opening).total_seconds()/60))}
        reasons=[]
        if len(rows)!=len(expected) or {r['timestamp'] for r in rows}!=expected: reasons.append('incomplete_or_duplicate_cash_price_minutes')
        for r in rows:
            if any(not finite(r[k]) or r[k]<=0 for k in ('open','high','low','close')) or r['high']<max(r['open'],r['close'])-1e-8 or r['low']>min(r['open'],r['close'])+1e-8:
                reasons.append('invalid_cash_ohlc'); break
        cash[day]=rows; errors[day]=reasons
    return cash,errors


def feature_binding(session, source_hashes, method_hash, engine_hash):
    return dict(source_hashes=source_hashes,method_sha256=method_hash,engine_sha256=engine_hash,
                session_calendar={k:session[k] for k in ('date','open','close')})


def cache_matches(cached, binding):
    return all(cached.get(k)==v for k,v in binding.items())


def session_feature(session, source, price_source, cash, condition_rule, method_hash, engine_hash, tail=None):
    """One trade file at a time; aggregated cache is bound to exact input hashes."""
    import polars as pl
    day=session['date']; cache=HERE/'features'/f'{day}.json'
    hashes={source['file']:source['sha256'],price_source['file']:price_source['sha256']}
    if tail: hashes[tail['file']]=tail['sha256']
    binding=feature_binding(session,hashes,method_hash,engine_hash)
    if cache.exists():
        old=json.loads(cache.read_text())
        if cache_matches(old,binding): return old
    frame=pl.read_parquet(OUTPUTS/source['file']); raw=frame.height; tail_added=0
    opening=stamp(day,session['open']); close=stamp(day,session['close'])
    tail_counts=None
    if tail:
        extra=pl.read_parquet(OUTPUTS/tail['file'])
        tagged=pl.lit(False)
        fields=['condition']+[c for c in extra.columns if c.startswith('ext_condition')]
        for field in fields: tagged=tagged|pl.col(field).is_in([63,98])
        closing=extra.filter(tagged&(pl.col('timestamp')>=close)&(pl.col('timestamp')<=close+dt.timedelta(minutes=1)))
        # Only tail overlap is deduplicated, against the entire original event record.
        novel=closing.join(frame,on=frame.columns,how='anti')
        tail_counts=dict(raw_records=extra.height,explicit_closing_records=closing.height,
                         excluded_nonclosing_or_outside_tail_window=extra.height-closing.height,
                         exact_original_record_overlap=closing.height-novel.height,novel_closing_records=novel.height)
        tail_added=novel.height
        frame=pl.concat([frame,novel],how='vertical')
    tagged=pl.lit(False)
    fields=['condition']+[c for c in frame.columns if c.startswith('ext_condition')]
    for field in fields: tagged=tagged|pl.col(field).is_in([63,98])
    inwindow=((pl.col('timestamp')>=opening)&(pl.col('timestamp')<=close))|(tagged&(pl.col('timestamp')>=close)&(pl.col('timestamp')<=close+dt.timedelta(minutes=1)))
    validprice=pl.col('price').is_finite()&(pl.col('price')>0)&(pl.col('size')>0)
    eligible=pl.col('condition').is_in(condition_rule['volume_eligible_codes'])
    disallowed=condition_rule['cancel_codes']+condition_rule['cash_scope_exclusions']
    known=[int(k) for k in condition_rule['conditions']]+[condition_rule['absence_sentinel_assumption']]
    unknown_counts={}
    for field in fields:
        eligible=eligible&(~pl.col(field).is_in(disallowed))&pl.col(field).is_in(known)
        unknown=frame.filter(~pl.col(field).is_in(known)).group_by(field).len().to_dicts()
        if unknown: unknown_counts[field]=unknown
    counts=dict(raw_source_records=raw,merged_records=frame.height,auction_tail_added=tail_added,
                outside_cash_window=frame.filter(~inwindow).height,
                invalid_price_or_size=frame.filter(inwindow&(~validprice)).height,
                ineligible_condition=frame.filter(inwindow&validprice&(~eligible)).height,
                unknown_condition_records_by_field=unknown_counts,auction_tail=tail_counts)
    good=frame.filter(inwindow&validprice&eligible).with_columns(((pl.col('price')/BIN+1e-9).floor().cast(pl.Int64)).alias('bin'))
    counts['eligible_records']=good.height
    if good.height==0: raise ValueError('No eligible trade records on '+day)
    aggregate=good.select(pl.col('size').sum().alias('volume'),(pl.col('price')*pl.col('size')).sum().alias('pv')).row(0,named=True)
    volume=float(aggregate['volume']); vwap=aggregate['pv']/volume
    variance=good.select(((pl.col('price')-vwap)**2*pl.col('size')).sum()).item()/volume
    sigma=math.sqrt(max(variance,0.))
    bins={r['bin']:float(r['volume']) for r in good.group_by('bin').agg(pl.col('size').sum().alias('volume')).to_dicts()}
    profile=volume_profile(bins)
    lastclock=(close-dt.timedelta(minutes=1)).strftime('%H:%M')
    minutes=good.with_columns(pl.when(pl.col('timestamp')>=close).then(pl.lit(lastclock)).otherwise(pl.col('timestamp').dt.strftime('%H:%M')).alias('clock')).group_by('clock').agg(pl.col('size').sum().alias('volume')).sort('clock')
    minute_volume={r['clock']:float(r['volume']) for r in minutes.to_dicts()}
    expected=[(opening+dt.timedelta(minutes=i)).strftime('%H:%M') for i in range(int((close-opening).total_seconds()/60))]
    missing=[x for x in expected if x not in minute_volume]
    # Preserve zero-volume clocks explicitly; references with zero means later reject RVOL.
    minute_volume={x:minute_volume.get(x,0.) for x in expected}
    bands={name:vwap+k*sigma for name,k in [('minus_2sigma',-2),('minus_1sigma',-1),('vwap',0),('plus_1sigma',1),('plus_2sigma',2)]}
    bars=[]
    for i in range(0,len(cash),15):
        block=cash[i:i+15]
        if len(block)!=15: raise ValueError('Incomplete 15m cash bar.')
        bars.append(dict(timestamp=block[0]['timestamp'].isoformat(),open=block[0]['open'],high=max(r['high'] for r in block),
                         low=min(r['low'] for r in block),close=block[-1]['close']))
    feature=dict(date=day,**binding,profile=profile,bins={str(k):v for k,v in sorted(bins.items())},vwap=vwap,sigma=sigma,
                 vwap_bands=bands,eligible_volume=volume,minute_volume=minute_volume,missing_cash_trade_clocks=missing,
                 bars_15m=bars,cash_open=cash[0]['open'],cash_high=max(r['high'] for r in cash),cash_low=min(r['low'] for r in cash),cash_close=cash[-1]['close'],
                 first_trade_timestamp=good['timestamp'].min().isoformat(),last_trade_timestamp=good['timestamp'].max().isoformat(),
                 price_last_bar_start=cash[-1]['timestamp'].isoformat(),cash_close_at=close.isoformat(),filter_counts=counts,
                 tape_completeness_certified=False,auction_tail=tail,
                 condition_counts=frame.group_by('condition').agg(pl.len().alias('records'),pl.col('size').sum().alias('raw_size')).sort('condition').to_dicts())
    assert math.isclose(sum(bins.values()),volume,abs_tol=1e-8)
    assert math.isclose(sum(minute_volume.values()),volume,abs_tol=1e-8)
    json_write(cache,feature); return feature


def classic_pivots(f):
    h,l,c=f['cash_high'],f['cash_low'],f['cash_close']; p=(h+l+c)/3
    return {k:round(v,2) for k,v in dict(P=p,R1=2*p-l,S1=2*p-h,R2=p+h-l,S2=p-h+l,R3=h+2*(p-l),S3=l-2*(h-p)).items()}


def assembled_inputs(history, options):
    if len(history)<21: raise ValueError('warmup_requires_21_prior_completed_sessions')
    recent=history[-5:]; pooled={}; profile_nodes=[]; vwap_nodes=[]; weighted_vwap=0.
    for feature,weight in zip(recent,PROFILE_WEIGHTS):
        if feature.get('invalid_reasons'): raise ValueError('invalid_prior_profile_session_'+feature['date'])
        for k,v in feature['bins'].items(): pooled[int(k)]=pooled.get(int(k),0.)+weight/15*v/feature['eligible_volume']
        weighted_vwap+=weight/15*feature['vwap']
        profile_nodes.extend(dict(price=feature['profile'][k],weight=weight/15/3,date=feature['date'],kind=k) for k in ('poc','val','vah'))
        vwap_nodes.extend(dict(price=v,weight=weight/15/5,date=feature['date'],kind=k) for k,v in feature['vwap_bands'].items())
    closes=history[-21:]
    if any(f.get('invalid_reasons') for f in closes): raise ValueError('invalid_hv_or_rvol_reference_session')
    hv=historical_volatility([f['cash_close'] for f in closes])
    rvol,minute_rvol=relative_volume(history[-1]['minute_volume'],[f['minute_volume'] for f in history[-21:-1]])
    reference_counts={clock:sum(clock in f['minute_volume'] for f in history[-21:-1]) for clock in history[-1]['minute_volume']}
    if history[-1].get('adx') is None: raise ValueError('adx_warmup_or_invalid_history')
    overlaps=[]
    for i,a in enumerate(recent):
        for b in recent[i+1:]:
            lo=max(a['profile']['val'],b['profile']['val']); hi=min(a['profile']['vah'],b['profile']['vah'])
            overlaps.append(dict(dates=[a['date'],b['date']],lower=lo,upper=hi,width=max(0.,hi-lo),overlaps=hi>=lo))
    return dict(spot=history[-1]['cash_close'],hv=hv,adx=history[-1]['adx'],rvol=rvol,
                pooled_profile=volume_profile(pooled),recency_vwap=weighted_vwap,options=options,
                profile_nodes=profile_nodes,vwap_nodes=vwap_nodes,profile_dates=[f['date'] for f in recent],
                hv_close_dates=[f['date'] for f in closes],rvol_reference_dates=[f['date'] for f in history[-21:-1]],
                minute_rvol=minute_rvol,rvol_reference_counts=reference_counts,
                rvol_reference_count_min=min(reference_counts.values()),rvol_reference_count_max=max(reference_counts.values()),
                prior_value_area_overlaps=overlaps,
                source_session_features=[dict(date=f['date'],cash_close=f['cash_close'],cash_close_at=f['cash_close_at'],
                                             vwap=f['vwap'],sigma=f['sigma'],profile=f['profile'],filter_counts=f['filter_counts']) for f in recent])


def prior_inputs(history, target_date, options):
    """Date admission occurs before rolling feature selection; future rows cannot enter."""
    prior=sorted((f for f in history if f['date']<target_date),key=lambda f:f['date'])
    if len({f['date'] for f in prior})!=len(prior): raise ValueError('Duplicate source session date.')
    return assembled_inputs(prior,options)


def main():
    import polars as pl
    rule_path=HERE/'condition_policy.json'
    if not rule_path.exists(): raise ValueError('Missing source-qualified condition_policy.json; volume semantics not inferred.')
    rule=json.loads(rule_path.read_text())
    if not rule.get('source_sha256') or 115 not in rule['volume_eligible_codes'] or any(c in rule['volume_eligible_codes'] for c in range(40,45)):
        raise ValueError('Condition qualification must preserve volume eligible oddlot115 and exclude cancels40-44.')
    source_rows,byname,acquired=source_manifest()
    source_rows.append(dict(name='condition_eligibility',file=rule_path.relative_to(OUTPUTS).as_posix(),sha256=digest(rule_path)))
    definition=method(rule); json_write(HERE/'method.json',definition)
    method_hash=digest(HERE/'method.json'); engine_hash=digest(HERE/'engine.py')
    roster=json.loads((RANGE/'sessions.json').read_text()); cash,price_errors=read_cash_prices(roster)
    nextday=acquired['next_session_after_latest_close']
    calendars=pl.concat([pl.read_parquet(RANGE/'data'/f'calendar_{y}.parquet') for y in (2025,2026)]).to_dicts()
    target_closes={s['date']:s['close'] for s in roster}
    for row in calendars:
        day=str(row['date'])
        if row['type']!='full_close' and 'close' in row: target_closes[day]=str(row['close'])
    target_closes.setdefault(nextday,'16:00:00')
    tails={}; tail_manifest=HERE/'auction_tails'/'manifest.json'
    if tail_manifest.exists():
        source_rows.append(dict(name='auction_tail_manifest',file=tail_manifest.relative_to(OUTPUTS).as_posix(),sha256=digest(tail_manifest)))
        for day,record in json.loads(tail_manifest.read_text())['requests'].items():
            row={**record,'date':day,'file':(HERE/'auction_tails'/record['file']).relative_to(OUTPUTS).as_posix()}
            path=OUTPUTS/row['file']
            if digest(path)!=row['sha256']: raise ValueError('Auction tail hash mismatch.')
            tails[day]=row
            source_rows.append(dict(name='auction_tail_'+day,file=row['file'],sha256=row['sha256']))
        if len(tails)!=len(roster): raise ValueError('Closing-auction tail acquisition must finish for every source session before final calculation.')
    history=[]; forecasts=[]; exclusions=[]; features=[]; all_bars=[]; feature_manifest=[]
    prior_price_hashes={}
    for i,session in enumerate(roster):
        day=session['date']; target=roster[i+1]['date'] if i+1<len(roster) else nextday
        price_name='prices_'+day[:7].replace('-','_')
        invalid=list(price_errors[day]); feature=None
        if not invalid:
            try: feature=session_feature(session,byname['trades_'+day],byname[price_name],cash[day],rule,method_hash,engine_hash,tails.get(day))
            except ValueError as exc: invalid.append(str(exc))
        if feature is None:
            feature=dict(date=day,invalid_reasons=invalid,adx=None)
            all_bars=[]
        else:
            all_bars.extend(feature['bars_15m'])
            prior_price_hashes[byname[price_name]['file']]=byname[price_name]['sha256']
            feature={**feature,'adx':wilder_adx(all_bars)[-1] if all_bars else None,'invalid_reasons':invalid}
            feature['adx_lineage']=dict(price_source_hashes=dict(prior_price_hashes),bars=len(all_bars),
                                       first_bar=all_bars[0]['timestamp'],last_bar=all_bars[-1]['timestamp'],
                                       seed='Wilder14 TR/DM sums; first DX at14, first ADX at27 bar index')
            cache=HERE/'features'/f'{day}.json'; json_write(cache,feature)
            feature_manifest.append(dict(file=cache.relative_to(HERE).as_posix(),sha256=digest(cache),source_hashes=feature['source_hashes']))
        history.append(feature); features.append(dict(date=day,adx=feature['adx'],invalid_reasons=invalid,feature_file=f'features/{day}.json' if not invalid else None))
        reasons=list(invalid)
        if i<20: reasons.append('warmup_requires_21_prior_completed_sessions')
        if not reasons:
            try:
                qsource=byname['closing_greeks_'+day]; osource=byname['open_interest_'+day]
                q=pl.read_parquet(OUTPUTS/qsource['file']).to_dicts(); oi=pl.read_parquet(OUTPUTS/osource['file']).to_dicts()
                options=admit_options(q,oi,session,target,target_closes.get(target,'16:00:00'),feature['cash_close'])
                options['source_hashes']={qsource['file']:qsource['sha256'],osource['file']:osource['sha256']}
                options['source_parameters']=dict(greeks=qsource['parameters'],open_interest=osource['parameters'])
                inputs=prior_inputs(history,target,options); result=calculate(inputs)
                forecast=dict(symbol='QQQ',mode='full_composite_v1',target_date=target,anchor_date=day,as_of=stamp(target,'08:00:00').isoformat(),
                              inputs=inputs,**result,classic_floor_reference=classic_pivots(feature),method_sha256=method_hash,
                              engine_sha256=engine_hash,tape_completeness_certified=False)
                forecasts.append(forecast)
            except (ValueError,KeyError) as exc: reasons.append(str(exc))
        if reasons: exclusions.append(dict(target_date=target,anchor_date=day,as_of=stamp(target,'08:00:00').isoformat(),reasons=reasons,mode='excluded_no_fallback'))
        if (i+1)%25==0: print(f'Aggregated {i+1}/{len(roster)} prior sessions; full forecasts {len(forecasts)}',flush=True)
    json_write(HERE/'features.json',features); json_write(HERE/'forecasts.json',forecasts); json_write(HERE/'exclusions.json',exclusions)
    json_write(HERE/'latest.json',forecasts[-1] if forecasts and forecasts[-1]['target_date']==nextday else dict(target_date=nextday,status='excluded',reasons=exclusions[-1]['reasons']))
    with (HERE/'forecasts.csv').open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=['target_date','anchor_date','as_of','P','R1','R2','R3','S1','S2','S3','dollar_scale','regime','adjusted_scale','spot','hv','iv','atm_straddle','adx','rvol'])
        writer.writeheader()
        for f in forecasts:
            row={k:f[k] for k in ('target_date','anchor_date','as_of','dollar_scale','regime','adjusted_scale')}; row.update(f['levels'])
            row.update({k:f['inputs'][k] for k in ('spot','hv','adx','rvol')}); row.update(iv=f['inputs']['options']['iv'],atm_straddle=f['inputs']['options']['straddle']); writer.writerow(row)
    valid_features=[f for f in history if not f.get('invalid_reasons')]
    filter_totals={k:sum(f['filter_counts'][k] for f in valid_features) for k in ('raw_source_records','merged_records','auction_tail_added','outside_cash_window','invalid_price_or_size','ineligible_condition','eligible_records')}
    tail_totals={k:sum((f['filter_counts']['auction_tail'] or {}).get(k,0) for f in valid_features) for k in ('raw_records','explicit_closing_records','excluded_nonclosing_or_outside_tail_window','exact_original_record_overlap','novel_closing_records')}
    report=dict(status='calculated' if forecasts else 'no_eligible_full_forecasts',symbol='QQQ',source_sessions=len(roster),forecast_count=len(forecasts),exclusion_count=len(exclusions),
                first_target=roster[1]['date'],latest_target=nextday,first_eligible_target=forecasts[0]['target_date'] if forecasts else None,
                latest_status='full_composite_calculated' if forecasts and forecasts[-1]['target_date']==nextday else 'excluded',
                source_manifest=source_rows,feature_manifest=feature_manifest,engine_sha256=engine_hash,method_sha256=method_hash,
                session_roster_sha256=digest(RANGE/'sessions.json'),
                forecast_sha256=digest(HERE/'forecasts.json'),accuracy_status='not_run',outcome_scoring='not_run',fitting='none',tape_completeness_certified=False,
                total_trade_source_records=sum(r.get('rows',0) for r in source_rows if r['name'].startswith('trades_')),
                trade_filter_totals=filter_totals,auction_tail_totals=tail_totals,
                eligible_share_volume=math.fsum(f['eligible_volume'] for f in valid_features),
                missing_cash_trade_clocks=sum(len(f['missing_cash_trade_clocks']) for f in valid_features),
                source_features_calculated=len(valid_features),
                raw_trade_files_processed_one_at_a_time=True,limits=definition['limits'],
                architect_update=dict(diagnostic='All-input prior-session composite; volume and timestamp admission explicit.',
                                      changes='Frozen engine, hash-bound per-session features, forecasts/exclusions and independent assert check.',
                                      quality_gates='Run check.py and check.py --artifacts; calculation itself is not an accuracy test.',
                                      system_state='Calculated heuristic levels; retrospective Greeks and tape completeness remain qualified.'))
    json_write(HERE/'report.json',report)
    print(f'COMPOSITE_PIVOTS_CALCULATED forecasts={len(forecasts)} exclusions={len(exclusions)} latest={nextday}',flush=True)


if __name__=='__main__': main()
