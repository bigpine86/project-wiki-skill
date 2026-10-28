"""Usage accounting invariants; synthetic logs only, no network or paid calls."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import wiki_usage as usage
import project_wiki as editor
import test_project_wiki as project_tests


class Usage_tests(unittest.TestCase):
    def setUp(self):
        self.fixture = project_tests.Project_wiki_tests('test_same_day_merges_topic_and_preserves_all_provenance')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.repo
        self.sid = '11111111-1111-1111-1111-111111111111'
        self.root = usage.Root(self.repo)
        self.sha = self.fixture.sha
        self.record = dict(self.fixture.record, session_id=self.sid, observed_at=200)
        self.Window(self.sha, 100, 200)
        self.log = self.repo/'session.jsonl'

    def Window(self, sha, lower, upper):
        editor.devlog.Write_json(self.root/'windows'/(sha+'.json'),
            {'commit': sha, 'date': self.record['date'] if hasattr(self, 'record') else '2026-10-21',
             'sessions': [self.sid], 'after': lower, 'before': upper, 'basis': 'test'})

    def Lines(self, response='r1', start=110, end=150, model='model-a', value=None):
        return [
            {'timestamp': 1, 'type': 'session_meta', 'payload': {'id': self.sid}},
            {'timestamp': start, 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 't'+response}},
            {'timestamp': start+1, 'type': 'turn_context', 'payload': {'turn_id': 't'+response, 'model': model, 'cwd': str(self.repo)}},
            {'timestamp': end, 'type': 'token_usage_record', 'payload': {'session_id': self.sid,
                'turn_id': 't'+response, 'response_id': response, 'usage': value or
                {'input_tokens': 100, 'cached_input_tokens': 80, 'output_tokens': 20,
                 'reasoning_output_tokens': 10, 'total_tokens': 120}}}]

    def Collect(self, lines, record=None, now=None):
        self.log.write_text('\n'.join(json.dumps(v) for v in lines)+'\n')
        with mock.patch.object(usage, 'Find_session', return_value=self.log):
            usage.Collect(self.repo, record or self.record, now=now)
        return usage.Report(self.repo)

    def test_request_dedup_and_subset_counters(self):
        lines = self.Lines()
        lines += [lines[-1], {'timestamp': 180, 'type': 'event_msg', 'payload':
                  {'type': 'token_count', 'info': {'total_token_usage': {'total_tokens': 999999}}}}]
        report = self.Collect(lines)
        report = self.Collect(lines)
        self.assertEqual(1, len(report['events']))
        totals = report['totals']['project']['development']
        self.assertEqual((120, 100, 20, 80, 10, 1), tuple(totals[k] for k in
                         ('tokens', 'input', 'output', 'cached_input', 'reasoning_output', 'requests')))
        self.assertEqual(self.sha, report['events'][0]['owner'])

    def test_commit_boundary_late_arrival_and_overlap(self):
        self.Collect(self.Lines())
        second = 'b'*40
        self.Window(second, 200, 300)
        lines = self.Lines()+self.Lines('late', 190, 220)+self.Lines('next', 210, 240)
        # Appended late, but timestamp belongs to the previous window.
        lines += self.Lines('delayed', 160, 180)
        report = self.Collect(lines, dict(self.record, commit=second, observed_at=300))
        owners = {e['id']: e['owner'] for e in report['events']}
        self.assertIsNone(owners['codex:late'])
        self.assertEqual(second, owners['codex:next'])
        self.assertEqual(self.sha, owners['codex:delayed'])
        self.Window('c'*40, 100, 200)  # amended/overlapping commit must not double count
        report = usage.Report(self.repo)
        self.assertEqual(480, report['totals']['project']['development']['tokens'])
        self.assertIsNone(next(e for e in report['events'] if e['id']=='codex:r1')['owner'])

    def test_model_change_keeps_request_time_model(self):
        lines = self.Lines()
        changed = self.Lines('r2', 110, 160, model='model-b')[2:]
        for item in changed:
            item['payload']['turn_id'] = 'tr1'
        report = self.Collect(lines+changed)
        self.assertEqual(120, report['totals']['model:model-a']['development']['tokens'])
        self.assertEqual(120, report['totals']['model:model-b']['development']['tokens'])

    def test_cumulative_reset_unknown_baseline_and_corruption_are_not_zero(self):
        self.Window(self.sha, None, 200)
        report = self.Collect(self.Lines())
        self.assertFalse(report['events'])
        self.assertIn('interval_start_unavailable', report['observations'][0]['issues'])
        text, _ = usage.Daily_summary(report, self.record['date'])
        self.assertIn('개발: 미수집', text)
        raw = [json.dumps({'timestamp': 2, 'type': 'session_meta', 'payload': {'id': self.sid}}), '{broken']
        for n in (500, 100):
            raw.append(json.dumps({'timestamp': 10, 'type': 'event_msg', 'payload':
                {'type': 'token_count', 'info': {'total_token_usage': {'total_tokens': n}}}}))
        events, issues = usage.Events(raw, self.sid, self.repo)
        self.assertFalse(events)
        self.assertIn('malformed_event', issues)

    def test_foreign_session_and_workspace_are_not_imported(self):
        lines = self.Lines()
        lines[0]['payload']['id'] = 'other-session'
        self.assertFalse(self.Collect(lines)['events'])
        lines = self.Lines()
        lines[2]['payload']['cwd'] = str(self.repo/'elsewhere')
        self.assertFalse(self.Collect(lines)['events'])

    def test_invalid_or_conflicting_counts_are_excluded(self):
        self.assertIsNone(usage.Counters({'input_tokens': True, 'output_tokens': 1}))
        self.assertIsNone(usage.Counters({'input_tokens': 2, 'output_tokens': 1, 'cached_input_tokens': 3}))
        self.assertIsNone(usage.Counters({'input_tokens': 2, 'output_tokens': 1, 'total_tokens': 9}))
        self.Collect(self.Lines())
        report = self.Collect(self.Lines(value={'input_tokens': 101, 'output_tokens': 20}))
        self.assertFalse(report['events'])
        self.assertEqual(1, report['conflicts'])

    def test_cli_invocations_retry_missing_usage_and_models(self):
        self.log.write_text(json.dumps({'type': 'turn.completed', 'usage':
            {'input_tokens': 100, 'cached_input_tokens': 80, 'output_tokens': 5}})+'\n')
        for invocation in ('attempt-1', 'attempt-1', 'attempt-2'):
            usage.Cli_usage(self.log, self.repo, self.sha, self.record['date'], invocation, 160)
        self.log.write_text('{"type":"turn.failed"}\n')
        usage.Cli_usage(self.log, self.repo, self.sha, self.record['date'], 'attempt-3', 170)
        report = usage.Report(self.repo)
        self.assertEqual(210, report['totals']['project']['wiki']['tokens'])
        self.assertIsNone(report['totals']['project']['wiki']['requests'])
        self.assertIn('model:unknown', report['totals'])
        self.assertEqual(3, len(report['calls']))
        self.assertIn('확인하지 못한', usage.Daily_summary(report, self.record['date'])[0])

    def test_usage_block_saves_without_model_and_preserves_human_edits(self):
        self.Collect(self.Lines())
        fixture = self.fixture
        with mock.patch.object(editor.devlog, 'Mcp', return_value=fixture.wiki), mock.patch.object(editor, 'Generate') as generate:
            result = editor.Publish_usage(self.repo, fixture.config, self.record['date'])
            self.assertEqual('done', result['state'])
            title = result['title']
            original, revision = fixture.wiki.pages[title]
            self.assertIn('120 토큰', original)
            self.assertIn('캐시 입력 80', original)
            self.assertIn('모델 model-a', original)
            self.assertNotIn('session.jsonl', original)
            count = fixture.wiki.saves
            editor.Publish_usage(self.repo, fixture.config, self.record['date'])
            self.assertEqual(count, fixture.wiki.saves)
            fixture.wiki.pages[title] = (original.replace('120 토큰', '사용자 확인 메모'), str(int(revision)+1))
            result = editor.Publish_usage(self.repo, fixture.config, self.record['date'])
            self.assertEqual('review', result['state'])
            self.assertIn('사용자 확인 메모', fixture.wiki.pages[title][0])
            generate.assert_not_called()

    def test_generation_stdout_collects_usage_without_an_extra_model(self):
        config = {'codex': 'fake-codex'}
        def Run(args, **kwargs):
            Path(args[args.index('-o')+1]).write_text('{"edits":[],"deferred":[]}')
            kwargs['stdout'].write(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':2}})+'\n')
            return type('Result', (), {'returncode': 0})()
        state = self.repo/'model-state'
        state.mkdir()
        with mock.patch.object(usage, 'Root', return_value=self.root), mock.patch.object(editor.devlog, 'Run', return_value=''), mock.patch.object(editor.devlog.subprocess, 'run', side_effect=Run) as run:
            result = editor.devlog.Model_json('synthetic', {}, config, state,
                {'repo': self.repo, 'owner': self.sha, 'date': self.record['date']})
        self.assertEqual([], result['edits'])
        self.assertEqual(1, run.call_count)
        self.assertEqual(12, usage.Report(self.repo)['totals']['project']['wiki']['tokens'])
        self.assertFalse(list(state.iterdir()))

    def test_large_managed_usage_does_not_crowd_out_diary_evidence(self):
        fixture = self.fixture
        title = editor.Daily(self.record, fixture.config)
        start, end = editor.Markers(fixture.config, 'ai-usage')
        usage_block = start+'== AI 사용량 ==\n'+('숫자 전용 요청 상세 123456789\n'*1500)+end
        prose = '작업 목적과 실패 이유. 사람의 측정 결과 42토큰.\n'
        original = prose+usage_block+'[[기술/확인한 근거]]\n'
        fixture.wiki.pages[title] = (original, '3')
        editor.devlog.Write_json(editor.Journal_path(self.repo),
            {'blocks': {title+'\nai-usage': {'content': usage_block}}})
        self.assertGreater(len(original), 24000)
        context = editor.Context(self.repo, self.record, fixture.config, fixture.wiki)
        self.assertNotIn(title, context['omitted'])
        self.assertEqual(prose+'[[기술/확인한 근거]]\n', context['pages'][title]['content'])
        self.assertEqual(['ai-usage'], context['pages'][title]['excluded_blocks'])
        self.assertEqual('3', context['pages'][title]['revision'])
        self.assertIn('기술/확인한 근거', context['pages'])
        self.assertEqual(fixture.record['sources'][0], context['sources'][0])
        with mock.patch.object(editor.devlog, 'Model_json', return_value={'edits': [], 'deferred': []}) as model:
            editor.Generate(context, self.record, fixture.config, self.root)
        self.assertNotIn('숫자 전용 요청 상세', model.call_args.args[0])
        self.assertIn('사람의 측정 결과 42토큰', model.call_args.args[0])
        self.assertEqual((original, '3'), fixture.wiki.pages[title])

    def test_unverified_usage_and_human_edits_remain_in_context(self):
        fixture = self.fixture
        title = editor.Daily(self.record, fixture.config)
        start, end = editor.Markers(fixture.config, 'ai-usage')
        original = start+'수집량 100토큰\n'+end
        other_start, other_end = editor.Markers(dict(fixture.config, project_id='another'), 'ai-usage')
        for label, content, baseline in [
            ('human edit', original.replace('100토큰', '수정 이유와 200토큰'), original),
            ('unknown origin', original, None),
            ('ambiguous markers', original+original, original),
            ('other project', other_start+'다른 프로젝트 근거\n'+other_end, original),
        ]:
            with self.subTest(label=label):
                fixture.wiki.pages[title] = (content, '4')
                editor.devlog.Write_json(editor.Journal_path(self.repo),
                    {'blocks': {title+'\nai-usage': {'content': baseline}}})
                context = editor.Context(self.repo, self.record, fixture.config, fixture.wiki)
                self.assertEqual(content, context['pages'][title]['content'])
                self.assertNotIn('excluded_blocks', context['pages'][title])

    def test_each_session_uses_its_previous_boundary_and_repairs_legacy_window(self):
        sid_b = '22222222-2222-2222-2222-222222222222'
        for sha, sid, observed in [('a'*40, self.sid, 180), ('b'*40, sid_b, 120)]:
            editor.devlog.Write_json(editor.devlog.State(self.repo)/'records'/(sha+'.json'),
                                    {'commit': sha, 'usage_sessions': [sid], 'observed_at': observed})
        record = dict(self.record, usage_sessions=[self.sid, sid_b])
        editor.devlog.Write_json(self.root/'windows'/(self.sha+'.json'),
            {'commit': self.sha, 'date': record['date'], 'sessions': [self.sid, sid_b],
             'after': 180, 'before': 200, 'basis': 'observed_commit_window'})
        self.sid = sid_b
        self.log.write_text('\n'.join(json.dumps(v) for v in self.Lines(start=130, end=150)))
        with mock.patch.object(usage, 'Find_session', side_effect=lambda sid: self.log if sid == sid_b else None):
            usage.Collect(self.repo, record)
        window = json.loads((self.root/'windows'/(self.sha+'.json')).read_text())
        self.assertEqual([120, 180], sorted(window['session_after'].values()))
        self.assertEqual(self.sha, usage.Report(self.repo)['events'][0]['owner'])

    def test_backup_restore_recollection_and_tamper_guard(self):
        import backup_local
        import shutil
        import subprocess
        self.Collect(self.Lines())
        state = editor.devlog.State(self.repo)
        snapshot = self.repo/'backup'
        lock = state/'worker.lock'
        lock.mkdir()
        with self.assertRaises(RuntimeError):
            backup_local.Backup_state(self.repo, snapshot)
        lock.rmdir()
        expected = usage.Report(self.repo)
        backup_local.Backup_state(self.repo, snapshot)
        with self.assertRaises(RuntimeError):
            backup_local.Restore_state(snapshot, self.repo)
        restored_repo = self.repo/'restored'
        subprocess.run(['git', 'init', '-q', restored_repo], check=True)
        backup_local.Restore_state(snapshot, restored_repo)
        self.assertEqual(expected, usage.Report(restored_repo))
        shutil.rmtree(state)
        backup_local.Restore_state(snapshot, self.repo)
        self.assertEqual(expected['totals'], self.Collect(self.Lines())['totals'])
        manifest = json.loads((snapshot/'state-manifest.json').read_text())
        (snapshot/next(iter(manifest['sha256']))).write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            backup_local.Restore_state(snapshot, restored_repo)

    def test_outcomes_and_same_id_metadata_conflict(self):
        self.Collect(self.Lines())
        report = self.Collect(self.Lines(model='different-model'))
        self.assertEqual(1, report['conflicts'])
        self.assertFalse(report['events'])
        self.log.write_text('[]\n{"type":"turn.failed"}\n')
        for outcome in ('failed', 'timeout'):
            usage.Cli_usage(self.log, self.repo, self.sha, self.record['date'], outcome, 170,
                            outcome=outcome, mode='preview', attempt=2)
        calls = usage.Report(self.repo)['calls']
        self.assertEqual({'failed', 'timeout'}, {c['outcome'] for c in calls})
        self.assertTrue(all(c['attempt'] == 2 and c['mode'] == 'preview' and not c['usage_available'] for c in calls))

    def test_late_usage_refreshes_existing_previous_day_without_model(self):
        fixture = self.fixture
        with mock.patch.object(editor.devlog, 'Mcp', return_value=fixture.wiki), mock.patch.object(editor, 'Generate') as generate:
            first = editor.Publish_usage(self.repo, fixture.config, self.record['date'])
            self.Collect(self.Lines())
            editor.Publish_usage(self.repo, fixture.config, '2026-10-22')
            self.assertIn('120 토큰', fixture.wiki.pages[first['title']][0])
            count = fixture.wiki.saves
            editor.Publish_usage(self.repo, fixture.config, '2026-10-22')
            self.assertEqual(count, fixture.wiki.saves)
            generate.assert_not_called()

    def test_sync_lost_response_retries_without_counting_twice(self):
        self.Collect(self.Lines())
        class Central:
            def __init__(self):
                self.saved = {}
                self.lose = True
            def Call(self, tool_name, **args):
                if tool_name == 'wiki_usage_sync':
                    for record in args['records']:
                        self.saved[json.dumps(record, sort_keys=True)] = record
                    if self.lose:
                        self.lose = False
                        raise RuntimeError('response lost')
                    return {'response': 'ok'}
                return {'response': 'ok', 'records': list(self.saved.values()), 'cursor': '', 'revision': '1'}
        central = Central()
        with self.assertRaises(RuntimeError):
            usage.Sync(self.repo, self.fixture.config, central)
        self.assertEqual('pending', json.loads((self.root/'sync.json').read_text())['state'])
        report = usage.Sync(self.repo, self.fixture.config, central)
        self.assertEqual(120, report['totals']['project']['development']['tokens'])
        self.assertEqual('done', json.loads((self.root/'sync.json').read_text())['state'])

    def test_generation_is_reused_when_central_sync_fails(self):
        fixture = self.fixture
        config = dict(fixture.config, usage_central=True)
        plan = {'edits': [fixture.Edit()], 'deferred': []}
        report = dict(usage.Report(self.repo), storage='central', revision='1')
        with mock.patch.object(editor.devlog, 'Mcp', return_value=fixture.wiki), \
             mock.patch.object(editor, 'Generate', return_value=plan) as generate, \
             mock.patch.object(usage, 'Sync', side_effect=[RuntimeError('offline'), report]):
            editor.Process(self.repo, fixture.path, config)
            self.assertIn('generated_plan', json.loads(fixture.path.read_text()))
            editor.Process(self.repo, fixture.path, config)
            self.assertEqual(1, generate.call_count)
            self.assertEqual('done', json.loads(fixture.path.read_text())['state'])
