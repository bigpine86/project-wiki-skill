"""Behavior tests for bounded project editing, with no network or model calls."""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import devlog_wiki as devlog
import project_wiki as editor


class Memory_wiki:
    def __init__(self):
        self.pages = {'프로젝트/테스트': ('사람이 쓴 프로젝트 소개\n', '1')}
        self.saves = 0
        self.fail_title = None
        self.lose_response = False
        self.conflict_once = False

    def Call(self, name, **args):
        if name == 'wiki_search':
            return {'response': 'ok', 'data': [t for t in self.pages if args['keyword'] in t]}
        title = args['title']
        content, revision = self.pages.get(title, ('', '0'))
        if name == 'wiki_read':
            return {'response': 'ok' if revision != '0' else 'not exist', 'data': content, 'revision': revision}
        if name == 'wiki_history':
            return {'response': 'ok', 'data': []}
        if title == self.fail_title:
            raise RuntimeError('offline')
        if self.conflict_once:
            self.conflict_once = False
            self.pages[title] = (content+'동시 사용자 메모\n', str(int(revision)+1))
            return {'response': 'error', 'data': 'edit conflict'}
        if revision != args['expected_revision']:
            return {'response': 'error', 'data': 'edit conflict'}
        self.pages[title] = (args['content'], str(int(revision)+1))
        self.saves += 1
        if self.lose_response:
            self.lose_response = False
            raise RuntimeError('response lost after save')
        return {'response': 'ok'}

    def Close(self):
        pass


class Project_wiki_tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'tmp')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        subprocess.run(['git', 'init', '-q', self.repo], check=True)
        devlog.Git(self.repo, 'config', 'user.name', 'Wiki test')
        devlog.Git(self.repo, 'config', 'user.email', 'wiki@localhost')
        (self.repo/'app.txt').write_text('implemented feature: local wiki\n')
        devlog.Git(self.repo, 'add', 'app.txt')
        devlog.Git(self.repo, 'commit', '-qm', 'Implement local wiki feature')
        self.sha = devlog.Git(self.repo, 'rev-parse', 'HEAD')
        self.config = {'project': '테스트', 'project_id': 'test', 'overview': '프로젝트/테스트',
                       'base_ref': 'HEAD', 'wiki_repo': str(self.repo), 'mode': 'project'}
        self.wiki = Memory_wiki()
        self.record = {'commit': self.sha, 'date': '2026-10-21', 'project': '테스트', 'captured': True,
            'state': 'pending', 'message': 'Implement local wiki feature', 'files': ['app.txt'],
            'checkpoints': [], 'truncated': False, 'sources': [
                {'id': 's1', 'role': 'user', 'text': '위키 기록을 날짜별로 모으기로 했다. 같은 날의 작업을 함께 읽기 위해서다.'}]}
        self.path = devlog.State(self.repo)/'records'/(self.sha+'.json')
        devlog.Write_json(self.path, self.record)
        self.context = editor.Context(self.repo, self.record, self.config, self.wiki)

    def Edit(self, role='daily', title=None, key='recording', text='날짜별 기록을 연결했다. 실행 결과는 미확인이다.'):
        return {'title': title or self.context['daily'], 'role': role, 'key': key,
            'heading': '개발 기록 연결', 'text': text, 'links': ['프로젝트/테스트'],
            'evidence': [{'source': 's1', 'quote': '위키 기록을 날짜별로 모으기로 했다.'}]}

    def Run_edit(self, plan, path=None, preview=False):
        with mock.patch.object(devlog, 'Mcp', return_value=self.wiki), mock.patch.object(editor, 'Generate', return_value=plan):
            return editor.Process(self.repo, path or self.path, self.config, preview=preview)

    def test_same_day_merges_topic_and_preserves_all_provenance(self):
        plan = {'edits': [self.Edit()], 'deferred': []}
        editor.Validate(plan, self.context, self.config)
        self.Run_edit(plan)
        record2 = dict(self.record, commit='b'*40)
        path2 = self.path.parent/('b'*40+'.json')
        devlog.Write_json(path2, record2)
        # Only Context's Git code lookup is stubbed for the second synthetic SHA.
        context2 = editor.Context(self.repo, self.record, self.config, self.wiki)
        with mock.patch.object(editor, 'Context', return_value=context2):
            self.Run_edit({'edits': [self.Edit(text='날짜별 기록을 연결했고 재시도도 보완했다.')], 'deferred': []}, path2)
        body = self.wiki.pages[self.context['daily']][0]
        self.assertEqual(1, body.count('== 개발 기록 연결 =='))
        self.assertIn('재시도도 보완했다', body)
        self.assertIn(self.sha, body)
        self.assertIn('b'*40, body)
        self.assertEqual(1, len([t for t in self.wiki.pages if t.startswith('개발 기록/')]))
        saves = self.wiki.saves
        self.Run_edit(plan, path2)
        self.assertEqual(saves, self.wiki.saves)

    def test_human_edit_is_deferred_other_records_still_saved(self):
        self.Run_edit({'edits': [self.Edit()], 'deferred': []})
        daily = self.context['daily']
        body, revision = self.wiki.pages[daily]
        self.wiki.pages[daily] = (body.replace('날짜별 기록을 연결했다.', '사용자가 다시 쓴 작업 설명.'), str(int(revision)+1))
        record2 = dict(self.record, event_id='followup', commit=self.sha)
        path2 = self.path.parent/'manual-followup.json'
        devlog.Write_json(path2, record2)
        self.Run_edit({'edits': [self.Edit(text='모델의 덮어쓰기 시도')], 'deferred': []}, path2)
        self.assertIn('사용자가 다시 쓴 작업 설명.', self.wiki.pages[daily][0])
        self.assertNotIn('모델의 덮어쓰기 시도', self.wiki.pages[daily][0])
        self.assertIn('followup', self.wiki.pages[daily][0])
        self.assertEqual('review', editor.Read_json(path2, {})['state'])

    def test_response_loss_and_partial_retry_do_not_duplicate(self):
        self.wiki.lose_response = True
        plan = {'edits': [self.Edit()], 'deferred': []}
        self.Run_edit(plan)
        self.assertEqual('pending', editor.Read_json(self.path, {})['state'])
        with mock.patch.object(devlog, 'Mcp', return_value=self.wiki), mock.patch.object(editor, 'Generate') as generate:
            editor.Process(self.repo, self.path, self.config)
            generate.assert_not_called()
        self.assertEqual('done', editor.Read_json(self.path, {})['state'])
        self.assertEqual(1, self.wiki.pages[self.context['daily']][0].count('== 개발 기록 연결 =='))

    def test_concurrent_human_edit_outside_managed_range_survives(self):
        edit = self.Edit(role='overview', title=self.config['overview'])
        edit['evidence'] = [{'source': 'base:0', 'quote': 'implemented feature: local wiki'}]
        self.wiki.conflict_once = True
        self.Run_edit({'edits': [edit], 'deferred': []})
        body = self.wiki.pages[self.config['overview']][0]
        self.assertIn('사람이 쓴 프로젝트 소개', body)
        self.assertIn('동시 사용자 메모', body)

    def test_late_commit_cannot_regress_current_description(self):
        edit = self.Edit(role='overview', title=self.config['overview'])
        edit['evidence'] = [{'source': 'base:0', 'quote': 'implemented feature: local wiki'}]
        operations, deferred = editor.Prepare({'edits': [edit], 'deferred': []}, self.context, self.record, self.config, {}, self.wiki)
        self.record.update(operations=operations, deferred=deferred, base_commit=self.sha)
        devlog.Write_json(self.path, self.record)
        (self.repo/'app.txt').write_text('newer implementation\n')
        devlog.Git(self.repo, 'commit', '-qam', 'New state')
        self.Run_edit({})
        self.assertNotIn('== 개발 기록 연결 ==', self.wiki.pages[self.config['overview']][0])
        self.assertEqual('review', editor.Read_json(self.path, {})['state'])
        self.assertIn(self.context['daily'], self.wiki.pages)

    def test_preview_is_not_an_automatic_publication(self):
        input_file = self.repo/'note.txt'
        input_file.write_text('문서 구조를 날짜별로 모으기로 결정했다.')
        args = argparse.Namespace(id='manual-test', date='2026-10-20', input=input_file, topic=[], file=[])
        path = editor.Manual(self.repo, self.config, args)
        before = copy.deepcopy(self.wiki.pages)
        plan = {'edits': [], 'deferred': []}
        report = self.Run_edit(plan, path, preview=True)
        self.assertTrue(report['changes'])
        self.assertEqual(before, self.wiki.pages)
        self.assertEqual('draft', editor.Read_json(path, {})['state'])
        self.Run_edit(plan, path)
        self.assertEqual(before, self.wiki.pages)
        args.date = '2026-10-22'
        self.assertEqual(path, editor.Manual(self.repo, self.config, args))
        self.assertEqual('2026-10-20', editor.Read_json(path, {})['date'])

    def test_project_lock_keeps_input_pending_and_releases(self):
        with editor.Lock(self.repo, self.config):
            self.Run_edit({'edits': [self.Edit()], 'deferred': []})
            self.assertEqual('pending', editor.Read_json(self.path, {})['state'])
            self.assertEqual(0, self.wiki.saves)
        self.Run_edit({'edits': [self.Edit()], 'deferred': []})
        self.assertEqual('done', editor.Read_json(self.path, {})['state'])

    def test_bounded_failures_retain_pending_input(self):
        self.wiki.fail_title = self.context['daily']
        for _ in range(5):
            self.Run_edit({'edits': [self.Edit()], 'deferred': []})
        value = editor.Read_json(self.path, {})
        self.assertEqual(3, value['attempts'])
        self.assertEqual('pending', value['state'])
        self.assertIn('sources', value)

    def test_existing_foreign_page_is_never_overwritten(self):
        title = self.context['daily']
        self.wiki.pages[title] = ('사용자가 먼저 작성한 다른 문서', '8')
        self.Run_edit({'edits': [self.Edit()], 'deferred': []})
        self.assertEqual(('사용자가 먼저 작성한 다른 문서', '8'), self.wiki.pages[title])
        self.assertEqual('review', editor.Read_json(self.path, {})['state'])

    def test_exact_evidence_and_current_code_gate(self):
        edit = self.Edit()
        edit['evidence'][0]['quote'] = '존재하지 않는 근거 원문입니다'
        with self.assertRaisesRegex(RuntimeError, 'not found'):
            editor.Validate({'edits': [edit], 'deferred': []}, self.context, self.config)
        edit = self.Edit(role='overview', title=self.config['overview'])
        with self.assertRaisesRegex(RuntimeError, 'current code'):
            editor.Validate({'edits': [edit], 'deferred': []}, self.context, self.config)
        edit = self.Edit(role='decision', title='의사결정/테스트/선택')
        edit['evidence'] = [{'source': 'base:0', 'quote': 'implemented feature: local wiki'}]
        with self.assertRaisesRegex(RuntimeError, 'conversation'):
            editor.Validate({'edits': [edit], 'deferred': []}, self.context, self.config)

    def test_plain_text_cannot_inject_namumark(self):
        edit = self.Edit(text='[[위험 링크]]\n[include(틀:삽입)]\n## project-wiki:test:x:end')
        rendered, _, _ = editor.Render(edit, self.record, self.config, {})
        self.assertNotIn('\n## project-wiki:test:x:end', rendered)
        self.assertNotIn('\n[include(', rendered)
        self.assertIn('[[프로젝트/테스트]]', rendered)

    def test_folded_evidence_preserves_prior_sources_and_escapes_closing_markup(self):
        prior = {'provenance': ['이전 기록: original'], 'quotes': ['old: 앞선 선택의 이유를 보존한다.']}
        edit = self.Edit()
        edit['evidence'] = [{'source': 's1', 'quote': '}}}\n[[가짜 링크]]\n{{{#!html <script>악성</script>'}]
        rendered, provenance, quotes = editor.Render(edit, self.record, self.config, prior)
        before, folded = rendered.split('{{{#!folding 근거와 연결 기록 펼치기\n', 1)
        self.assertIn(edit['text'], before)
        self.assertIn('[[프로젝트/테스트]]', before)
        self.assertNotIn('이전 기록: original', before)
        self.assertIn('이전 기록: original', folded)
        self.assertIn('앞선 선택의 이유를 보존한다.', folded)
        self.assertIn(self.sha, folded)
        self.assertEqual(1, rendered.count('\n}}}\n'))
        self.assertNotIn('[[가짜 링크]]', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertEqual(2, len(provenance))
        self.assertEqual(2, len(quotes))

    def test_excerpt_respects_sentence_or_word_boundary_and_marks_omission(self):
        sentence = '하루 일지를 합쳤다.'
        result = editor.Excerpt(sentence+' 뒤의 상세 설명을 계속 이어서 작성한다. 추가 내용.', 25)
        self.assertEqual(sentence+' …', result)
        words = '하나의 작업을 여러 번 확인하면서 기록을 이어가는 아주 긴 설명'
        result = editor.Excerpt(words, 23)
        self.assertLessEqual(len(result), 23)
        self.assertTrue(result.endswith('…'))
        prefix = result[:-1]
        self.assertEqual(' ', words[len(prefix)])
        self.assertEqual('짧은 요약', editor.Excerpt('짧은 요약', 160))

    def test_legacy_commit_links_move_inside_evidence_without_losing_navigation(self):
        edit = self.Edit()
        legacy = ['개발 기록/2026-10-20/테스트-'+'a'*40, '의사결정/테스트-'+'b'*40+'/1']
        edit['links'] += legacy+['기술/Entire', '의사결정/테스트/기록 구조']
        rendered, _, _ = editor.Render(edit, self.record, self.config, {})
        before, folded = rendered.split('{{{#!folding 근거와 연결 기록 펼치기\n', 1)
        for title in legacy:
            self.assertNotIn(title, before)
            self.assertIn('[['+title+']]', folded)
        self.assertIn('[[기술/Entire]]', before)
        self.assertIn('[[의사결정/테스트/기록 구조]]', before)

    def test_recent_diary_uses_short_opening_without_evidence_or_followup_prose(self):
        opening = '기록을 날짜별로 모으고 원래 선택 이유를 보존했다.'
        self.Run_edit({'edits': [self.Edit(text=opening+'\n긴 검증 과정과 내부 실행기 설정을 여기에서 설명한다.')],
                       'deferred': []})
        recent = editor.Block(self.wiki.pages[self.config['overview']][0], self.config, 'recent')
        self.assertIn(opening, recent)
        self.assertNotIn('내부 실행기', recent)
        self.assertNotIn(self.sha, recent)

    def test_day_and_project_titles_are_separate(self):
        next_day = dict(self.record, date='2026-10-22')
        self.assertNotEqual(editor.Daily(self.record, self.config), editor.Daily(next_day, self.config))
        other = dict(self.config, project='다른 프로젝트')
        self.assertNotEqual(editor.Daily(self.record, self.config), editor.Daily(self.record, other))

    def test_new_decision_and_knowledge_are_connected_after_creation(self):
        decision = '의사결정/테스트/기록 구조'
        knowledge = '기술/위키'
        daily = self.Edit()
        daily['links'] += [decision, knowledge]
        edits = [daily, self.Edit('decision', decision), self.Edit('knowledge', knowledge)]
        editor.Validate({'edits': edits, 'deferred': []}, self.context, self.config)
        self.Run_edit({'edits': edits, 'deferred': []})
        self.assertIn('[['+decision+']]', self.wiki.pages[self.context['daily']][0])
        self.assertIn('[['+knowledge+']]', self.wiki.pages[self.context['daily']][0])
        self.assertIn('[[프로젝트/테스트|프로젝트]]', self.wiki.pages[decision][0])


    def test_repeated_decision_evidence_reuses_existing_document(self):
        original = '의사결정/테스트/기록 구조'
        self.Run_edit({'edits': [self.Edit('decision', original)], 'deferred': []})
        record2 = dict(self.record, event_id='repeated')
        path2 = self.path.parent/'manual-repeated.json'
        devlog.Write_json(path2, record2)
        duplicate = '의사결정/테스트/같은 선택의 새 이름'
        daily = self.Edit()
        daily['links'].append(duplicate)
        self.Run_edit({'edits': [daily, self.Edit('decision', duplicate)], 'deferred': []}, path2)
        self.assertNotIn(duplicate, self.wiki.pages)
        self.assertIn('[['+original+']]', self.wiki.pages[self.context['daily']][0])

    def test_recent_days_are_sorted_and_keep_human_overview(self):
        self.Run_edit({'edits': [self.Edit()], 'deferred': []})
        record2 = dict(self.record, date='2026-10-20', event_id='earlier')
        path2 = self.path.parent/'manual-earlier.json'
        devlog.Write_json(path2, record2)
        daily = self.Edit(title=editor.Daily(record2, self.config), text='이전 날짜의 작업도 보존한다.')
        self.Run_edit({'edits': [daily], 'deferred': []}, path2)
        body = self.wiki.pages[self.config['overview']][0]
        self.assertTrue(body.startswith('사람이 쓴 프로젝트 소개'))
        recent = editor.Block(body, self.config, 'recent')
        self.assertLess(recent.index('2026-10-21'), recent.index('2026-10-20'))
        self.assertIn('이전 날짜의 작업도 보존한다.', recent)

    def test_busy_worker_does_not_reset_a_completed_record(self):
        record = dict(self.record, state='done')
        devlog.Write_json(self.path, record)
        with editor.Lock(self.repo, self.config):
            self.Run_edit({'edits': [], 'deferred': []})
        self.assertEqual('done', editor.Read_json(self.path, {})['state'])

    def test_pause_preserves_pending_input_without_model_or_wiki(self):
        with mock.patch.object(devlog, 'Mcp') as client:
            devlog.Process(self.repo, self.path, dict(self.config, mode='paused'))
            client.assert_not_called()
        self.assertEqual('pending', editor.Read_json(self.path, {})['state'])

    def test_crashed_process_lock_can_be_recovered(self):
        root = devlog.State(self.repo)/'project-locks'
        lock = root/'test.lock'
        lock.mkdir(parents=True)
        devlog.Write_json(lock/'owner.json', {'pid': 123456})
        with mock.patch.object(editor.os, 'kill', side_effect=ProcessLookupError):
            with editor.Lock(self.repo, self.config):
                self.assertTrue(lock.exists())
        self.assertFalse(lock.exists())

    def test_project_mode_dispatch_uses_new_editor_once(self):
        with mock.patch.object(editor, 'Process') as process, mock.patch.object(devlog, 'Sync') as legacy:
            devlog.Process(self.repo, self.path, self.config)
            process.assert_called_once_with(self.repo, self.path, self.config)
            legacy.assert_not_called()

    def test_omitted_page_cannot_be_rewritten_from_memory(self):
        self.Run_edit({'edits': [self.Edit()], 'deferred': []})
        context = editor.Context(self.repo, self.record, self.config, self.wiki)
        context['pages'].pop(context['daily'])
        journal = editor.Read_json(editor.Journal_path(self.repo), {})
        operations, deferred = editor.Prepare({'edits': [self.Edit(text='앞선 내용을 읽지 않은 새 원문')], 'deferred': []},
            context, self.record, self.config, journal, self.wiki)
        self.assertTrue(deferred)
        self.assertFalse(any(op['key'] == 'recording' for op in operations))

    def test_register_section_preserves_content_and_recovers_lost_response(self):
        title = self.config['overview']
        body = '소개 문장\n== 현재 상태 ==\n기존 상태 설명\n\n== 다음 작업 ==\n사람의 다음 작업\n'
        self.wiki.pages[title] = (body, '1')
        preview = editor.Adopt(self.repo, self.config, self.wiki, title, '현재 상태', 'current')
        self.assertEqual(body, self.wiki.pages[title][0])
        self.assertEqual('== 현재 상태 ==\n기존 상태 설명\n\n', preview['content'])
        self.wiki.lose_response = True
        with self.assertRaisesRegex(RuntimeError, 'response lost'):
            editor.Adopt(self.repo, self.config, self.wiki, title, '현재 상태', 'current', True)
        saves = self.wiki.saves
        editor.Adopt(self.repo, self.config, self.wiki, title, '현재 상태', 'current', True)
        self.assertEqual(saves, self.wiki.saves)
        stored = self.wiki.pages[title][0]
        start, end = editor.Markers(self.config, 'current')
        self.assertEqual(body, stored.replace(start, '').replace(end, ''))
        self.assertIn(title+'\ncurrent', editor.Read_json(editor.Journal_path(self.repo), {})['blocks'])


if __name__ == '__main__':
    unittest.main()
