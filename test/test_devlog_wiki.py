"""Behavior checks for commit->Entire->wiki publishing, without LLM/network dependencies."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('devlog_wiki', ROOT/'scripts/devlog_wiki.py')
devlog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(devlog)


class Memory_wiki:
    def __init__(self):
        self.pages = {'개발 기록': ('사용자 기존 내용\n', 1), '의사결정': ('기존 결정 목록\n', 1)}
        self.saves = 0
        self.fail_on = None
        self.conflict_once = False

    def Call(self, name, **args):
        title = args['title']
        content, revision = self.pages.get(title, ('', 0))
        if name == 'wiki_read':
            return {'response': 'ok' if revision else 'not exist', 'data': content, 'revision': str(revision)}
        if title == self.fail_on:
            raise RuntimeError('offline')
        if self.conflict_once and title == '개발 기록':
            self.conflict_once = False
            self.pages[title] = (content+'동시 사용자 편집\n', revision+1)
            return {'response': 'error', 'data': 'edit conflict'}
        if args['expected_revision'] != str(revision):
            return {'response': 'error', 'data': 'edit conflict'}
        self.pages[title] = (args['content'], revision+1)
        self.saves += 1
        return {'response': 'ok'}


def Record(sha='a'*40):
    return {'commit': sha, 'date': '2026-10-20', 'message': 'Fix login cache', 'project': '테스트 프로젝트',
            'files': ['login.go'], 'checkpoints': ['0123456789ab'], 'truncated': False,
            'sources': [{'id': 'commit', 'text': 'Fix login cache'},
                        {'id': 's1', 'role': 'user', 'text': '캐시를 분리하기로 결정했다. 엔진 업데이트를 쉽게 하기 위해서다.'}]}


def Summary(decisions=True):
    evidence = [{'source': 's1', 'quote': '캐시를 분리하기로 결정했다.'}]
    return {'what': {'text': '캐시 분리', 'evidence': evidence},
            'why': {'text': '엔진 업데이트를 쉽게 하기 위해', 'evidence': evidence},
            'result': {'text': '대화에서 변경을 보고했으며 실행은 미검증', 'evidence': []},
            'abandoned': {'text': '확인 필요', 'evidence': []},
            'decisions': [{'title': '캐시 분리', 'choice': '캐시 분리', 'why': '엔진 업데이트',
                           'abandoned': '확인 필요', 'evidence': evidence}] if decisions else []}


class Devlog_tests(unittest.TestCase):
    def test_idempotence_preserves_human_edit(self):
        wiki = Memory_wiki()
        title = devlog.Sync(Record(), Summary(), wiki)
        body, revision = wiki.pages[title]
        wiki.pages[title] = (body+'사용자가 고친 설명\n', revision+1)
        count = wiki.saves
        devlog.Sync(Record(), Summary(), wiki)
        self.assertEqual(count, wiki.saves)
        self.assertIn('사용자가 고친 설명', wiki.pages[title][0])
        self.assertIn('[[의사결정/', wiki.pages[title][0])

    def test_partial_write_retry_and_concurrent_index_edit(self):
        wiki = Memory_wiki()
        wiki.fail_on = '개발 기록'
        with self.assertRaises(RuntimeError):
            devlog.Sync(Record(), Summary(), wiki)
        doc_titles = set(wiki.pages)-{'개발 기록', '의사결정'}
        wiki.fail_on = None
        wiki.conflict_once = True
        title = devlog.Sync(Record(), Summary(), wiki)
        self.assertTrue(doc_titles.issubset(wiki.pages))
        self.assertIn('동시 사용자 편집', wiki.pages['개발 기록'][0])
        self.assertEqual(1, wiki.pages['개발 기록'][0].count('[['+title+']]'))

    def test_no_decision_without_conversation_evidence(self):
        summary = Summary()
        summary['decisions'][0]['evidence'] = [{'source': 'commit', 'quote': 'Fix login cache'}]
        with self.assertRaisesRegex(RuntimeError, 'conversation'):
            devlog.Validate(summary, Record())
        summary = Summary()
        summary['why']['evidence'][0]['quote'] = 'made up explanation'
        with self.assertRaisesRegex(RuntimeError, 'not found'):
            devlog.Validate(summary, Record())
        wiki = Memory_wiki()
        devlog.Sync(Record(), Summary(False), wiki)
        self.assertFalse(any(t.startswith('의사결정/') for t in wiki.pages))

    def test_transcript_excludes_internal_and_tool_data(self):
        entries = [
            {'type': 'response_item', 'payload': {'type': 'reasoning', 'content': 'internal'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'developer', 'content': [{'type': 'text', 'text': 'policy'}]}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '분리해줘'}]}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '분리했어'}]}},
            {'type': 'assistant', 'blocks': [{'type': 'text', 'text': 'compact assistant'}, {'type': 'tool_use', 'name': 'Read', 'input': {'secret': 'x'}}]},
        ]
        self.assertEqual(['분리해줘', '분리했어', 'compact assistant'],
                         [m['text'] for m in devlog.Messages('\n'.join(map(json.dumps, entries)))])

    def test_foreign_document_collision_keeps_existing_content(self):
        wiki = Memory_wiki()
        title, _, _ = devlog.Render(Record(), Summary(False))
        wiki.pages[title] = ('someone else wrote this', 7)
        with self.assertRaisesRegex(RuntimeError, 'ownership'):
            devlog.Sync(Record(), Summary(False), wiki)
        self.assertEqual(('someone else wrote this', 7), wiki.pages[title])

    def test_retry_cannot_use_conversation_from_after_commit(self):
        def Message(timestamp, text):
            return json.dumps({'timestamp': timestamp, 'type': 'response_item',
                               'payload': {'type': 'message', 'role': 'user',
                                           'content': [{'type': 'input_text', 'text': text}]}})
        raw = '\n'.join([Message('2026-10-20T10:00:00Z', 'original choice'),
                         Message('2026-10-20T11:00:00Z', 'later unrelated choice')])
        messages = devlog.Messages(raw, devlog.Timestamp('2026-10-20T10:30:00Z'))
        self.assertEqual(['original choice'], [m['text'] for m in messages])

    def test_ui_context_does_not_discard_actual_user_request(self):
        raw = json.dumps({'type': 'user', 'message': {'content': [
            {'type': 'text', 'text': '<in-app-browser-context source="ambient">URL info</in-app-browser-context>\n실제 선택 이유'}]}})
        self.assertEqual('실제 선택 이유', devlog.Messages(raw)[0]['text'])

    def test_failed_worker_keeps_retry_and_generation_cache(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as temp:
            path = Path(temp)/('a'*40+'.json')
            devlog.Write_json(path, dict(Record(), captured=True, summary=Summary(), state='pending'))
            wiki = Memory_wiki()
            wiki.fail_on = '개발 기록'
            wiki.Close = lambda: None
            with mock.patch.object(devlog, 'Mcp', return_value=wiki), mock.patch.object(devlog, 'Generate') as generate:
                devlog.Process(ROOT, path, {})
                self.assertEqual('pending', json.loads(path.read_text())['state'])
                self.assertFalse(path.with_suffix('.lock').exists())
                wiki.fail_on = None
                devlog.Process(ROOT, path, {})
                result = json.loads(path.read_text())
                self.assertEqual('done', result['state'])
                self.assertNotIn('sources', result)
                generate.assert_not_called()

    def test_real_git_hook_enqueues_and_runs_after_existing_hook(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as temp:
            repo = Path(temp)
            subprocess.run(['git', 'init', '-q', repo], check=True)
            for key, value in [('user.name', 'Devlog test'), ('user.email', 'devlog@localhost')]:
                devlog.Git(repo, 'config', key, value)
            hook = repo/'.git/hooks/post-commit'
            hook.write_text('#!/bin/sh\nprintf original >> existing-hook.txt\n')
            hook.chmod(0o755)
            devlog.Install(repo, 'Git hook test', ROOT)
            devlog.Install(repo, 'Git hook test', ROOT)
            self.assertEqual(1, hook.read_text().count('# BEGIN personal-devlog-wiki'))
            config = devlog.Load_config(repo)
            # An unreachable adapter proves a wiki outage cannot abort the commit.
            config['mcp_command'] = ['/usr/bin/false']
            devlog.Write_json(devlog.State(repo)/'config.json', config)
            (repo/'file.txt').write_text('change')
            devlog.Git(repo, 'add', 'file.txt')
            env = {k: v for k, v in os.environ.items() if k != 'CODEX_THREAD_ID'}
            completed = subprocess.run(['git', 'commit', '-qm', 'Hook outage test'], cwd=repo, env=env, timeout=10)
            self.assertEqual(0, completed.returncode)
            sha = devlog.Git(repo, 'rev-parse', 'HEAD')
            path = devlog.State(repo)/'records'/(sha+'.json')
            for _ in range(100):
                if path.exists() and json.loads(path.read_text()).get('error'):
                    break
                time.sleep(.05)
            record = json.loads(path.read_text())
            self.assertEqual('pending', record['state'])
            self.assertTrue(record.get('error'))
            self.assertEqual('original', (repo/'existing-hook.txt').read_text())


if __name__ == '__main__':
    unittest.main()
