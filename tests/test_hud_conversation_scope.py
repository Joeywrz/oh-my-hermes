"""Session-local HUD rows must never borrow another conversation's work."""
from contextlib import closing
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from omh.plugin_bundle.omh.runtime_reader import read_omh_hud
from omh.tui_widget_pack import widget_payload
from test_kanban_board_reader import build_board, task
from test_plugin_hermes_delegation import (
    NOW,
    PARENT_ID,
    _build_state_db,
    _record,
    _write_manifest,
    _write_provenance,
)


class HudConversationScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hermes = self.root / '.hermes'
        self.hermes.mkdir()
        self.omh = self.root / '.omh'
        self.env = mock.patch.dict(os.environ, {'HOME': str(self.root), 'OMH_HOME': str(self.omh), 'HERMES_HOME': str(self.hermes)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.clock = mock.patch('omh.plugin_bundle.omh.hermes_delegation.time.time', return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def build(self, other_count=1):
        children = [{'id': 'child_own', 'model': 'gpt-5.6-sol', 'started_at': NOW - 30,
                     'usage': {'input_tokens': 100, 'output_tokens': 20, 'actual_cost_usd': 0.25, 'last_seen': NOW - 1}}]
        children += [{'id': f'child_other{i}', 'model': 'other-model', 'started_at': NOW - 10,
                      'usage': {'input_tokens': 9000, 'actual_cost_usd': 9, 'last_seen': NOW - 1}} for i in range(other_count)]
        _build_state_db(self.hermes, children)
        with closing(sqlite3.connect(self.hermes / 'state.db')) as db, db:
            db.execute('INSERT INTO sessions VALUES (?, ?, ?, ?)', ('other-owner', 'other-model', '{}', NOW - 50))
            for i in range(other_count):
                db.execute('UPDATE sessions SET model_config=? WHERE id=?', (json.dumps({'_delegate_from': 'other-owner'}), f'child_other{i}'))

    def hud(self, **kwargs):
        return read_omh_hud(self.omh, self.hermes, **kwargs)

    def test_rows_and_derived_totals_are_owned_by_the_reading_conversation(self):
        self.build()
        own = self.hud(session_ref=PARENT_ID)['subagents']
        self.assertEqual([row['task_id'] for row in own['rows']], ['own'])
        self.assertEqual((own['active'], own['running'], own['blocked'], own['completed']), (1, 1, 0, 0))
        self.assertEqual(sum(row['tokens'] for row in own['rows']), 120)
        self.assertEqual(sum(row['cost_usd'] for row in own['rows']), 0.25)
        self.assertEqual([row['task_id'] for row in self.hud(session_ref='other-owner')['subagents']['rows']], ['other0'])
        self.assertEqual(len(self.hud()['subagents']['rows']), 2)

    def test_mapped_empty_conversation_never_falls_back(self):
        self.build()
        with closing(sqlite3.connect(self.hermes / 'state.db')) as db, db:
            db.execute('INSERT INTO sessions VALUES (?, ?, ?, ?)', ('empty-owner', '', '{}', NOW))
        result = self.hud(session_ref='empty-owner')['subagents']
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['active'], 0)
        self.assertEqual(result['scope'], 'session')

    def test_global_fallback_preserves_manifest_context(self):
        self.build()
        _write_manifest(self.hermes, 'global-dispatch', ['Global work'], started=NOW - 32, log_mtime=NOW)
        result = self.hud(tui_session_ref='unmapped')['subagents']
        self.assertTrue(any(row['action'] == 'Global work' for row in result['rows']))
        self.assertTrue(all(row['scope'] == 'global' for row in result['rows']))

    def test_ownership_is_applied_before_the_native_row_cap(self):
        self.build(other_count=40)
        result = self.hud(session_ref=PARENT_ID)['subagents']
        self.assertEqual([row['task_id'] for row in result['rows']], ['own'])
        self.assertEqual(result['hidden_rows'], 0)
        self.assertEqual(result['active'], 1)

    def test_unknown_or_malformed_identity_does_not_select_an_owner(self):
        self.build()
        for reference in ('not-a-session', PARENT_ID + '\n', ' ' + PARENT_ID, PARENT_ID + '/' , 'x' * 161):
            for argument in ('session_ref', 'tui_session_ref'):
                with self.subTest(argument=argument, reference=reference):
                    result = self.hud(**{argument: reference})
                    self.assertEqual(len(result['subagents']['rows']), 2)
                    self.assertEqual(result['subagents']['scope'], 'global')
                    self.assertTrue(all(row['scope'] == 'global' for row in result['subagents']['rows']))

    def test_compression_edges_keep_own_history_but_not_delegates_or_branches(self):
        self.build()
        with closing(sqlite3.connect(self.hermes / 'state.db')) as db, db:
            for column in ('parent_session_id TEXT', 'end_reason TEXT', 'source TEXT'):
                db.execute('ALTER TABLE sessions ADD COLUMN ' + column)
            db.execute("UPDATE sessions SET end_reason='compression', source='tui' WHERE id=?", (PARENT_ID,))
            for sid, parent, config, source in (
                ('continued', PARENT_ID, {}, 'tui'),
                ('branch', PARENT_ID, {'_branched_from': PARENT_ID}, 'tui'),
                ('tool-child', PARENT_ID, {}, 'tool'),
                ('real-child', PARENT_ID, {'_delegate_from': PARENT_ID}, 'tool'),
            ):
                db.execute('INSERT INTO sessions (id, model, model_config, started_at, parent_session_id, source) VALUES (?, ?, ?, ?, ?, ?)',
                           (sid, 'gpt-5.6-sol', json.dumps(config), NOW - 400, parent, source))
            for suffix, parent in (('continued', 'continued'), ('branch', 'branch'), ('nested', 'real-child'), ('tool', 'tool-child')):
                db.execute('INSERT INTO sessions (id, model, model_config, started_at) VALUES (?, ?, ?, ?)',
                           ('worker_' + suffix, 'gpt-5.6-sol', json.dumps({'_delegate_from': parent}), NOW - 3))
        for reference in (PARENT_ID, 'continued'):
            result = self.hud(session_ref=reference)
            self.assertEqual({row['task_id'] for row in result['subagents']['rows']}, {'own', 'continue', 'real-chi'})
        self.assertEqual({row['task_id'] for row in self.hud(session_ref='real-child')['subagents']['rows']}, {'nested'})

    def _own_row(self, records, reference=PARENT_ID):
        # `child_own` runs the parent's own model, so the chain projection
        # alone says `inherit` and only a route record can say the lane was
        # routed there — the owner's `deep` head is that exact shape.
        if not (self.hermes / 'state.db').exists():
            self.build()
        _write_provenance(self.omh, records)
        rows = self.hud(session_ref=reference)['subagents']['rows']
        return next(row for row in rows if row['task_id'] == 'own')

    @staticmethod
    def _head_record(**overrides):
        return _record(**{'origin': 'head', 'alias': 'gpt-5.6-sol', 'wire_model': 'gpt-5.6-sol',
                          'provider': '', 'written_at': NOW - 60, **overrides})

    def test_a_route_this_conversation_prepared_labels_its_own_child(self):
        # The upgrade had never once fired in the HUD: the widget always
        # reads session-scoped, and session scope discarded every record.
        row = self._own_row([self._head_record(session_id=PARENT_ID)])
        self.assertEqual(row['category'], 'visual-engineering')
        self.assertEqual(row['category_source'], 'route_provenance')
        self.assertIs(row['same_as_parent'], True)

    def test_a_route_another_conversation_prepared_never_labels_this_one(self):
        row = self._own_row([self._head_record(session_id='other-owner')])
        self.assertEqual(row['category'], 'inherit')
        self.assertNotIn('category_source', row)
        self.assertNotIn('same_as_parent', row)

    def test_a_route_record_naming_no_session_is_never_claimed_by_one(self):
        # Records written before the field existed carry no ownership, and
        # unknown ownership is not this conversation's. This is also what
        # those records did before, so no install loses a label it had.
        row = self._own_row([self._head_record()])
        self.assertEqual(row['category'], 'inherit')
        self.assertNotIn('category_source', row)

    def test_an_exhaustion_record_is_owned_like_every_other_route_record(self):
        # The exhaustion claim is precomputed against the same record list,
        # so a foreign record must never reach it: it would relabel an
        # ordinary inherit lane as another conversation's cleared chain.
        exhausted = dict(origin='exhausted_to_inherit', alias='', wire_model='', provider='')
        row = self._own_row([self._head_record(session_id=PARENT_ID, **exhausted)])
        self.assertEqual(row['route_origin'], 'exhausted_to_inherit')
        self.assertEqual(row['route_category'], 'visual-engineering')
        row = self._own_row([self._head_record(session_id='other-owner', **exhausted)])
        self.assertEqual(row['category'], 'inherit')
        self.assertNotIn('route_origin', row)

    def test_a_route_prepared_before_a_compression_survives_into_it(self):
        # Ownership is the conversation, not one session row: the reader
        # already selects children across a compression edge, so a route
        # the pre-compression session prepared still belongs to the
        # continuation reading its own HUD.
        self.build()
        with closing(sqlite3.connect(self.hermes / 'state.db')) as db, db:
            for column in ('parent_session_id TEXT', 'end_reason TEXT', 'source TEXT'):
                db.execute('ALTER TABLE sessions ADD COLUMN ' + column)
            db.execute("UPDATE sessions SET end_reason='compression', source='tui' WHERE id=?", (PARENT_ID,))
            db.execute('INSERT INTO sessions (id, model, model_config, started_at, parent_session_id, source) VALUES (?, ?, ?, ?, ?, ?)',
                       ('continued', 'gpt-5.6-sol', '{}', NOW - 400, PARENT_ID, 'tui'))
        _write_provenance(self.omh, [self._head_record(session_id=PARENT_ID)])
        rows = self.hud(session_ref='continued')['subagents']['rows']
        row = next(row for row in rows if row['task_id'] == 'own')
        self.assertEqual(row['category_source'], 'route_provenance')
        self.assertIs(row['same_as_parent'], True)

    def test_global_scope_reads_every_route_record_whoever_prepared_it(self):
        # Global scope makes no ownership claim at all — it already shows
        # children from every conversation — so filtering it would hide a
        # record from the one scope that never promised to be selective.
        self.build()
        for owner in ({}, {'session_id': PARENT_ID}, {'session_id': 'other-owner'}):
            with self.subTest(owner=owner):
                _write_provenance(self.omh, [self._head_record(**owner)])
                rows = self.hud(tui_session_ref='unmapped')['subagents']['rows']
                row = next(row for row in rows if row['task_id'] == 'own')
                self.assertEqual(row['category'], 'visual-engineering')
                self.assertEqual(row['category_source'], 'route_provenance')

    def test_unowned_manifests_cannot_supply_labels_or_liveness(self):
        self.build()
        _write_manifest(self.hermes, 'unrelated-dispatch', ['Unrelated work'], started=NOW - 32, log_mtime=NOW + 50)
        result = next(row for row in self.hud(session_ref=PARENT_ID)['subagents']['rows'] if row['task_id'] == 'own')
        self.assertEqual(result['action'], '')
        self.assertEqual(result['delegation_id'], '')
        self.assertLessEqual(result['elapsed_seconds'], 30)

    def test_unowned_omh_and_maestro_rows_remain_explicitly_global(self):
        self.build()
        status = {'active_executors': [
            {'target_id': 'foreign-run', 'executor_profile': profile, 'tokens_total': 9999, 'cost_usd': 55}
            for profile in ('hermes_local', 'maestro')],
            'latest_progress_events': [{'event_type': 'executor_completed'}], 'runs': []}
        result = self.hud(session_ref=PARENT_ID, status=status)
        self.assertTrue(result['maestro']['rows'])
        self.assertEqual(result['maestro']['scope'], 'global')
        self.assertEqual(result['maestro']['rows'][0]['scope'], 'global')
        self.assertEqual(result['subagents']['active'], 3)
        self.assertEqual(result['subagents']['scope'], 'mixed')
        self.assertEqual(result['runtime']['scope'], 'global')
        self.assertNotEqual(result['graph'].get('reason'), 'session_ownership_unavailable')

    def test_kanban_rows_are_owned_by_the_same_conversation_identities(self):
        # The board stamps a task with the originating HERMES_SESSION_ID, the
        # durable id state.db names, so board rows scope exactly like the
        # delegate rows: owned for a mapped conversation, explicitly global
        # for an unmapped one, and never borrowed across owners.
        self.build()
        build_board(self.hermes / 'kanban.db', [
            task('t_mine0001', 'running', title='Own board task', started_at=int(NOW) - 5, session_id=PARENT_ID),
            task('t_other001', 'ready', title='Other board task', session_id='other-owner'),
        ])
        with mock.patch('omh.plugin_bundle.omh.kanban_board_reader.time.time', return_value=NOW):
            own = self.hud(session_ref=PARENT_ID)
            other = self.hud(session_ref='other-owner')
            unmapped = self.hud(tui_session_ref='unmapped')
            nobody = self.hud(session_ref='empty-owner')
        board_rows = [row for row in own['subagents']['rows'] if row.get('lane_backend') == 'kanban']
        self.assertEqual([row['task_id'] for row in board_rows], ['mine0001'])
        self.assertEqual(board_rows[0]['scope'], 'session')
        self.assertEqual(own['subagents']['scope'], 'session')
        self.assertEqual((own['subagents']['active'], own['subagents']['running']), (2, 2))
        self.assertEqual(own['kanban']['rows_total'], 1)
        self.assertEqual(
            [row['task_id'] for row in other['subagents']['rows'] if row.get('lane_backend') == 'kanban'],
            ['other001'],
        )
        self.assertEqual(sorted(row['task_id'] for row in unmapped['subagents']['rows'] if row.get('lane_backend') == 'kanban'),
                         ['mine0001', 'other001'])
        self.assertEqual(unmapped['subagents']['scope'], 'global')
        # A mapped conversation that owns no board task sees the whole board
        # as explicitly global, and the header says both scopes are on screen.
        self.assertEqual(nobody['subagents']['scope'], 'global')
        self.assertTrue(all(row['scope'] == 'global' for row in nobody['subagents']['rows']))

    def test_widget_transport_identity_resolves_only_through_the_host_lease(self):
        # The widget's reference is the gateway transport id whenever its
        # session was created rather than resumed, and no state.db row carries
        # that name. The host's lease registry is the one surface pairing it
        # with the durable key; a reference no lease names is a reference, not
        # a licence to read the most-recently-active session's plan.
        self.build()
        from omh.plugin_bundle.omh.todo_store import TODO_SCHEMA_VERSION, todo_path
        record = {'schema_version': TODO_SCHEMA_VERSION, 'session_ref': PARENT_ID,
                  'updated_at': '2027-01-15T08:00:00Z', 'title': 'Private plan',
                  'items': [{'text': 'Own task', 'state': 'active'}]}
        path = todo_path(self.omh, PARENT_ID)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record))
        registry = self.hermes / 'runtime' / 'active_sessions.json'
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps({'entries': [
            {'lease_id': 'lease-0', 'pid': 4321, 'session_id': PARENT_ID, 'surface': 'tui',
             'metadata': {'live_session_id': 'paired-transport'}}]}))
        with mock.patch('omh.plugin_bundle.omh.runtime_reader.live_tui_session_rows', return_value=[
            {'id': PARENT_ID, 'activity': NOW, 'started_at': NOW - 50}]), mock.patch(
                'omh.plugin_bundle.omh.runtime_reader._utc_epoch_now', return_value=NOW):
            paired = self.hud(tui_session_ref='paired-transport')
            unmapped = self.hud(tui_session_ref='unmapped-transport')
        self.assertEqual(paired['todo']['title'], 'Private plan')
        self.assertEqual((unmapped['todo']['title'], unmapped['todo']['status']), ('', 'absent'))

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the widget boundary')
    def test_scoped_activity_rows_fit_and_keep_token_columns(self):
        widget = self.root / 'widget.mjs'
        widget.write_bytes(widget_payload(Path(sys.executable)))
        script = """
import register from './widget.mjs';
const apps = [], lines = [];
const h = (tag, props, ...children) => {
  if (typeof tag === 'function') {
    const text = tag(props);
    if (tag.name === 'ActivityRow') lines.push(text);
    return text;
  }
  return children.flat(Infinity).filter(x => x != null).join('');
};
register({Box:'box', Text:'text', h, defineWidgetApp: app => {apps.push(app); return app}, openWidget:()=>{}, updateWidget:()=>{}});
const app = apps.find(x => x.id === 'omh-status');
const results = [];
for (const cols of [80, 100]) {
  for (const model of ['', 'gpt', 'claude-fable-5-1:xhigh'])
  for (const scopes of [['global', 'global'], ['session', 'session'], ['global', 'session']]) {
    lines.length = 0;
    const rows = scopes.map((scope, i) => ({scope, model, task_id:`worker${i}`, action:'A long action title that must yield to the token column', state:'done', elapsed_seconds:12, tokens:12345}));
    app.render({cols, rows:30, state:{payload:{privacy:'metadata_only', active:true, subagents:{rows}, maestro:{rows:[]}}}, t:{color:{}}});
    results.push({cols, scopes, lines:[...lines]});
  }
}
console.log(JSON.stringify(results));
"""
        result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=self.root,
                                encoding='utf-8', capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for case in json.loads(result.stdout):
            with self.subTest(columns=case['cols'], scopes=case['scopes']):
                self.assertEqual(len(case['lines']), 2)
                for scope, line in zip(case['scopes'], case['lines']):
                    # Rows carry what runs them, not a scope word: a delegate
                    # child is `[sub]`. The header marks `[global]` when rows
                    # from outside this conversation are listed; neither line
                    # repeats it.
                    self.assertIn('[sub] ', line)
                    self.assertNotIn('[global]', line)
                    self.assertNotIn('[this chat]', line)
                    self.assertIn('12.3k tokens', line[:case['cols']])
                    self.assertLessEqual(len(line), case['cols'] - 2)
                self.assertEqual(*[line.index('12.3k tokens') for line in case['lines']])

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the widget boundary')
    def test_header_marks_the_scope_only_when_rows_from_outside_are_listed(self):
        # The header word is a presence marker, not a description of the list:
        # it says rows from outside this conversation are on screen, and says
        # nothing at all when they are not. A session-only list is already
        # fully described by the rows' own `[sub]`/`[bot]` tags, and a payload
        # that names no scope must stay silent rather than pick a side.
        widget = self.root / 'widget.mjs'
        widget.write_bytes(widget_payload(Path(sys.executable)))
        script = """
import register from './widget.mjs';
const apps = [];
const h = (tag, props, ...children) => typeof tag === 'function' ? tag(props) : children.flat(Infinity).filter(x => x != null).join('');
register({Box:'box', Text:'text', h, defineWidgetApp: app => {apps.push(app); return app}, openWidget:()=>{}, updateWidget:()=>{}});
const app = apps.find(x => x.id === 'omh-status');
const row = scope => ({scope, task_id:'worker', action:'Work', state:'running', elapsed_seconds:12, tokens:1200});
const cases = {
  session: [{scope:'session', active:1, running:1, rows:[row('session')]}, {rows:[]}],
  global: [{scope:'global', active:1, running:1, rows:[row('global')]}, {rows:[]}],
  mixed: [{scope:'mixed', active:1, running:1, rows:[row('session'), row('global')]}, {rows:[]}],
  absent: [{active:1, running:1, rows:[row('session')]}, {rows:[]}],
  absent_global_maestro: [{active:1, running:1, rows:[row('session')]}, {rows:[row('global')]}],
  session_global_maestro: [{scope:'session', active:1, running:1, rows:[row('session')]}, {rows:[row('global')]}],
};
const results = {};
for (const [name, [subagents, maestro]] of Object.entries(cases)) {
  results[name] = app.render({cols:160, rows:30, state:{payload:{privacy:'metadata_only', active:true, subagents, maestro}}, t:{color:{}}});
}
console.log(JSON.stringify(results));
"""
        result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=self.root,
                                encoding='utf-8', capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = json.loads(result.stdout)
        state = '1 agent · 1 running'
        # A global row anywhere in the list — the aggregate scope, or a
        # Maestro row the aggregate does not cover — earns the one marker.
        for name in ('global', 'mixed', 'absent_global_maestro', 'session_global_maestro'):
            with self.subTest(case=name):
                self.assertIn(f'[global] {state}', rendered[name])
        # Nothing outside this conversation is listed, so the header states
        # the activity and stops there.
        for name in ('session', 'absent'):
            with self.subTest(case=name):
                self.assertIn(state, rendered[name])
                self.assertNotIn('[global]', rendered[name])
        # The retired vocabulary is gone from every branch, including the one
        # that used to spell both scopes out.
        for name, view in rendered.items():
            with self.subTest(case=name):
                self.assertNotIn('[this chat]', view)
                self.assertNotIn('this chat + global', view)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the widget boundary')
    def test_widget_renders_owned_rows_and_explicit_global_fallback(self):
        # Run the shipped widget's actual Python reader, not a replica of its kwargs.
        self.build()
        with closing(sqlite3.connect(self.hermes / 'state.db')) as db, db:
            db.execute('UPDATE sessions SET started_at=? WHERE id LIKE ?', (time.time() - 2, 'child_%'))
        plugins = self.hermes / 'plugins'
        plugins.mkdir()
        source = Path(__file__).resolve().parents[1] / 'src/plugin_bundle/omh'
        shutil.copytree(source, plugins / 'omh')
        widget = self.root / 'widget.mjs'
        widget.write_bytes(widget_payload(Path(sys.executable)))
        script = """
import childProcess from 'node:child_process';
import {syncBuiltinESMExports} from 'node:module';
import {pathToFileURL} from 'node:url';
childProcess.execFile = (exe, args, opts, cb) => {
  process.stdout.write(JSON.stringify({exe, args, env: opts.env})); cb(new Error('capture'));
};
syncBuiltinESMExports();
const {default: register} = await import(pathToFileURL(process.argv[1]).href);
register({Box: 'box', Text: 'text', h: () => null, defineWidgetApp: x => x,
          openWidget: () => null, updateWidget: () => null});
"""
        active = self.root / 'active.json'
        for reference in (None, 'bad/id', 'unmapped-transport', PARENT_ID, 'other-owner'):
            if reference is not None:
                active.write_text(json.dumps({'session_id': reference}))
            env = {**os.environ, 'HERMES_TUI_ACTIVE_SESSION_FILE': str(active)}
            capture = subprocess.run(['node', '--input-type=module', '-e', script, str(widget)], env=env, encoding='utf-8', capture_output=True)
            self.assertEqual(capture.returncode, 0, capture.stderr)
            invocation = json.loads(capture.stdout)
            read = subprocess.run([invocation['exe'], *invocation['args']], env=invocation['env'], encoding='utf-8', capture_output=True)
            self.assertEqual(read.returncode, 0, read.stderr)
            rows = json.loads(read.stdout)['subagents']['rows']
            expected = {PARENT_ID: ['own'], 'other-owner': ['other0']}.get(reference or '', ['own', 'other0'])
            self.assertEqual(sorted(row['task_id'] for row in rows), sorted(expected), reference)
            scope = 'session' if reference in (PARENT_ID, 'other-owner') else 'global'
            self.assertTrue(all(row['scope'] == scope for row in rows))
            render_script = """
import register from './widget.mjs';
const payload = JSON.parse(process.argv[1]);
const apps = [];
const h = (tag, props, ...children) => typeof tag === 'function' ? tag(props) : children.flat(Infinity).filter(x => x != null).join('');
register({Box:'box', Text:'text', h, defineWidgetApp: app => {apps.push(app); return app}, openWidget:()=>{}, updateWidget:()=>{}});
const app = apps.find(x => x.id === 'omh-status');
const render = value => app.render({cols:160, rows:30, state:{payload:value}, t:{color:{}}});
const native = render(payload);
const globalRow = {...payload.subagents.rows[0], scope:'global', task_id:'executor', dispatch_lane:'maestro', executor_profile:'codex'};
const omh = render({...payload, active:true, subagents:{...payload.subagents, scope:'global', rows:[globalRow]}, maestro:{scope:'global', rows:[globalRow]}, graph:{scope:'global', status:'active', nodes:[{node_id:'global-node', state:'running'}]}});
console.log(JSON.stringify({native, omh}));
"""
            render = subprocess.run(['node', '--input-type=module', '-e', render_script, read.stdout], cwd=self.root, env=env, encoding='utf-8', capture_output=True)
            self.assertEqual(render.returncode, 0, render.stderr)
            views = json.loads(render.stdout)
            # Executor and Maestro rows carry no kind tag; the DAG block keeps
            # its explicit global word.
            self.assertIn('MAIN', views['omh'])
            self.assertNotIn('[global] MAIN', views['omh'])
            self.assertIn('executor', views['omh'])
            self.assertNotIn('[bot] executor', views['omh'])
            self.assertNotIn('[sub] executor', views['omh'])
            self.assertIn('[global] DAG', views['omh'])
            self.assertIn('global-node', views['omh'])
            self.assertIn('codex/maestro', views['omh'])
            rendered = views['native']
            # The header marks `[global]` once when rows from outside this
            # conversation are listed and stays silent otherwise; every native
            # row carries the `[sub]` kind tag either way.
            self.assertEqual('[global]' in rendered, scope == 'global')
            self.assertEqual(rendered.count('[sub] '), len(rows))
            self.assertNotIn('[this chat]', rendered)
