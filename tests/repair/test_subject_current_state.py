"""Subject ownership and explicit state transitions agree across read surfaces."""
import pytest


@pytest.fixture()
def state_lab(lab):
    owner = lab.context('state_writer', memberships={'P1': 'member'})
    viewer = lab.context('state_reader', kind='service', memberships={'P1': 'viewer'})
    def save(text, key, fields, kind='observation'):
        page = lab.add_page('P1')
        transcript = lab.add_transcript(page['page_id'], [text])
        value = lab.invoke(owner, 'memory.proposal.create', {
            'project_id': 'P1', 'page_id': page['page_id'], 'transcript_revision': 1,
            'profile_id': 'chemistry_notebook', 'operation_id': key, 'items': [{'item_key': key, 'event_kind': kind,
                'raw_text': text, 'fields': fields, 'occurred_at': '2026-10-20',
                'evidence_refs': [{'page_id': page['page_id'], 'transcript_revision': 1,
                                   'block_id': transcript['block_ids'][0]}]}]}, operation_id=key)
        return value['result']['proposal']['items'][0]['event_id']
    plan = save('Both A and B are scheduled for a measurement.', 'plan', {
        'related_sample_labels': ['A', 'B'], 'state': {'status': 'pending'}}, 'plan')
    queued = save('B is queued for that measurement.', 'queue', {
        'sample_label': 'B', 'state': {'status': 'pending'}})
    lab.execute("UPDATE event_revisions SET knowledge_as_of='2026-10-21 01:00:00'")
    ids = {row['label']: row['sample_id'] for row in lab.rows('SELECT label,sample_id FROM samples')}
    def context(label, **extra):
        return lab.invoke(viewer, 'query.sample.context', {'project_id': 'P1',
                          'sample_id': ids[label], **extra})['result']
    return lab, viewer, save, context, ids, plan, queued


def status(context, target):
    return next(state['status'] for state in context['current_state'] if state['event_id'] == target)


def test_mentions_do_not_assign_another_samples_number(state_lab):
    lab, viewer, save, context, ids, _, _ = state_lab
    event = save('B result is 11; A is mentioned only for context.', 'B-only', {
        'sample_label': 'B', 'mentioned_sample_labels': ['A'],
        'measurement': {'normalized': 11, 'unit': 'arbitrary'}})
    assert event not in {e['event_id'] for e in context('A')['events']}
    actual = next(e for e in context('B')['events'] if e['event_id'] == event)
    assert actual['subject_sample_ids'] == [ids['B']]
    assert actual['fields']['mentioned_sample_labels'] == ['A']
    cards = lab.invoke(viewer, 'query.samples.list', {'project_ids': ['P1']})['result']['samples']
    assert event not in {e['event_id'] for c in cards if c['label'] == 'A' for e in c['records']}


def test_partial_completion_then_supersession_preserves_history_and_all_views(state_lab):
    lab, viewer, save, context, ids, plan, queued = state_lab
    done_a = save('A completed its scheduled measurement.', 'A-done', {
        'sample_label': 'A', 'measurement': {'normalized': 10, 'unit': 'arbitrary'},
        'state_updates': [{'target_event_id': plan, 'target_revision': 1, 'status': 'completed',
                           'subject_sample_labels': ['A'], 'reason': 'A completion is reported here.'}]})
    lab.execute("UPDATE event_revisions SET knowledge_as_of='2026-10-21 02:00:00' WHERE entity_id=?", (done_a,))
    assert status(context('A'), plan) == 'completed'
    assert status(context('B'), plan) == 'pending'
    original = next(e for e in context('A')['events'] if e['event_id'] == plan)
    assert original['revision'] == 1 and original['fields']['state']['status'] == 'pending'
    assert original['lifecycle_state']['status'] == 'completed'
    done_b = save('B completed the queued measurement; A is unchanged.', 'B-done', {
        'sample_label': 'B', 'mentioned_sample_labels': ['A'],
        'measurement': {'normalized': 11, 'unit': 'arbitrary'},
        'state_updates': [{'target_event_id': target, 'target_revision': 1, 'status': state,
            'subject_sample_labels': ['B'], 'reason': 'B completed the previously queued work.'}
            for target, state in [(plan, 'completed'), (queued, 'superseded')]]})
    lab.execute("UPDATE event_revisions SET knowledge_as_of='2026-10-21 03:00:00' WHERE entity_id=?", (done_b,))
    assert status(context('B'), plan) == 'completed'
    assert status(context('B'), queued) == 'superseded'
    assert done_b not in {e['event_id'] for e in context('A')['events']}
    cards = lab.invoke(viewer, 'query.samples.list', {'project_ids': ['P1']})['result']['samples']
    for card in cards:
        ctx = context(card['label'])
        timeline = lab.invoke(viewer, 'query.sample.timeline', {'sample_id': card['sample_id']})['result']
        assert card['current_state'] == ctx['current_state'] == timeline['current_state']
    historical = context('B', snapshot={'mode': 'as_of', 'knowledge_as_of': '2026-10-21T02:30:00Z'})
    assert status(historical, plan) == status(historical, queued) == 'pending'
    only_states = context('B', include=['current_state'], budget={'max_bytes': 4096})
    assert only_states['status'] == 'complete' and only_states['events'] == []
    assert status(only_states, plan) == 'completed'
    assert only_states['coverage']['returned']['current_state'] == 2
