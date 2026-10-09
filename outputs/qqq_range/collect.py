"""Bounded, resumable QQQ history collector using ThetaData's official SDK.

Run with the workspace's work/theta-venv Python. Credential is Windows DPAPI,
never part of the source, data, request manifest, or diagnostic output.
"""
import argparse
import calendar
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess
import threading
import time

HERE = Path(__file__).resolve().parent
DATA = HERE / 'data'
START = dt.date(2025, 10, 8)
END = dt.date(2026, 10, 7)
PRIOR = dt.date(2025, 10, 7)
LOCK = threading.Lock()


def load_key():
    secret = Path.home() / '.codex/secrets/thetadata-qqq-range.dpapi.xml'
    command = "$s=Import-Clixml -LiteralPath '" + str(secret).replace("'", "''") + "';[System.Net.NetworkCredential]::new('', $s).Password"
    return subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', command], capture_output=True, text=True, check=True).stdout.strip()


def safe_error(error, key):
    return re.sub(r'td1_[A-Za-z0-9_]+', '[REDACTED]', str(error).replace(key, '[REDACTED]'))[:1200]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')
    tmp.replace(path)


def sessions(calendar_rows):
    special = {str(r['date']): r for r in calendar_rows}
    days = []
    day = PRIOR
    while day <= END:
        event = special.get(str(day), {})
        if day.weekday() < 5 and event.get('type') != 'full_close':
            close = str(event.get('close') or '16:00:00')
            days.append({'date': str(day), 'open': '09:30:00', 'close': close,
                         'cutoff': '12:45:00' if close.startswith('13:') else '15:45:00'})
        day += dt.timedelta(days=1)
    return days


def collect():
    import polars as pl
    from thetadata import ThetaClient
    DATA.mkdir(parents=True, exist_ok=True)
    key = load_key()
    client = ThetaClient(api_key=key, dataframe_type='polars')
    previous_path = HERE / 'collection_status.json'
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {'requests': []}
    cached_requests = {r['name']: r for r in previous['requests'] if 'file' in r}
    status = {'symbol': 'QQQ', 'start': str(START), 'end': str(END),
              'authentication': 'verified', 'state': 'collecting', 'requests': [],
              'minute_venue': 'utp_cta', 'options': 'prior-session cutoff, next-session expiry',
              'created_utc': dt.datetime.now(dt.timezone.utc).isoformat()}

    def request(name, parameters, job, optional=False):
        path = DATA / (name + '.parquet')
        row = {'name': name, 'parameters': parameters, 'optional': optional}
        if path.exists():
            original = cached_requests.get(name)
            if not original or original['parameters'] != parameters or original['sha256'] != digest(path):
                raise RuntimeError('Cache provenance or parameters do not match: ' + name)
            frame = pl.read_parquet(path)
            row['status'] = 'cached'
            row['original_recorded_utc'] = original.get('original_recorded_utc', original['recorded_utc'])
        else:
            for attempt in range(3):
                try:
                    frame = job()
                    temporary = path.with_suffix('.tmp')
                    frame.write_parquet(temporary)
                    temporary.replace(path)
                    row['status'] = 'received'
                    break
                except Exception as error:
                    if attempt < 2 and any(s in str(error) for s in ('RESOURCE_EXHAUSTED', 'UNAVAILABLE', 'DEADLINE_EXCEEDED')):
                        time.sleep(2 ** attempt)
                        continue
                    row.update(status='failed', error=safe_error(error, key))
                    frame = None
                    break
        if frame is not None:
            row.update(rows=frame.height, fields=frame.columns, file=path.name, sha256=digest(path))
        row['recorded_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
        with LOCK:
            status['requests'].append(row)
            atomic_json(HERE / 'collection_status.json', status)
            print(json.dumps({k: row[k] for k in ('name', 'status', 'rows', 'error') if k in row}), flush=True)
        return frame

    holiday_rows = []
    for year in (2025, 2026):
        frame = request(f'calendar_{year}', {'year': year}, lambda y=year: client.calendar_year(str(y)))
        if frame is None:
            raise RuntimeError('Exchange calendar unavailable; collection cannot safely select session dates.')
        holiday_rows.extend(frame.to_dicts())
    roster = sessions(holiday_rows)
    atomic_json(HERE / 'sessions.json', roster)
    status['expected_target_sessions'] = len(roster) - 1

    # Monthly requests stay within the documented one-month OHLC limit.
    month = PRIOR.replace(day=1)
    while month <= END:
        last = month.replace(day=calendar.monthrange(month.year, month.month)[1])
        lo, hi = max(month, PRIOR), min(last, END)
        params = {'symbol': 'QQQ', 'start_date': str(lo), 'end_date': str(hi), 'interval': '1m',
                  'start_time': '09:30:00', 'end_time': '16:00:00', 'venue': 'utp_cta'}
        request(f'prices_{month:%Y_%m}', params, lambda a=lo,b=hi: client.stock_history_ohlc(
            symbol='QQQ', start_date=a, end_date=b, interval='1m', start_time='09:30:00', end_time='16:00:00', venue='utp_cta'))
        month = (last + dt.timedelta(days=1)).replace(day=1)

    # Two independent read requests at a time; no full-chain minute download.
    def snapshots(pair):
        previous, target = pair
        day = dt.date.fromisoformat(previous['date'])
        expiry = dt.date.fromisoformat(target['date'])
        cutoff = previous['cutoff']
        params = {'symbol': 'QQQ', 'date': str(day), 'expiration': str(expiry), 'cutoff': cutoff, 'strike_range': 5}
        request('quotes_' + str(expiry), params, lambda: client.option_at_time_quote(
            symbol='QQQ', start_date=day, end_date=day, expiration=expiry, time_of_day=cutoff, strike_range=5))
        request('greeks_' + str(expiry), {**params, 'interval': '1m', 'version': 'latest'}, lambda: client.option_history_greeks_first_order(
            symbol='QQQ', date=day, expiration=expiry, interval='1m', start_time=cutoff, end_time=cutoff, strike_range=5), optional=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(snapshots, pair) for pair in zip(roster[:-1], roster[1:])]
        for future in as_completed(futures):
            future.result()
    status['state'] = 'collected_with_failures' if any(r['status'] == 'failed' for r in status['requests']) else 'received'
    status['finished_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
    atomic_json(HERE / 'collection_status.json', status)
    verify()


def verify():
    import polars as pl
    from zoneinfo import ZoneInfo
    roster = json.loads((HERE / 'sessions.json').read_text())
    status = json.loads((HERE / 'collection_status.json').read_text())
    price_files = sorted(DATA.glob('prices_*.parquet'))
    if not price_files:
        raise RuntimeError('No price files collected.')
    frame = pl.concat([pl.read_parquet(p) for p in price_files]).sort('timestamp')
    grouped = {}
    for row in frame.iter_rows(named=True):
        stamp = row['timestamp']
        if stamp.tzinfo is None:
            raise RuntimeError('Provider timestamps lack timezone; do not guess.')
        stamp = stamp.astimezone(ZoneInfo('America/New_York'))
        grouped.setdefault(str(stamp.date()), []).append({**row, 'timestamp': stamp})
    gaps, eligible, quotes_missing, greeks_missing = [], [], [], []
    for session in roster:
        day = dt.date.fromisoformat(session['date'])
        start = dt.datetime.combine(day, dt.time.fromisoformat(session['open']), ZoneInfo('America/New_York'))
        close = dt.datetime.combine(day, dt.time.fromisoformat(session['close']), ZoneInfo('America/New_York'))
        actual = {r['timestamp'] for r in grouped.get(str(day), []) if start <= r['timestamp'] < close}
        expected = {start + dt.timedelta(minutes=i) for i in range(int((close-start).total_seconds()/60))}
        missing = expected - actual
        duplicates = len([r for r in grouped.get(str(day), []) if start <= r['timestamp'] < close]) - len(actual)
        if missing or duplicates:
            gaps.append({'date': str(day), 'missing_minutes': len(missing), 'duplicate_minutes': duplicates})
        else:
            eligible.append(str(day))
    for target in roster[1:]:
        for prefix, collection in [('quotes_', quotes_missing), ('greeks_', greeks_missing)]:
            path = DATA / (prefix + target['date'] + '.parquet')
            if not path.exists() or not pl.read_parquet(path).height:
                collection.append(target['date'])
    hashes_ok = all((DATA / r['file']).exists() and digest(DATA / r['file']) == r['sha256'] for r in status['requests'] if 'file' in r)
    result = {'target_sessions': len(roster)-1, 'price_rows_received': frame.height,
              'complete_cash_sessions_including_prior': len(eligible), 'minute_gaps': gaps,
              'quotes_missing_or_empty': quotes_missing, 'greeks_missing_or_empty': greeks_missing,
              'artifact_hashes_match': hashes_ok,
              'full_year_price_and_quote_coverage': not gaps and not quotes_missing and hashes_ok,
              'note': 'Quote presence is separate from quote quality and model eligibility.'}
    atomic_json(HERE / 'coverage.json', result)
    print(json.dumps(result, indent=2), flush=True)
    if result['full_year_price_and_quote_coverage']:
        print('DATA_COVERAGE_VERIFIED', flush=True)
    else:
        print('DATA_COVERAGE_INCOMPLETE', flush=True)
        raise SystemExit(2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    verify() if args.verify else collect()
