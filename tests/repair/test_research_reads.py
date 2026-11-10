"""External AI research sources remain exact, scoped and compact."""
import json

import pytest

from chemclaw_lab import auth, db, query, research_reads as reads


def _call(lab, context, method, params):
    conn = db.connect(lab.db_path)
    try:
        return method(conn, context, params)
    finally:
        conn.close()


def test_exact_transcript_revision_ignores_failures_timestamps_and_run_names(lab):
    ctx = lab.context('research-reader', memberships={'P1': 'viewer'})
    page = lab.add_page('P1')['page_id']
    first = lab.add_transcript(page, ['original text'], run_id='opaque_z999', created_at='2030-01-01')
    lab.execute("INSERT INTO transcript_runs(run_id,page_id,provider,status,input_sha256) VALUES('failed_0002',?,'fixture','failed','x')", (page,))
    second = lab.add_transcript(page, ['corrected text'], run_id='opaque_a001', created_at='2020-01-01')
    # Activated needs_human runs are real revisions, failed attempts are not.
    lab.execute("UPDATE transcript_runs SET status='needs_human' WHERE run_id=?", (second['run_id'],))
    old = _call(lab, ctx, reads.read_page, {'page_id': page, 'transcript_revision': 1})['page']['transcript']
    current = _call(lab, ctx, reads.read_page, {'page_id': page})['page']['transcript']
    assert old['run_id'] == first['run_id']
    assert old['transcript_revision'] == 1 and old['active_revision'] == 2
    assert old['blocks'][0]['text'] == 'original text'
    assert not old['is_current']
    assert current['run_id'] == second['run_id'] and current['transcript_revision'] == 2
    assert current['blocks'][0]['text'] == 'corrected text'
    with pytest.raises(query.QueryError) as exc:
        _call(lab, ctx, reads.read_page, {'page_id': page, 'transcript_revision': 3})
    assert exc.value.code == 'transcript_revision_not_found'


def test_missing_block_never_falls_back_to_latest_and_agent_keeps_quality(lab):
    ctx = lab.context('research-reader', memberships={'P1': 'viewer'})
    page = lab.add_page('P1')['page_id']
    old = lab.add_transcript(page, ['old'])
    new = lab.add_transcript(page, ['new'])
    block = {'text': 'old', 'quality_flags': ['uncertain'], 'geometry': {'x': 1}, 'storage_path': 'secret'}
    lab.execute('UPDATE transcript_blocks SET block_json=? WHERE block_id=?', (json.dumps(block), old['block_ids'][0]))
    compact = _call(lab, ctx, reads.read_page, {'page_id': page, 'transcript_revision': 1, 'block_ids': old['block_ids']})
    actual = compact['page']['transcript']['blocks'][0]
    assert actual['quality_flags'] == ['uncertain'] and actual['text'] == 'old'
    assert 'geometry' not in actual and 'storage_path' not in json.dumps(compact)
    full = _call(lab, ctx, reads.read_page, {'page_id': page, 'transcript_revision': 1, 'view': 'full'})
    assert full['page']['transcript']['blocks'][0]['geometry'] == {'x': 1}
    assert 'storage_path' not in json.dumps(full)
    with pytest.raises(query.QueryError) as exc:
        _call(lab, ctx, reads.read_page, {'page_id': page, 'transcript_revision': 1, 'block_ids': new['block_ids']})
    assert exc.value.code == 'source_block_not_found'


def test_page_scope_and_corrupt_mapping_fail_closed(lab):
    ctx = lab.context('research-reader', memberships={'P1': 'viewer'})
    outside = lab.add_page('P2')['page_id']
    with pytest.raises(auth.AuthError):
        _call(lab, ctx, reads.read_page, {'page_id': outside})
    page = lab.add_page('P1')['page_id']
    lab.add_transcript(page, ['text'])
    lab.execute('UPDATE page_transcripts SET active_revision=2 WHERE page_id=?', (page,))
    with pytest.raises(query.QueryError) as exc:
        _call(lab, ctx, reads.read_page, {'page_id': page})
    assert exc.value.code == 'transcript_revision_unavailable'


def test_event_revision_asof_and_history_select_exact_facts(lab):
    ctx = lab.context('research-reader', memberships={'P1': 'viewer'})
    page = lab.add_page('P1')['page_id']
    event = lab.add_event(project_id='P1', raw_text='old', knowledge_as_of='2026-01-01 00:00:00')
    lab.add_event(project_id='P1', raw_text='new', revision=2, event_id=event, knowledge_as_of='2026-02-01 00:00:00')
    current = _call(lab, ctx, reads.read_event, {'event_id': event})
    exact = _call(lab, ctx, reads.read_event, {'event_id': event, 'revision': 1})
    historical = _call(lab, ctx, reads.read_event, {'event_id': event, 'knowledge_as_of': '2026-01-02 00:00:00'})
    history = _call(lab, ctx, reads.read_event, {'event_id': event, 'include_history': True})
    assert current['field_json']['raw_text'] == 'new'
    assert json.loads(current['provenance_json']) == current['provenance']
    assert json.loads(current['evidence_refs_json']) == current['evidence_refs']
    assert exact['field_json']['raw_text'] == historical['field_json']['raw_text'] == 'old'
    assert exact['current_revision'] == 2 and not exact['is_current']
    assert [r['revision'] for r in history['revisions']] == [1, 2]
    assert exact['field_json']['page_id'] == page
    with pytest.raises(query.QueryError) as exc:
        _call(lab, ctx, reads.read_event, {'event_id': event, 'knowledge_as_of': '2025-01-01'})
    assert exc.value.code == 'event_revision_not_found'


@pytest.mark.parametrize('selectors', [
    {'revision': 1, 'include_history': True},
    {'knowledge_as_of': '2026-01-01', 'revision': 1},
    {'knowledge_as_of': '2026-01-01', 'include_history': True},
    {'revision': True}, {'include_history': 'false'},
])
def test_event_selectors_are_strict(lab, selectors):
    ctx = lab.context('research-reader', memberships={'P1': 'viewer'})
    with pytest.raises(query.QueryError) as exc:
        _call(lab, ctx, reads.read_event, {'event_id': 'any', **selectors})
    assert exc.value.code == 'schema_violation'


def test_event_current_ownership_and_historical_sources_both_require_permission(lab):
    ctx = lab.context('research-reader', memberships={'P1': 'viewer'})
    lab.add_page('P1')
    lab.add_page('P2')
    event = lab.add_event(project_id='P2', raw_text='previous')
    lab.add_event(project_id='P1', event_id=event, revision=2, raw_text='current')
    assert _call(lab, ctx, reads.read_event, {'event_id': event})['revision'] == 2
    with pytest.raises(auth.AuthError):
        _call(lab, ctx, reads.read_event, {'event_id': event, 'revision': 1})
    with pytest.raises(auth.AuthError):
        _call(lab, ctx, reads.read_event, {'event_id': event, 'include_history': True})
    only_old = lab.context('old-reader', memberships={'P2': 'viewer'})
    with pytest.raises(auth.AuthError):
        _call(lab, only_old, reads.read_event, {'event_id': event, 'revision': 1})


def test_event_cross_project_reference_or_false_project_is_refused(lab):
    ctx = lab.context('research-reader', memberships={'P1': 'viewer', 'P2': 'viewer'})
    lab.add_page('P1')
    outside = lab.add_page('P2')['page_id']
    event = lab.add_event(project_id='P1', evidence_refs=[{'page_id': outside, 'transcript_revision': 1}])
    with pytest.raises(query.QueryError) as exc:
        _call(lab, ctx, reads.read_event, {'event_id': event})
    assert exc.value.code == 'forbidden_scope'
    event = lab.add_event(project_id='P1')
    lab.execute("UPDATE event_revisions SET field_json=json_set(field_json,'$.project_id','P2') WHERE entity_id=?", (event,))
    with pytest.raises(query.QueryError) as exc:
        _call(lab, ctx, reads.read_event, {'event_id': event})
    assert exc.value.code == 'forbidden_scope'


def test_catalog_candidates_aliases_scope_and_stable_pagination(lab):
    ctx = lab.context('research-reader', memberships={'P1': 'viewer'})
    for sample_id, project, label, aliases in [('b', 'P1', 'S1', ['blue']), ('a', 'P1', 'S1', ['BLUE-2']), ('c', 'P1', 'Other', ['S1']), ('hidden', 'P2', 'S1', [])]:
        lab.execute('INSERT INTO samples(sample_id,project_id,label,aliases_json) VALUES(?,?,?,?)', (sample_id, project, label, json.dumps(aliases)))
    first = _call(lab, ctx, reads.catalog_samples, {'project_ids': ['P1'], 'q': 'S1', 'limit': 2})
    assert [s['sample_id'] for s in first['samples']] == ['a', 'b']
    assert all(s['match'] == 'exact' for s in first['samples'])
    assert first['has_more'] and first['next_offset'] == 2 and first['total'] == 3
    second = _call(lab, ctx, reads.catalog_samples, {'q': 'S1', 'limit': 2, 'offset': first['next_offset']})
    assert [s['sample_id'] for s in second['samples']] == ['c']
    assert not second['has_more'] and second['next_offset'] is None
    assert set(second['samples'][0]) == {'sample_id', 'project_id', 'label', 'aliases', 'match'}
    substring = _call(lab, ctx, reads.catalog_samples, {'q': 'blue'})
    assert [s['match'] for s in substring['samples']] == ['substring', 'exact']
    with pytest.raises(query.QueryError) as exc:
        _call(lab, ctx, reads.catalog_samples, {'project_ids': ['P2']})
    assert exc.value.code == 'forbidden_scope'


@pytest.mark.parametrize('params', [{'limit': 0}, {'limit': 201}, {'limit': True}, {'offset': -1}, {'offset': 1.2}, {'project_ids': 'P1'}])
def test_catalog_validates_paging(lab, params):
    ctx = lab.context('research-reader', memberships={'P1': 'viewer'})
    with pytest.raises(query.QueryError) as exc:
        _call(lab, ctx, reads.catalog_samples, params)
    assert exc.value.code == 'schema_violation'


@pytest.mark.parametrize('cutoff', ['2026-10-23T08:00:00Z', '2026-10-23T16:00:00+08:00', '2026-10-23T04:00:00-04:00'])
@pytest.mark.parametrize('future_time', ['2026-10-23 09:00:00', '2026-10-23T09:00:00Z', '2026-10-23T17:00:00+08:00'])
def test_event_asof_compares_instants_across_timestamp_formats(lab, cutoff, future_time):
    ctx = lab.context('research-time-reader', memberships={'P1': 'viewer'})
    lab.add_page('P1')
    event = lab.add_event(project_id='P1', raw_text='known at seven', knowledge_as_of='2026-10-23 07:00:00')
    lab.add_event(project_id='P1', event_id=event, revision=2, raw_text='known at nine', knowledge_as_of=future_time)
    result = _call(lab, ctx, reads.read_event, {'event_id': event, 'knowledge_as_of': cutoff})
    assert result['revision'] == 1
    assert result['field_json']['raw_text'] == 'known at seven'
    assert _call(lab, ctx, reads.read_event, {'event_id': event})['revision'] == 2
    assert _call(lab, ctx, reads.read_event, {'event_id': event, 'knowledge_as_of': '2026-10-23T09:00:00Z'})['revision'] == 2


def test_event_asof_null_knowledge_time_remains_earliest_and_invalid_cutoff_is_rejected(lab):
    ctx = lab.context('research-time-reader', memberships={'P1': 'viewer'})
    lab.add_page('P1')
    event = lab.add_event(project_id='P1', raw_text='undated')
    lab.execute('UPDATE event_revisions SET knowledge_as_of=NULL WHERE entity_id=?', (event,))
    lab.add_event(project_id='P1', event_id=event, revision=2, knowledge_as_of='2026-10-23 09:00:00')
    assert _call(lab, ctx, reads.read_event, {'event_id': event, 'knowledge_as_of': '1900-01-01T00:00:00Z'})['revision'] == 1
    with pytest.raises(query.QueryError) as exc:
        _call(lab, ctx, reads.read_event, {'event_id': event, 'knowledge_as_of': 'not-a-timestamp'})
    assert exc.value.code == 'schema_violation'
