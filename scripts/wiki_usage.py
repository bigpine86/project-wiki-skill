"""Local, numeric-only Codex usage ledger. No model calls or wiki credentials."""
import datetime
import hashlib
import json
import os
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
        if prior['counters'] == event['counters'] and prior['purpose'] == event['purpose']:
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
            if prior['counters'] != event['counters'] or prior['purpose'] != event['purpose']:
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
    if not window_path.exists():
        bounds = []
        for path in (devlog.State(repo)/'records').glob('*.json'):
            prior = json.loads(path.read_text())
            if prior.get('observed_at', cutoff) < cutoff and prior.get('session_id') in sessions:
                bounds.append(prior['observed_at'])
        if bounds:
            lower = max(bounds)
        else:
            try:
                lower = devlog.Timestamp(devlog.Git(repo, 'show', '-s', '--format=%cI', record['commit']+'^'))
            except RuntimeError:
                lower = None
        if lower is not None and lower >= cutoff:
            lower = None
        devlog.Write_json(window_path, {'commit': record['commit'], 'date': record['date'],
            'sessions': sessions, 'after': lower, 'before': cutoff,
            'basis': 'observed_commit_window' if bounds else 'parent_commit_time'})
    window = json.loads(window_path.read_text())
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
        starts = [w['after'] for p in (root/'windows').glob('*.json')
                  if session_id in (w := json.loads(p.read_text()))['sessions'] and w['after'] is not None]
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
    if window['after'] is None:
        issues.append('interval_start_unavailable')
    devlog.Write_json(root/'observations'/(record['commit']+'.json'),
                     {'commit': record['commit'], 'checked_at': upper, 'status': 'partial', 'issues': sorted(set(issues))})


def Cli_usage(path, repo, owner, date, invocation, occurred_at):
    """CLI turn totals do not expose individual API request count or guaranteed model."""
    count = 0
    if path.exists():
        with path.open() as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get('type') != 'turn.completed':
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
                      'usage_available': count > 0, 'invocation': invocation})


def Report(repo):
    root = Root(repo)
    windows = [json.loads(p.read_text()) for p in (root/'windows').glob('*.json')]
    events = []
    for path in sorted((root/'events').glob('*.json')):
        if (root/'conflicts'/path.name).exists():
            continue
        event = json.loads(path.read_text())
        if event['purpose'] == 'development':
            eligible = [w for w in windows if event['session_id'] in w['sessions'] and w['after'] is not None
                        and w['after'] < event['occurred_at'] <= w['before']
                        and event.get('started_at') is not None and w['after'] < event['started_at'] <= event['occurred_at']]
            # Multiple overlapping windows are ambiguous, including branches and amended commits.
            event['owner'] = eligible[0]['commit'] if len(eligible) == 1 else None
            event['date'] = eligible[0]['date'] if len(eligible) == 1 else None
        events.append(event)
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
    calls = [json.loads(p.read_text()) for p in (root/'calls').glob('*.json')]
    observations = [json.loads(p.read_text()) for p in (root/'observations').glob('*.json')]
    return {'status': 'partial', 'timezone': 'Asia/Seoul', 'totals': totals, 'events': events,
            'calls': calls, 'observations': observations,
            'conflicts': len(list((root/'conflicts').glob('*.json')))}


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
