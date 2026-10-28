"""Local, numeric-only Codex usage ledger. No model calls or wiki credentials."""
import datetime
import hashlib
import json
import os
import time
from pathlib import Path
import re
import uuid
from zoneinfo import ZoneInfo

import devlog_wiki as devlog


def Root(repo):
    return devlog.State(repo)/'usage'


def Key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def Counters(value):
    """Codex cache/reasoning counters are subsets, not additional total tokens."""
    if not isinstance(value, dict):
        return None
    result = {}
    for field in ('input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_output_tokens'):
        number = value.get(field)
        if number is None and field not in ('input_tokens', 'output_tokens'):
            continue
        if type(number) is not int or number < 0:
            return None
        result[field] = number
    result['total_tokens'] = result['input_tokens']+result['output_tokens']
    total = value.get('total_tokens', result['total_tokens'])
    if type(total) is not int or total != result['total_tokens']:
        return None
    if result.get('cached_input_tokens', 0) > result['input_tokens'] or result.get('reasoning_output_tokens', 0) > result['output_tokens']:
        return None
    return result


def Events(lines, session_id, repo):
    """Only explicit per-response usage; cumulative snapshots are never added."""
    turns, requests, issues = {}, [], set()
    matched = False
    for line in lines:
        try:
            item = json.loads(line)
            payload = item.get('payload', {})
            if not isinstance(payload, dict):
                continue
            timestamp = devlog.Timestamp(item.get('timestamp'))
        except (ValueError, TypeError, AttributeError):
            issues.add('malformed_event')
            continue
        kind = item.get('type')
        if kind == 'session_meta':
            matched = payload.get('id', payload.get('session_id')) == session_id
        turn_id = payload.get('turn_id')
        if kind == 'event_msg' and payload.get('type') == 'task_started' and turn_id:
            turns.setdefault(turn_id, {})['started_at'] = timestamp
        if kind == 'turn_context' and turn_id:
            turn = turns.setdefault(turn_id, {})
            turn.update(model=payload.get('model'), cwd=payload.get('cwd'))
        if kind == 'token_usage_record':
            if payload.get('session_id', payload.get('thread_id')) != session_id:
                continue
            counters = Counters(payload.get('usage'))
            response_id = payload.get('response_id')
            if not counters or not isinstance(response_id, str) or not response_id:
                issues.add('invalid_request_usage')
                continue
            requests.append({'id': 'codex:'+response_id, 'session_id': session_id,
                             'turn_id': turn_id, 'occurred_at': timestamp, 'counters': counters,
                             'context': dict(turns.get(turn_id, {}))})
    if not matched:
        return [], ['session_identity_mismatch']
    result = []
    for event in requests:
        turn = event.pop('context')
        if not isinstance(turn.get('cwd'), str) or Path(turn['cwd']).resolve() != Path(repo).resolve():
            issues.add('workspace_not_verified')
            continue
        event.update(purpose='development', model=turn.get('model') or 'unknown',
                     started_at=turn.get('started_at'), requests=1, source='codex.token_usage_record')
        result.append(event)
    if not result:
        issues.add('request_usage_unavailable')
    return result, sorted(issues)


def Store(root, event):
    """Publish immutable events atomically; duplicate response IDs count once."""
    folder = root/'events'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder/(Key(event['id'])+'.json')
    if target.exists():
        prior = json.loads(target.read_text())
        if prior == event:
            return
        devlog.Write_json(root/'conflicts'/(Key(event['id'])+'.json'), {'id': event['id']})
        raise RuntimeError('usage event conflict; totals require review')
    temporary = folder/('.'+uuid.uuid4().hex)
    devlog.Write_json(temporary, event)
    try:
        try:
            os.link(temporary, target)
        except FileExistsError:
            prior = json.loads(target.read_text())
            if prior != event:
                devlog.Write_json(root/'conflicts'/(Key(event['id'])+'.json'), {'id': event['id']})
                raise RuntimeError('usage event conflict; totals require review')
    finally:
        temporary.unlink(missing_ok=True)


def Find_session(session_id):
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', session_id):
        return None
    home = Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))
    paths = []
    for folder in ('sessions', 'archived_sessions'):
        paths.extend((home/folder).rglob('*'+session_id+'*.jsonl'))
    return paths[0] if len(paths) == 1 else None


def Window_start(window, session_id):
    return window.get('session_after', {}).get(session_id, window.get('after'))


def Collect(repo, record, now=None):
    """Register a bounded commit window, then refresh only its exact sessions."""
    root = Root(repo)
    sessions = record.get('usage_sessions', []) or [record.get('imported_session') or record.get('session_id')]
    sessions = sorted({sid for sid in sessions if isinstance(sid, str) and re.fullmatch(r'[0-9a-fA-F-]{36}', sid)})
    if not record.get('commit'):
        return
    if not sessions:
        devlog.Write_json(root/'observations'/(record['commit']+'.json'),
                         {'commit': record['commit'], 'status': 'partial', 'issues': ['linked_session_unavailable']})
        return
    cutoff = record.get('observed_at') or devlog.Timestamp(devlog.Git(repo, 'show', '-s', '--format=%cI', record['commit']))+1
    window_path = root/'windows'/(record['commit']+'.json')
    window = json.loads(window_path.read_text()) if window_path.exists() else None
    if window is None or window.get('boundary_version') != 2:
        records = [json.loads(p.read_text()) for p in (devlog.State(repo)/'records').glob('*.json')]
        # Preserve the observed end of old windows; repair only their per-session starts.
        cutoff = window['before'] if window else cutoff
        sessions = window['sessions'] if window else sessions
        session_after = {}
        for sid in sessions:
            bounds = [prior['observed_at'] for prior in records
                      if prior.get('commit') != record['commit'] and prior.get('observed_at', cutoff) < cutoff
                      and sid in (prior.get('usage_sessions') or [prior.get('imported_session') or prior.get('session_id')])]
            if bounds:
                lower = max(bounds)
            elif window and len(sessions) == 1:
                lower = window['after']
            else:
                try:
                    lower = devlog.Timestamp(devlog.Git(repo, 'show', '-s', '--format=%cI', record['commit']+'^'))
                except RuntimeError:
                    lower = None
            session_after[sid] = lower if lower is not None and lower < cutoff else None
        window = {'commit': record['commit'], 'date': record['date'], 'sessions': sessions,
                  'after': None, 'session_after': session_after, 'before': cutoff,
                  'basis': 'per_session_commit_window', 'boundary_version': 2}
        devlog.Write_json(window_path, window)
    issues = []
    upper = now if now is not None else cutoff
    for session_id in window['sessions']:
        path = Find_session(session_id)
        if path is None:
            issues.append('exact_session_log_unavailable')
            continue
        with path.open() as stream:
            events, errors = Events(stream, session_id, repo)
        issues.extend(errors)
        starts = [Window_start(w, session_id) for p in (root/'windows').glob('*.json')
                  if session_id in (w := json.loads(p.read_text()))['sessions'] and Window_start(w, session_id) is not None]
        lower = min(starts) if starts else None
        for event in events:
            # Never pull a whole historical session into the first commit's total.
            if lower is None or not lower < event['occurred_at'] <= upper:
                continue
            try:
                Store(root, event)
            except RuntimeError:
                issues.append('usage_event_conflict')
    if not sessions:
        issues.append('linked_session_unavailable')
    if any(Window_start(window, sid) is None for sid in sessions):
        issues.append('interval_start_unavailable')
    devlog.Write_json(root/'observations'/(record['commit']+'.json'),
                     {'commit': record['commit'], 'checked_at': upper, 'status': 'partial', 'issues': sorted(set(issues))})


def Cli_usage(path, repo, owner, date, invocation, occurred_at, outcome='unknown', mode='unknown', attempt=None):
    """CLI turn totals do not expose individual API request count or guaranteed model."""
    count = 0
    if path.exists():
        with path.open() as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(item, dict) or item.get('type') != 'turn.completed':
                    continue
                counters = Counters(item.get('usage'))
                if counters is None:
                    continue
                event = {'id': 'wiki:'+invocation+':'+str(count), 'purpose': 'wiki',
                         'source': 'codex.exec.turn.completed', 'owner': owner, 'date': date,
                         'model': 'unknown', 'occurred_at': occurred_at, 'counters': counters,
                         'requests': None}
                Store(Root(repo), event)
                count += 1
    # Missing usage is an observation, never a zero-token event.
    devlog.Write_json(Root(repo)/'calls'/(invocation+'.json'),
                     {'owner': owner, 'date': date, 'occurred_at': occurred_at,
                      'usage_available': count > 0, 'invocation': invocation,
                      'outcome': outcome, 'mode': mode, 'attempt': attempt})


def Report(repo):
    root = Root(repo)
    windows = [json.loads(p.read_text()) for p in (root/'windows').glob('*.json')]
    events = [json.loads(p.read_text()) for p in sorted((root/'events').glob('*.json'))
              if not (root/'conflicts'/p.name).exists()]
    calls = [json.loads(p.read_text()) for p in (root/'calls').glob('*.json')]
    observations = [json.loads(p.read_text()) for p in (root/'observations').glob('*.json')]
    return Summarize(events, windows, calls, observations, len(list((root/'conflicts').glob('*.json'))))


def Summarize(events, windows, calls, observations, conflicts):
    for event in events:
        if event['purpose'] == 'development':
            eligible = [w for w in windows if event['session_id'] in w['sessions'] and Window_start(w, event['session_id']) is not None
                        and Window_start(w, event['session_id']) < event['occurred_at'] <= w['before']
                        and event.get('started_at') is not None and Window_start(w, event['session_id']) < event['started_at'] <= event['occurred_at']]
            event['owner'] = eligible[0]['commit'] if len(eligible) == 1 else None
            event['date'] = eligible[0]['date'] if len(eligible) == 1 else None
    totals = {}
    for event in events:
        occurred_date = datetime.datetime.fromtimestamp(event['occurred_at'], ZoneInfo('Asia/Seoul')).date().isoformat()
        for group in ('project', 'day:'+occurred_date, 'month:'+occurred_date[:7], 'model:'+event['model'],
                      'commit:'+str(event.get('owner') or 'unattributed')):
            bucket = totals.setdefault(group, {}).setdefault(event['purpose'], {'events': 0, 'tokens': 0,
                'input': 0, 'output': 0, 'cached_input': None, 'reasoning_output': None, 'requests': None})
            bucket['events'] += 1
            bucket['tokens'] += event['counters']['total_tokens']
            bucket['input'] += event['counters']['input_tokens']
            bucket['output'] += event['counters']['output_tokens']
            for output, field in [('cached_input', 'cached_input_tokens'), ('reasoning_output', 'reasoning_output_tokens')]:
                # None for a mixed group with missing fields, not a fabricated zero.
                if field in event['counters'] and (bucket['events'] == 1 or bucket[output] is not None):
                    bucket[output] = (bucket[output] or 0)+event['counters'][field]
                else:
                    bucket[output] = None
            if event['requests'] is not None and (bucket['events'] == 1 or bucket['requests'] is not None):
                bucket['requests'] = (bucket['requests'] or 0)+event['requests']
            else:
                bucket['requests'] = None
    return {'status': 'partial', 'timezone': 'Asia/Seoul', 'totals': totals, 'events': events,
            'calls': calls, 'observations': observations,
            'conflicts': conflicts}


def Central_report(client, config):
    for attempt in range(3):
        records, cursor, revision = [], '', None
        while True:
            result = client.Call('wiki_usage_read', project_id=config['project_id'], cursor=cursor)
            if result.get('response') != 'ok':
                raise RuntimeError('central usage read failed; local ledger preserved')
            if revision is not None and revision != result['revision']:
                break
            revision = result['revision']
            records.extend(result['records'])
            cursor = result['cursor']
            if not cursor:
                conflicts = {r['payload']['id'] for r in records if r['kind'] == 'conflict'}
                conflicts.update(r['payload']['id'] for r in records if r.get('conflict') and r['kind'] == 'event')
                good = [r for r in records if not r.get('conflict')]
                events = [r['payload'] for r in good if r['kind'] == 'event' and r['payload']['id'] not in conflicts]
                report = Summarize(events, [r['payload'] for r in good if r['kind'] == 'window'],
                                   [r['payload'] for r in good if r['kind'] == 'call'],
                                   [r['payload'] for r in good if r['kind'] == 'observation'],
                                   len(conflicts)+sum(bool(r.get('conflict')) for r in records if r['kind'] != 'event'))
                report.update(storage='central', revision=revision)
                return report
    raise RuntimeError('central usage changed during read; retry')


def Sync(repo, config, client):
    """Resend immutable evidence. The server owns deduplication and canonical totals."""
    root = Root(repo)
    state_path = root/'sync.json'
    try:
        # Session boundaries can be repaired from existing input records, without a model.
        for path in (devlog.State(repo)/'records').glob('*.json'):
            record = json.loads(path.read_text())
            if record.get('commit') and (root/'windows'/(record['commit']+'.json')).exists():
                window = json.loads((root/'windows'/(record['commit']+'.json')).read_text())
                if window.get('boundary_version') != 2:
                    Collect(repo, record)
        batch = []
        for folder, kind in [('events','event'), ('windows','window'), ('calls','call'),
                             ('observations','observation'), ('conflicts','conflict')]:
            for path in sorted((root/folder).glob('*.json')):
                batch.append({'kind': kind, 'payload': json.loads(path.read_text())})
        # Always register/check the binding, including an empty ledger.
        for offset in range(0, max(1, len(batch)), 100):
            result = client.Call('wiki_usage_sync', project_id=config['project_id'], overview=config['overview'],
                                 name=config['project'], records=batch[offset:offset+100])
            if result.get('response') != 'ok':
                raise RuntimeError('central usage sync failed; local ledger preserved')
        report = Central_report(client, config)
        devlog.Write_json(state_path, {'state':'done', 'checked_at':time.time(), 'revision':report['revision']})
        return report
    except Exception:
        devlog.Write_json(state_path, {'state':'pending', 'checked_at':time.time()})
        raise


def Usage_url(config):
    # Match the MCP client's existing local-only URL contract.
    import ipaddress
    from urllib.parse import urlsplit
    value = os.environ.get('NAMU_MCP_URL', 'http://127.0.0.1:3000').rstrip('/')
    parsed = urlsplit(value)
    if parsed.scheme != 'http' or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or not ipaddress.ip_address(parsed.hostname).is_loopback:
        raise RuntimeError('invalid local wiki URL')
    return value+'/personal_usage/'+config['project_id']


def Daily_summary(report, date):
    selected = [e for e in report['events'] if e.get('date') == date or
                (not e.get('owner') and datetime.datetime.fromtimestamp(e['occurred_at'], ZoneInfo('Asia/Seoul')).date().isoformat() == date)]
    amounts = {}
    details = []
    for event in selected:
        owner = event.get('owner') or '귀속 미확인'
        key = (event['purpose'], owner)
        amounts.setdefault(key, []).append(event)
    for (purpose, owner), group in sorted(amounts.items()):
        fields = []
        for field, label in [('total_tokens', '총'), ('input_tokens', '입력'), ('output_tokens', '출력'),
                             ('cached_input_tokens', '캐시 입력'), ('reasoning_output_tokens', '추론 출력')]:
            value = f'{sum(e["counters"][field] for e in group):,}' if all(field in e['counters'] for e in group) else '미확인'
            fields.append(label+' '+value)
        models = ', '.join(sorted({e['model'] if e['model'] != 'unknown' else '미확인' for e in group}))
        requests = str(sum(e['requests'] for e in group)) if all(e['requests'] is not None for e in group) else '미확인'
        details.append(('개발' if purpose == 'development' else '위키 작성')+' · '+owner+' · 모델 '+models+
                       ' · '+', '.join(fields)+' 토큰 · API 요청 '+requests+'회')
    parts = []
    for purpose, label in [('development', '개발'), ('wiki', '위키 작성')]:
        matches = [e for e in selected if e['purpose'] == purpose]
        parts.append(label+(': '+f'{sum(e["counters"]["total_tokens"] for e in matches):,} 토큰' if matches else ': 미수집'))
    missing = sum(not c['usage_available'] for c in report['calls'] if c.get('date') == date)
    text = ' / '.join(parts)+'. 부분 수집이며 커밋 연결 구간 기준이다. 캐시·추론 토큰은 총량에 다시 더하지 않았다.'
    unassigned = sum(e['counters']['total_tokens'] for e in selected if not e.get('owner'))
    if unassigned:
        text += f' 귀속 미확인 {unassigned:,} 토큰을 포함한다.'
    if missing:
        text += f' 사용량을 확인하지 못한 위키 생성 호출 {missing}회가 있다.'
    if report['conflicts']:
        text += ' 수치가 충돌한 요청은 합계에서 제외했다.'
    return text, details
