"""Offline native completion acceptance. Real registration/lifecycle/handlers.

Operator reproduction (not a normal-user command): copy this fixture into an
isolated Hermes checkout at tests/agent/test_omh_native_completion_integration.py,
then run its canonical scripts/run_tests.sh with that path and
-o omh_checkout=/absolute/path/to/this/OMH/checkout. Keep synthetic HOME,
HERMES_HOME, OMH_HOME and TMPDIR; do not copy credentials. The source argument is
required and the test asserts which host and OMH source paths were loaded.

Only the human approval answer is substituted in the optional B3 case. No model
exists. The suite proves the contract after semantic selection, not that a live
model understands a particular sentence or actually selects this workflow.
The optional B3 case runs when the checkout includes upstream PR #1794.
"""
import importlib
import importlib.util
import itertools
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def runtime(tmp_path, monkeypatch, request):
    home = tmp_path / 'hermes'
    home.mkdir()
    (home / 'config.yaml').write_text('terminal:\n  backend: local\napprovals:\n  mode: manual\n')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('OMH_HOME', str(tmp_path / 'omh'))
    monkeypatch.setenv('HERMES_INTERACTIVE', '1')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    attempts = []

    def no_network(*args, **kwargs):
        attempts.append('forbidden network')
        raise AssertionError('Offline fixture attempted network')

    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket, 'create_connection', no_network)
    from hermes_cli import plugins
    from hermes_cli.plugins_manifest import PluginManifest
    import model_tools
    assert Path(model_tools.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2])
    source = next(v.partition('=')[2] for v in request.config.getoption('override_ini')
                  if v.startswith('omh_checkout='))
    bundle = Path(source) / 'src/plugin_bundle/omh'
    name = '_omh_native_completion_host_fixture'
    spec = importlib.util.spec_from_file_location(name, bundle / '__init__.py',
                                                 submodule_search_locations=[str(bundle)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    manager = plugins.PluginManager()
    manager._discovered = True
    monkeypatch.setattr(plugins, 'get_plugin_manager', lambda: manager)
    ctx = plugins.PluginContext(PluginManifest(name='omh-completion-fixture', version='1.0.0'), manager)
    module.register(ctx)
    load = lambda child: importlib.import_module(name + '.' + child)
    assert Path(load('tools.todo_tool').__file__).resolve().is_relative_to(bundle.resolve())
    events = []
    ctx.register_hook('post_tool_call', lambda **kw: events.append(kw))
    ids = itertools.count()

    def call(action, *, session='owner', bridge=False, **args):
        payload = {'action': action, **args}
        tool = 'omh_todo'
        if bridge:
            tool, payload = 'tool_call', {'calls': [{'name': tool, 'arguments': payload}]}
        events.clear()
        call_id = f'completion-{next(ids)}'
        result = model_tools.handle_function_call(tool, payload, session_id=session,
            task_id=call_id, tool_call_id=call_id, enabled_toolsets=['omh', 'file'])
        assert len(events) == 1, (result, events)
        assert events[0]['session_id'] == session
        assert events[0]['tool_name'] == 'omh_todo'
        return json.loads(events[0]['result'])

    manager.invoke_hook('on_session_start', session_id='owner')
    try:
        yield SimpleNamespace(call=call, manager=manager, load=load, tmp=tmp_path,
                              home=home, model_tools=model_tools)
    finally:
        manager.unload()
        for key in list(sys.modules):
            if key.startswith(name + '.'):
                del sys.modules[key]
        assert not attempts


def capture(r, **extra):
    set_result = r.call('set', items=[{'text': 'Parser', 'state': 'active'},
        {'text': 'Regression', 'state': 'pending'}, {'text': 'Docs', 'state': 'pending'}], **extra)
    assert set_result['status'] == 'written', set_result
    saved = r.call('checkpoint', accepted=True, rejected=['New engine'], revision='rev-a', environment='fixture')
    assert saved['status'] == 'written', saved
    return saved['checkpoint']['checkpoint_id']


def row(kind='verification', item=1, verdict='PASS', findings=None):
    return dict(kind=kind, item=item, verdict=verdict, summary='Fixture result declaration',
        findings=findings or [], claimed_source='host_exit', claimed_evidence_state='observed', references=[])


@pytest.mark.parametrize('bridge', [False, True])
@pytest.mark.parametrize('malformed', ['missing_items', 'scope_object', 'root_extra'])
def test_malformed_native_metadata_is_refused(runtime, bridge, malformed):
    r = runtime
    key = capture(r)
    store = r.load('runtime_paths').default_omh_home() / 'runtime/completion/records.json'
    if malformed == 'missing_items':
        path = r.load('todo_store').todo_path(r.load('runtime_paths').default_omh_home(), 'owner')
        data = json.loads(path.read_text())
        del data['items']
        path.write_text(json.dumps(data))
        result = r.call('checkpoint', bridge=bridge, accepted=True, revision='rev-a', environment='fixture')
        assert result['status'] == 'invalid_completion'
    else:
        data = json.loads(store.read_text())
        if malformed == 'scope_object':
            data['checkpoints'][0]['items'][0]['text'] = {'unexpected': 'object'}
        else:
            data['raw_output'] = 'Unsupported private output'
        store.write_text(json.dumps(data))
        before = store.read_bytes()
        result = r.call('recall', bridge=bridge, checkpoint_id=key, revision='rev-a', environment='fixture')
        assert result['status'] == 'malformed'
        result = r.call('record', bridge=bridge, checkpoint_id=key, revision='rev-a', environment='fixture', result=row())
        assert result['status'] == 'invalid_completion'
        assert store.read_bytes() == before


@pytest.mark.parametrize('bridge', [False, True])
def test_native_registry_lifecycle_and_cross_session_resume(runtime, bridge):
    r = runtime
    key = capture(r)
    for kind in ('verification', 'review', 'qa'):
        stored = r.call('record', bridge=bridge, checkpoint_id=key, revision='rev-a',
                        environment='fixture', result=row(kind))
        assert stored['status'] == 'written', stored
    r.manager.invoke_hook('on_session_end', session_id='owner')
    r.call('clear')
    r.manager.invoke_hook('on_session_start', session_id='later')
    read = r.call('recall', session='later', bridge=bridge, checkpoint_id=key,
                  revision='rev-a', environment='fixture')
    assert read['status'] == 'current', read
    assert len(read['checkpoint']['items']) == 3
    assert read['checkpoint']['rejected'] == ['New engine']
    assert read['sources']['review'] == 'declared_no_findings'
    assert read['completion']['missing'] == [2, 3]
    assert read['completion']['status'] == 'not_verified'
    assert all(not v['observed'] for v in read['checkpoint']['results'])
    assert r.call('set', session='later', items=read['checkpoint']['items'])['status'] == 'written'
    assert r.call('show', session='later')['todo']['counts']['total'] == 3


def test_native_overrides_missing_scope_and_stale_results(runtime):
    r = runtime
    key = capture(r)
    assert 'error' in r.call('recall', checkpoint_id=key, omh_home=str(r.tmp))
    assert r.call('checkpoint', session='other', accepted=True, revision='rev-a', environment='fixture')['status'] == 'invalid_completion'
    result = r.call('record', checkpoint_id=key, revision='rev-a', environment='fixture', result={**row(), 'observed': True})
    assert result['status'] == 'invalid_completion'
    read = r.call('recall', checkpoint_id=key, revision='rev-b', environment='fixture')
    assert read['status'] == 'stale'
    assert read['sources']['review'] == 'absent'


def test_accepted_three_item_delivery_uses_real_file_and_probe_handlers(runtime):
    """Scripted model selection; actual local effects and exit status are observed."""
    r = runtime
    key = capture(r)
    artifacts = {
        'parser.py': 'def parse_number(value):\n    return int(value.strip())\n',
        'test_parser.py': 'import unittest\nfrom parser import parse_number\nclass ParserTest(unittest.TestCase):\n    def test_number(self):\n        self.assertEqual(parse_number(" 42 "), 42)\n',
        'README.md': '# Parser\nWhitespace around decimal integers is accepted.\n',
    }
    for item, (name, content) in enumerate(artifacts.items(), 1):
        result = r.model_tools.handle_function_call('write_file',
            {'path': str(r.tmp / name), 'content': content}, session_id='owner',
            tool_call_id=f'write-{item}', enabled_toolsets=['file'])
        assert (r.tmp / name).read_text() == content, result
        assert r.call('advance', item=item, item_text=('Parser', 'Regression', 'Docs')[item - 1], state='done')['status'] == 'written'
    observed = json.loads(r.model_tools.handle_function_call('omh_gather_evidence',
        {'commands': ['python3 -m unittest test_parser -v'], 'workdir': str(r.tmp)},
        session_id='owner', tool_call_id='real-probe', enabled_toolsets=['omh']))
    assert observed['all_pass'], observed
    assert observed['results'][0]['exit_code'] == 0
    assert observed['results'][0]['evidence_type'] == 'observed_local_command'
    assert 'Ran 1 test' in observed['results'][0]['output_tail']
    for item in (1, 2, 3):
        assert r.call('record', checkpoint_id=key, revision='rev-a', environment='fixture', result=row(item=item))['status'] == 'written'
    read = r.call('recall', checkpoint_id=key, revision='rev-a', environment='fixture')
    assert read['completion']['declarations_complete']
    assert read['completion']['status'] == 'not_verified'  # never promoted by storing
    assert not (r.tmp / 'new_engine.py').exists()
    assert [i['text'] for i in read['checkpoint']['items']] == ['Parser', 'Regression', 'Docs']
    assert r.call('show')['todo']['counts']['done'] == 3


def test_b3_checkpoint_never_grants_plan_edit_approval(runtime, monkeypatch):
    r = runtime
    schema = r.load('tools.todo_tool').OMH_TODO_SCHEMA
    if 'plan_stage' not in schema['parameters']['properties']:
        pytest.skip('Upstream B3 gate not present in this source; run against composed candidate')
    from tools import terminal_tool, approval
    for name, value in (('_permanent_approved', set()), ('_permanent_approved_by_home', {}),
                        ('_permanent_baseline_by_home', {}), ('_session_approved', {})):
        monkeypatch.setattr(approval, name, value)
    prompts = []
    previous = terminal_tool._get_approval_callback()
    terminal_tool.set_approval_callback(lambda command, description, **kw: (prompts.append((command, description)) or 'deny'))
    try:
        r.call('set', items=[{'text': 'Accepted only after review', 'state': 'active'}], plan_stage='awaiting_acceptance')
        blocked = r.call('checkpoint', accepted=True, revision='rev-a', environment='fixture')
        assert blocked['status'] == 'invalid_completion'
        target = r.tmp / 'must-not-exist.txt'
        output = r.model_tools.handle_function_call('write_file', {'path': str(target), 'content': 'unauthorized'},
            session_id='owner', tool_call_id='blocked-write', enabled_toolsets=['file'])
        assert not target.exists(), output
        assert prompts, output
        assert r.call('show')['todo']['plan_stage'] == 'awaiting_acceptance'
        # The normal accepted todo stamp is still owned by the existing writer.
        key = capture(r, plan_stage='accepted')
        assert r.call('show')['todo']['plan_stage'] == 'accepted'
        assert r.call('recall', checkpoint_id=key, revision='rev-a', environment='fixture')['status'] == 'current'
    finally:
        terminal_tool.set_approval_callback(previous)
