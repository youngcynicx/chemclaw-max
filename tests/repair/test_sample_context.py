"""External research context: actual objects, sources, permissions and budgets."""
import json
import threading
import urllib.request

import pytest

from chemclaw_lab import auth, capabilities, clients, db, experiments, mcp
from chemclaw_lab.api import create_api_server
from chemclaw_lab.validation import validate_payload_against_schema
from chemclaw_lab.read_contracts import READ_OUTPUT_SCHEMAS


@pytest.fixture()
def context_lab(lab):
    owner = lab.context('researcher', memberships={'P1': 'member', 'P2': 'member'})
    viewer = lab.context('external', kind='service', memberships={'P1': 'viewer'})
    page1, page2 = lab.add_page('P1'), lab.add_page('P1')
    transcript1 = lab.add_transcript(page1['page_id'], ['Synthetic records only.',
        'A and B: first 80 C for 2 h, then 100 C for 3 h.', 'A: 20 mg.', 'B: 18 mg.'])
    transcript2 = lab.add_transcript(page2['page_id'], ['A reworked at 110 C for 1 h; not weighed again.',
        'A and B: comparison planned; not yet performed.', 'Unassigned note.'])
    def item(key, fields, block, *, page=page1, transcript=transcript1, kind='observation', date='2026-10-21'):
        return {'item_key': key, 'event_kind': kind, 'occurred_at': date, 'raw_text': key,
            'fields': {**fields, 'source_kind': 'simulated_record', 'source_notice': 'Synthetic acceptance material'},
            'unknowns': ['yield not recorded'], 'evidence_refs': [{'page_id': page['page_id'],
                'transcript_revision': 1, 'block_id': transcript['block_ids'][block]}]}
    def create(page, items, operation):
        return lab.invoke(owner, 'memory.proposal.create', {'project_id': 'P1', 'page_id': page['page_id'],
            'transcript_revision': 1, 'profile_id': 'chemistry_notebook', 'items': items, 'operation_id': operation},
            operation_id=operation)['result']['proposal']
    one = create(page1, [
        item('shared', {'related_sample_labels': [{'normalized': 'A'}, {'normalized': 'B'}],
            'stages': [{'temperature': 80, 'hours': 2}, {'temperature': 100, 'hours': 3}]}, 1, kind='operation'),
        item('A-mass', {'sample_label': {'normalized': 'A'}, 'product_mass': {'normalized': 20, 'unit': 'mg'}}, 2),
        item('B-mass', {'sample_label': {'normalized': 'B'}, 'product_mass': {'normalized': 18, 'unit': 'mg'}}, 3),
        item('simulation notice', {'record_scope': 'source_metadata'}, 0),
    ], 'first')
    two = create(page2, [
        item('A-rework', {'sample_label': {'normalized': 'A'}, 'phase': 'rework', 'duration': {'normalized': 1, 'unit': 'h'}},
            0, page=page2, transcript=transcript2, kind='operation', date='2026-10-22'),
        item('shared-plan', {'related_sample_labels': [{'normalized': 'A'}, {'normalized': 'B'}], 'result': None,
            'result_status': 'not_performed'}, 1, page=page2, transcript=transcript2, kind='plan', date='2026-10-24'),
        item('unassigned', {}, 2, page=page2, transcript=transcript2),
    ], 'second')
    ids = {row['label']: row['sample_id'] for row in lab.rows('SELECT label,sample_id FROM samples')}
    return lab, owner, viewer, page1, page2, transcript1, transcript2, one, two, ids


def fetch(data, **extra):
    lab, _, viewer, *_, ids = data
    params = {'project_id': 'P1', 'sample_id': ids['A'], **extra}
    return lab.invoke(viewer, 'query.sample.context', params, operation_id='same-read')['result']


def test_context_one_call_shared_facts_sources_unknowns_and_no_business_writes(context_lab):
    lab, _, _, page1, _, transcript, _, first, _, ids = context_lab
    tables = ('samples', 'event_revisions', 'memory_proposals', 'event_confirmations', 'inventory_transactions', 'operation_results')
    before = {table: lab.scalar('SELECT count(*) FROM ' + table) for table in tables}
    result = fetch(context_lab)
    assert result['status'] == 'complete' and result['coverage']['retrieval_complete'] is True, result['quality']
    assert result['coverage']['knowledge_complete'] is None
    assert result['sample']['sample_id'] == ids['A']
    assert {e['raw_text'] for e in result['events']} == {'shared', 'A-mass', 'A-rework', 'shared-plan'}
    shared = next(e for e in result['events'] if e['raw_text'] == 'shared')
    assert set(shared['sample_ids']) == set(ids.values())
    assert shared['fields']['stages'] == [{'temperature': 80, 'hours': 2}, {'temperature': 100, 'hours': 3}]
    assert all(e['fields']['source_kind'] == 'simulated_record' for e in result['events'])
    assert next(e for e in result['events'] if e['event_kind'] == 'plan')['fields']['result'] is None
    assert result['quality']['unassigned_records_on_linked_pages'] == 1
    assert len(result['relations']) == 2
    assert result['source_context'][0]['raw_text'] == 'simulation notice'
    mass_block = next(e for e in result['evidence'] if e['block_id'] == transcript['block_ids'][2])
    assert mass_block['blocks'][0]['text'] == 'A: 20 mg.'
    assert 'storage_path' not in json.dumps(result)
    assert before == {table: lab.scalar('SELECT count(*) FROM ' + table) for table in tables}
    encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()
    assert len(encoded) == result['coverage']['result_bytes'] <= 64000


def test_correction_preserves_old_source_and_fingerprint_guards_expansion(context_lab):
    lab, owner, _, page1, _, transcript, _, proposal, _, _ = context_lab
    initial = fetch(context_lab)
    event_id = next(item['event_id'] for item in proposal['items'] if item['item_key'] == 'A-mass')
    lab.invoke(owner, 'memory.event.correct', {'event_id': event_id, 'expected_revision': 1,
        'changes': {'fields': {'product_mass': {'normalized': 22, 'unit': 'mg'}}}, 'reason': 'Correct to 22 mg'}, operation_id='mass-correction')
    lab.add_transcript(page1['page_id'], ['New source revision says 22 mg.'])
    current = fetch(context_lab)
    mass = next(e for e in current['events'] if e['event_id'] == event_id)
    assert mass['revision'] == 2 and mass['fields']['product_mass']['normalized'] == 22
    assert mass['source_stale'] is True and mass['provenance']['reason'] == 'Correct to 22 mg'
    source = next(e for e in current['evidence'] if e['block_id'] == transcript['block_ids'][2])
    assert source['transcript_revision'] == 1 and source['active_revision'] == 2
    assert source['blocks'][0]['text'] == 'A: 20 mg.'
    assert current['snapshot']['fingerprint'] != initial['snapshot']['fingerprint']
    with pytest.raises(capabilities.CapabilityError) as error:
        fetch(context_lab, expected_fingerprint=initial['snapshot']['fingerprint'])
    assert error.value.code == 'snapshot_changed'
    exact = lab.invoke(context_lab[2], 'memory.event.read', {'event_id': event_id, 'revision': 1})['result']
    assert exact['field_json']['fields']['product_mass']['normalized'] == 20


def test_context_asof_handles_iso_timezone_and_ignores_future_revisions(context_lab):
    lab, owner, _, _, _, _, _, proposal, _, _ = context_lab
    event_id = next(item['event_id'] for item in proposal['items'] if item['item_key'] == 'A-mass')
    lab.execute("UPDATE event_revisions SET knowledge_as_of='2026-10-21 07:00:00'")
    lab.invoke(owner, 'memory.event.correct', {'event_id': event_id, 'expected_revision': 1,
        'changes': {'fields': {'product_mass': {'normalized': 22, 'unit': 'mg'}}}, 'reason': '22 mg'}, operation_id='dated-correction')
    lab.execute("UPDATE event_revisions SET knowledge_as_of='2026-10-21 09:00:00' WHERE revision=2")
    old = fetch(context_lab, snapshot={'mode': 'as_of', 'knowledge_as_of': '2026-10-21T16:00:00+08:00'})
    mass = next(e for e in old['events'] if e['event_id'] == event_id)
    assert mass['revision'] == 1 and mass['fields']['product_mass']['normalized'] == 20
    assert mass['current_revision'] == 2 and mass['is_current'] is False
    assert old['snapshot']['identity_scope'] == 'current'


def test_context_resolves_unique_alias_and_returns_ambiguity_without_guessing(context_lab):
    lab, _, viewer, *_, ids = context_lab
    lab.execute('UPDATE samples SET aliases_json=? WHERE sample_id=?', ('["Alias-A"]', ids['A']))
    by_alias = lab.invoke(viewer, 'query.sample.context', {'project_id': 'P1', 'sample_label': 'Alias-A'})['result']
    assert by_alias['sample']['sample_id'] == ids['A']
    lab.execute('UPDATE samples SET aliases_json=? WHERE sample_id=?', ('["Alias-A"]', ids['B']))
    ambiguous = lab.invoke(viewer, 'query.sample.context', {'project_id': 'P1', 'sample_label': 'Alias-A'})['result']
    assert ambiguous['status'] == 'ambiguous' and ambiguous['sample'] is None
    assert len(ambiguous['resolution']['candidates']) == 2 and ambiguous['events'] == []
    with pytest.raises(capabilities.CapabilityError) as changed:
        lab.invoke(viewer, 'query.sample.context', {'project_id': 'P1', 'sample_label': 'Alias-A',
            'expected_fingerprint': by_alias['snapshot']['fingerprint']})
    assert changed.value.code == 'snapshot_changed'
    missing = lab.invoke(viewer, 'query.sample.context', {'project_id': 'P1', 'sample_label': 'missing'})['result']
    assert missing['status'] == 'not_found'
    with pytest.raises(capabilities.CapabilityError) as error:
        lab.invoke(viewer, 'query.sample.context', {'project_id': 'P2', 'sample_label': 'A'})
    assert error.value.code == 'forbidden_scope'


def test_budget_partial_is_explicit_whole_records_and_measured(context_lab):
    small = fetch(context_lab, budget={'max_bytes': 4096})
    assert small['status'] == 'partial' and small['coverage']['retrieval_complete'] is False
    assert small['coverage']['omitted']
    assert small['coverage']['result_bytes'] == len(json.dumps(small, ensure_ascii=False, separators=(',', ':')).encode()) <= 4096
    lab, _, viewer, *_ = context_lab
    expansion = small['coverage']['omitted'][0]['expand']
    full = lab.invoke(viewer, expansion['capability'], expansion['params'])['result']
    assert full['status'] == 'complete'
    assert full['snapshot']['fingerprint'] == small['snapshot']['fingerprint']
    assert all('fields' in event and 'evidence_refs' in event for event in small['events'])


def test_payload_extensions_and_incomplete_ref_are_preserved_without_guessing(context_lab):
    lab, owner, _, page, _, _, _, proposal, *_ = context_lab
    event_id = proposal['items'][1]['event_id']
    lab.invoke(owner, 'memory.event.correct', {'event_id': event_id, 'expected_revision': 1,
        'changes': {'details': {'instrument_result': [1, 2, None]}}, 'reason': 'Keep research extension'}, operation_id='extension')
    rows = lab.rows('SELECT revision_id,evidence_refs_json FROM event_revisions WHERE entity_id=? ORDER BY revision DESC LIMIT 1', (event_id,))
    refs = json.loads(rows[0]['evidence_refs_json'])
    refs[0].pop('block_id')
    lab.execute('UPDATE event_revisions SET evidence_refs_json=? WHERE revision_id=?', (json.dumps(refs), rows[0]['revision_id']))
    result = fetch(context_lab)
    assert next(e for e in result['events'] if e['event_id'] == event_id)['details']['instrument_result'] == [1, 2, None]
    assert result['status'] == 'partial'
    page_context = next(e for e in result['source_context'] if e.get('kind') == 'page_context')
    assert page_context['transcript_revision'] == 1 and page_context['blocks']
    assert page_context['relation'] == 'source_context_not_sample_facts'


def test_fingerprint_detects_new_unassigned_record_and_source_context_only_errors(context_lab):
    lab, _, _, page, *_ = context_lab
    before = fetch(context_lab)
    lab.add_event(project_id='P1', raw_text='new unassigned', evidence_refs=[{'page_id': page['page_id']}])
    after = fetch(context_lab)
    assert after['quality']['unassigned_records_on_linked_pages'] > before['quality']['unassigned_records_on_linked_pages']
    assert after['snapshot']['fingerprint'] != before['snapshot']['fingerprint']
    event_id = after['events'][0]['event_id']
    lab.execute('UPDATE event_revisions SET evidence_refs_json=? WHERE entity_id=?',
        (json.dumps([{'page_id': page['page_id'], 'transcript_revision': 999}]), event_id))
    failed = fetch(context_lab, include=['source_context'])
    assert failed['status'] == 'partial' and failed['coverage']['retrieval_complete'] is False
    assert any(issue['kind'] == 'unreadable_source_context' for issue in failed['quality']['issues'])


def test_http_client_and_mcp_deliver_same_context_and_declared_schema(context_lab):
    lab, _, viewer, _, _, _, _, _, _, ids = context_lab
    conn = db.connect(lab.db_path)
    key = 'test-only-external-context-secret'
    auth.register_credential(conn, credential_id='context-test', principal_id=viewer.principal_id, secret=key)
    conn.commit(); conn.close()
    server = create_api_server(lab.db_path, port=0, service_settings=lab.settings)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        client = clients.ReadOnlyQueryClient(f'http://127.0.0.1:{server.server_port}', key)
        http = client.get_sample_context('P1', sample_id=ids['A'])
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'query.sample.context', 'arguments': {'project_id': 'P1', 'sample_id': ids['A']}}}
        request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v2/mcp',
            data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
        with urllib.request.urlopen(request, timeout=10) as response:
            rpc = json.load(response)
        envelope = rpc['result']['structuredContent']
        assert not rpc['result']['isError']
        validate_payload_against_schema(envelope, READ_OUTPUT_SCHEMAS['query.sample.context'])
        assert http['snapshot']['fingerprint'] == envelope['result']['snapshot']['fingerprint']
        assert http['events'] == envelope['result']['events']
        catalog = client.list_samples(project_ids=['P1'], limit=1)
        assert len(catalog['samples']) == 1 and catalog['has_more'] and 'records' not in catalog['samples'][0]
        document = client.read_page(document_id=context_lab[3]['document_id'])
        assert document['pages'][0]['view'] == 'agent'
        assert 'storage_path' not in json.dumps(document)
        assert server.server_address[0] == '127.0.0.1'
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
