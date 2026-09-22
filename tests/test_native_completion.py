"""Native tool contract: durable declarations, never fabricated observations."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.tools.todo_tool import omh_todo_handler


class NativeCompletionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'omh'
        self.env = patch.dict(os.environ, {'OMH_HOME': str(self.home),
                                          'HERMES_HOME': str(self.root / 'hermes')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cwd = patch('omh.plugin_bundle.omh.runtime_paths.runtime_cwd', return_value=self.root)
        self.cwd.start()
        self.addCleanup(self.cwd.stop)

    def call(self, action, session='parent', **args):
        return json.loads(omh_todo_handler({'action': action, **args}, session_id=session))

    def capture(self):
        self.assertEqual(self.call('set', items=[{'text': text, 'state': 'active' if i == 0 else 'pending'}
                         for i, text in enumerate(['Parser fix', 'Regression test', 'Documentation'])])['status'], 'written')
        result = self.call('checkpoint', accepted=True, rejected=['New execution engine'],
                           revision='rev-a', environment='offline-fixture')
        self.assertEqual(result['status'], 'written', result)
        return result['checkpoint']['checkpoint_id']

    def test_accepted_scope_survives_session_and_todo_clear(self):
        key = self.capture()
        self.call('clear')
        result = self.call('recall', session='later', checkpoint_id=key,
                           revision='rev-a', environment='offline-fixture')
        self.assertEqual(result['status'], 'current', result)
        self.assertEqual([i['text'] for i in result['checkpoint']['items']],
                         ['Parser fix', 'Regression test', 'Documentation'])
        self.assertEqual(result['checkpoint']['rejected'], ['New execution engine'])
        self.assertEqual(result['completion']['status'], 'not_verified')
        self.assertEqual(result['completion']['missing'], [1, 2, 3])
        self.assertEqual(result['sources']['review'], 'absent')

    def test_review_empty_is_not_missing_and_pass_is_not_proof(self):
        key = self.capture()
        for kind in ('verification', 'review', 'qa'):
            result = self.call('record', checkpoint_id=key, revision='rev-a',
                               environment='offline-fixture', result={
                                   'kind': kind, 'item': 1, 'verdict': 'PASS',
                                   'summary': 'Check reported clean', 'findings': [],
                                   'claimed_source': 'independent_review',
                                   'claimed_evidence_state': 'observed', 'references': []})
            self.assertEqual(result['status'], 'written', result)
        read = self.call('recall', session='later', checkpoint_id=key,
                         revision='rev-a', environment='offline-fixture')
        self.assertEqual(read['sources']['review'], 'declared_no_findings')
        self.assertEqual(read['completion']['status'], 'not_verified')
        self.assertEqual(read['completion']['missing'], [2, 3])
        for record in read['checkpoint']['results']:
            self.assertEqual(record['standing'], 'model_declaration')
            self.assertEqual(record['claimed_evidence_state'], 'observed')
            self.assertFalse(record['observed'])


    def row(self, **changes):
        return {'kind': 'verification', 'item': 1, 'verdict': 'PASS',
                'summary': 'Tests returned exit zero', 'findings': [],
                'claimed_source': 'host_exit', 'claimed_evidence_state': 'observed',
                'references': [], **changes}

    def record(self, key, **changes):
        return self.call('record', checkpoint_id=key, revision='rev-a',
                         environment='offline-fixture', result=self.row(**changes))

    def recall(self, key, **changes):
        return self.call('recall', checkpoint_id=key, **{
            'revision': 'rev-a', 'environment': 'offline-fixture', **changes})

    def test_resume_is_explicit_and_does_not_reopen_rejected_scope(self):
        key = self.capture()
        read = self.recall(key, session='later')
        self.assertNotEqual(self.call('show', session='later')['todo']['status'], 'established')
        self.call('set', session='later', items=read['checkpoint']['items'])
        later = self.call('show', session='later')['todo']
        self.assertEqual(later['counts']['total'], 3)
        self.assertEqual(later['template'], '')
        self.assertNotIn('New execution engine', json.dumps(later))
        self.assertEqual(self.call('show')['todo']['counts']['total'], 3)

    def test_current_missing_stale_and_malformed_are_distinct(self):
        key = self.capture()
        self.assertEqual(self.recall('a' * 32)['status'], 'absent')
        self.record(key)
        for changes in ({'revision': 'rev-b'}, {'environment': 'other'}):
            read = self.recall(key, **changes)
            self.assertEqual(read['status'], 'stale')
            self.assertEqual(read['sources']['verification'], 'stale')
            self.assertEqual(read['completion']['missing'], [1, 2, 3])
        path = self.home / 'runtime/completion/records.json'
        path.write_text('{broken')
        self.assertEqual(self.recall(key)['status'], 'malformed')
        self.assertEqual(self.record(key)['status'], 'invalid_completion')
        self.assertEqual(path.read_text(), '{broken')

    def test_profile_project_and_session_ownership(self):
        key = self.capture()
        self.assertEqual(self.call('checkpoint', session='stranger', accepted=True,
                         revision='rev-a', environment='offline-fixture')['status'], 'invalid_completion')
        with patch.dict(os.environ, {'HERMES_HOME': str(self.root / 'foreign')}):
            self.assertEqual(self.recall(key)['status'], 'absent')
            self.assertEqual(self.record(key)['status'], 'absent')
            self.assertEqual(self.call('recall')['checkpoints'], [])
        with patch('omh.plugin_bundle.omh.runtime_paths.runtime_cwd', return_value=self.root / 'foreign'):
            self.assertEqual(self.recall(key)['status'], 'absent')
        self.assertEqual(self.call('recall', session='')['status'], 'malformed')
        self.assertEqual(self.call('recall', omh_home=str(self.root))['status'], 'invalid_completion')
        self.assertEqual(self.call('recall', project_root=str(self.root))['status'], 'invalid_completion')

    def test_project_subdirectories_share_scope_but_other_checkouts_do_not(self):
        (self.root / '.git').mkdir()
        subdir = self.root / 'src'
        subdir.mkdir()
        key = self.capture()
        with patch('omh.plugin_bundle.omh.runtime_paths.runtime_cwd', return_value=subdir):
            self.assertEqual(self.recall(key)['status'], 'current')
        other = self.root / 'other-checkout'
        (other / '.git').mkdir(parents=True)
        with patch('omh.plugin_bundle.omh.runtime_paths.runtime_cwd', return_value=other):
            self.assertEqual(self.recall(key)['status'], 'absent')

    def test_no_provenance_laundering_or_out_of_scope_item(self):
        key = self.capture()
        for source in ('model', 'host_exit', 'independent_review', 'ci'):
            result = self.record(key, claimed_source=source)
            row = result['checkpoint']['results'][-1]
            self.assertEqual(row['claimed_source'], source)
            self.assertEqual(row['standing'], 'model_declaration')
            self.assertIs(row['observed'], False)
        for changes in ({'observed': True}, {'standing': 'observed'}, {'item': 4},
                        {'item': True}, {'verdict': 'PASS trailing'}, {'raw_output': 'log'},
                        {'summary': 'x' * 201}, {'summary': 'a\nb'}, {'kind': []}):
            self.assertEqual(self.record(key, **changes)['status'], 'invalid_completion', changes)
        self.assertEqual(len(self.recall(key)['checkpoint']['results']), 4)

    def test_receipt_references_are_opaque_not_reissued_or_validated(self):
        key = self.capture()
        ref = {'type': 'verification_receipt/v1', 'id': 'c' * 64}
        self.assertEqual(self.record(key, references=[ref])['status'], 'written')
        row = self.recall(key)['checkpoint']['results'][0]
        self.assertEqual(row['references'], [ref])
        self.assertFalse(row['observed'])
        self.assertEqual(self.record(key, references=[{**ref, 'id': 'C' * 64}])['status'], 'invalid_completion')
        self.assertFalse((self.home / 'coding/verification-receipts').exists())

    def test_binding_identifiers_are_never_collapsed_by_redaction(self):
        key = self.capture()
        secret = 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456'
        self.assertEqual(self.call('record', checkpoint_id=key, revision=secret,
                         environment='offline-fixture', result=self.row())['status'], 'invalid_completion')
        self.assertEqual(self.call('record', checkpoint_id=key, revision='rev-a',
                         environment=secret, result=self.row())['status'], 'invalid_completion')
        self.assertEqual(self.recall(key, revision='rev-a ')['status'], 'stale')
        self.assertEqual(self.recall(key)['checkpoint']['revision'], 'rev-a')
        self.assertEqual(self.recall(key)['checkpoint']['results'], [])
        revision = 'd7271b208038895f50c74e9774616b1a0a26a0dd'
        stored = self.call('record', checkpoint_id=key, revision=revision,
                           environment='f' * 64, result=self.row())
        self.assertEqual(stored['status'], 'written', stored)
        self.assertEqual(stored['checkpoint']['results'][0]['revision'], revision)

    def test_complete_declarations_never_mean_verified_and_blockers_survive(self):
        key = self.capture()
        for item in (1, 2, 3):
            self.record(key, item=item)
        result = self.recall(key)['completion']
        self.assertTrue(result['declarations_complete'])
        self.assertEqual(result['status'], 'not_verified')
        self.record(key, item=2, kind='review', verdict='BLOCK', findings=['Missing error check'])
        self.assertEqual(self.recall(key)['completion']['blockers'], [2])
        self.record(key, item=2, kind='review', verdict='PASS')
        read = self.recall(key)
        self.assertTrue(read['completion']['declarations_complete'])
        self.assertEqual(len(read['checkpoint']['results']), 5)
        self.assertEqual(read['checkpoint']['results'][3]['findings'], ['Missing error check'])

    def test_expiry_keeps_history_and_never_reclassifies_it_clean(self):
        from omh.plugin_bundle.omh import completion_store as store
        key = self.capture()
        self.record(key)
        with patch.object(store.time, 'time', return_value=store.time.time() + store.STALE_SECONDS + 1):
            read = self.recall(key)
        self.assertEqual(read['status'], 'stale')
        self.assertEqual(read['sources']['verification'], 'stale')
        self.assertEqual(len(read['checkpoint']['results']), 1)

    def test_symlinks_fail_closed_without_touching_target(self):
        key = self.capture()
        path = self.home / 'runtime/completion/records.json'
        target = self.root / 'untouched.json'
        target.write_text(path.read_text())
        path.unlink()
        try:
            path.symlink_to(target)
        except OSError as error:
            self.skipTest(f"Symlink creation unavailable: {error}")
        before = target.read_bytes()
        self.assertEqual(self.recall(key)['status'], 'malformed')
        self.assertEqual(self.record(key)['status'], 'invalid_completion')
        self.assertEqual(target.read_bytes(), before)

    def test_redaction_at_write_and_read_and_private_permissions(self):
        key = self.capture()
        secret = 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456'
        self.assertEqual(self.record(key, summary=secret, findings=[secret])['status'], 'written')
        path = self.home / 'runtime/completion/records.json'
        self.assertNotIn(secret, path.read_text())
        if os.name == 'posix':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        data = json.loads(path.read_text())
        data['checkpoints'][0]['results'][0]['summary'] = secret
        path.write_text(json.dumps(data))
        self.assertNotIn(secret, json.dumps(self.recall(key)))

    def test_bounded_capacity_refuses_without_evicting_history(self):
        from omh.plugin_bundle.omh import completion_store as store
        key = self.capture()
        self.record(key)
        path = self.home / 'runtime/completion/records.json'
        before = path.read_bytes()
        with patch.object(store, 'MAX_RESULTS', 1):
            self.assertEqual(self.record(key)['status'], 'invalid_completion')
        with patch.object(store, 'MAX_CHECKPOINTS', 1):
            self.assertEqual(self.call('checkpoint', accepted=True, revision='rev-a',
                             environment='offline-fixture')['status'], 'invalid_completion')
        self.assertEqual(path.read_bytes(), before)

    def test_tampered_observation_is_malformed_not_evidence(self):
        key = self.capture()
        self.record(key)
        path = self.home / 'runtime/completion/records.json'
        data = json.loads(path.read_text())
        data['checkpoints'][0]['results'][0]['observed'] = True
        path.write_text(json.dumps(data))
        self.assertEqual(self.recall(key)['status'], 'malformed')

    def test_new_process_can_read_prior_session_declarations(self):
        import subprocess
        import sys
        key = self.capture()
        self.record(key, kind='review', findings=['Review found a missing guard'])
        args = {'action': 'recall', 'checkpoint_id': key, 'revision': 'rev-a', 'environment': 'offline-fixture'}
        program = ('import json,socket,os; '
                   'homes={k:os.environ[k] for k in ("OMH_HOME","HERMES_HOME")}; '
                   'socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError("network")); '
                   'from _local_package import load_local_package; load_local_package(); os.environ.update(homes); '
                   'from omh.plugin_bundle.omh.tools.todo_tool import omh_todo_handler; '
                   f'print(omh_todo_handler({args!r}, session_id="new-process"))')
        result = subprocess.run([sys.executable, '-c', program], cwd=self.root,
            env={**{name: os.environ[name] for name in ('PATH', 'OMH_HOME', 'HERMES_HOME')},
                 'HOME': str(self.root), 'USERPROFILE': str(self.root),
                 **{name: os.environ[name] for name in
                    ('TMPDIR', 'TMP', 'TEMP', 'SYSTEMROOT', 'WINDIR', 'COMSPEC') if name in os.environ},
                 'PYTHONPATH': str(Path(__file__).resolve().parent)},
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        read = json.loads(result.stdout)
        self.assertEqual(read['status'], 'current')
        self.assertEqual(read['sources']['review'], 'declared_findings')
        self.assertEqual(read['checkpoint']['results'][0]['findings'], ['Review found a missing guard'])
        self.assertEqual(read['completion']['status'], 'not_verified')

    def test_new_process_without_home_environment(self):
        with patch.dict(os.environ):
            os.environ.pop('HOME', None)
            self.test_new_process_can_read_prior_session_declarations()

    def test_concurrent_appends_preserve_every_attributed_result(self):
        from concurrent.futures import ThreadPoolExecutor
        key = self.capture()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: self.record(key, summary=f'Result {i}'), range(8)))
        self.assertTrue(all(r['status'] == 'written' for r in results), results)
        rows = self.recall(key)['checkpoint']['results']
        self.assertEqual(sorted(r['summary'] for r in rows), [f'Result {i}' for i in range(8)])

    def test_stale_todo_timestamp_cannot_be_refreshed_by_touching_file(self):
        from omh.plugin_bundle.omh.todo_store import todo_path
        self.capture()
        path = todo_path(self.home, 'parent')
        data = json.loads(path.read_text())
        data['updated_at'] = '2000-01-01T00:00:00Z'
        path.write_text(json.dumps(data))
        self.assertEqual(self.call('checkpoint', accepted=True, revision='rev-a',
                         environment='offline-fixture')['status'], 'invalid_completion')

    def test_persisted_root_and_scope_are_strict_not_normalized(self):
        key = self.capture()
        for item in (1, 2, 3):
            self.record(key, item=item)
        path = self.home / 'runtime/completion/records.json'
        original = path.read_text()
        mutations = (
            lambda data: data.update(raw_output='Authorization: Bearer synthetic-value'),
            lambda data: data['checkpoints'][0]['items'][0].update(text={'unexpected': 'object'}),
            lambda data: data['checkpoints'][0]['items'][0].update(constraint='unsupported'),
            lambda data: data['checkpoints'][0]['items'][0].pop('state'),
            lambda data: data['checkpoints'][0]['items'][0].update(text=' padded '),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                data = json.loads(original)
                mutate(data)
                path.write_text(json.dumps(data))
                before = path.read_bytes()
                self.assertEqual(self.recall(key)['status'], 'malformed')
                self.assertEqual(self.record(key)['status'], 'invalid_completion')
                self.assertEqual(path.read_bytes(), before)

    def test_missing_todo_items_is_structured_refusal(self):
        from omh.plugin_bundle.omh.todo_store import todo_path
        self.capture()
        path = todo_path(self.home, 'parent')
        data = json.loads(path.read_text())
        del data['items']
        path.write_text(json.dumps(data))
        self.assertEqual(self.call('checkpoint', accepted=True, revision='rev-a',
                         environment='offline-fixture')['status'], 'invalid_completion')

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO requires POSIX')
    def test_fifo_todo_is_refused_without_blocking_other_writers(self):
        import subprocess
        import sys
        from omh.plugin_bundle.omh.todo_store import todo_path
        key = self.capture()
        path = todo_path(self.home, 'parent')
        path.unlink()
        os.mkfifo(path)
        program = (
            'import json,os; homes={k:os.environ[k] for k in ("OMH_HOME","HERMES_HOME")}; '
            'from _local_package import load_local_package; load_local_package(); os.environ.update(homes); '
            'from omh.plugin_bundle.omh.tools.todo_tool import omh_todo_handler; '
            'print(omh_todo_handler({"action":"checkpoint","accepted":True,"revision":"rev-a",'
            '"environment":"offline-fixture"},session_id="parent"))'
        )
        result = subprocess.run([sys.executable, '-c', program], cwd=self.root,
            env={**{k: os.environ[k] for k in ('PATH', 'OMH_HOME', 'HERMES_HOME')},
                 'HOME': str(self.root), 'USERPROFILE': str(self.root),
                 **{k: os.environ[k] for k in
                    ('TMPDIR', 'TMP', 'TEMP', 'SYSTEMROOT', 'WINDIR', 'COMSPEC') if k in os.environ},
                 'PYTHONPATH': str(Path(__file__).resolve().parent)},
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'invalid_completion')
        self.assertEqual(self.record(key)['status'], 'written')

    def test_unsupported_lock_backend_does_not_write(self):
        from contextlib import contextmanager
        from omh.plugin_bundle.omh import completion_store as store
        key = self.capture()
        path = self.home / 'runtime/completion/records.json'
        before = path.read_bytes()
        @contextmanager
        def no_lock(*args, **kwargs):
            yield 'none'
        with patch.object(store, '_awareness_delivery_lock', no_lock):
            self.assertEqual(self.record(key)['status'], 'invalid_completion')
        self.assertEqual(path.read_bytes(), before)

    def test_checkpoint_respects_pending_acceptance_and_deferral(self):
        from omh.plugin_bundle.omh.todo_store import todo_path
        self.capture()
        path = todo_path(self.home, 'parent')
        for change in ({'plan_stage': 'awaiting_acceptance'}, {'deferred_reason': 'Person said stop'}):
            data = json.loads(path.read_text())
            data.update(change)
            path.write_text(json.dumps(data))
            before = path.read_bytes()
            self.assertEqual(self.call('checkpoint', accepted=True, revision='rev-a',
                             environment='offline-fixture')['status'], 'invalid_completion')
            self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
