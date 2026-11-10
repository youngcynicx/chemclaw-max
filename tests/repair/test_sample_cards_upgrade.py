"""Knowledge card projection and human correction bridge boundaries."""
import json
import sys
from pathlib import Path

import pytest

from chemclaw_lab import db, experiments, query
from chemclaw_lab.sample_cards import enrich_samples

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from frontend import dev_server as bridge


def test_deduplicated_cards_keep_each_event_revision_for_readback(lab):
    page = lab.add_page('P1')
    transcript = lab.add_transcript(page['page_id'], ['A measured 2 units'])
    item = {'item_key': 'measurement', 'event_kind': 'observation',
        'raw_text': 'A measured 2 units', 'fields': {'sample_label': {'normalized': 'A'}},
        'evidence_refs': [{'page_id': page['page_id'], 'transcript_revision': 1,
                           'block_id': transcript['block_ids'][0]}]}
    conn = db.connect(lab.db_path)
    try:
        events = []
        for operation in ('import-one', 'import-two'):
            result = experiments.create_proposal(conn, project_id='P1', page_id=page['page_id'],
                transcript_revision=1, profile_id='chemistry_notebook', profile_version='1.0',
                created_by='human', operation_id=operation, items=[item])
            events.append(result['proposal']['items'][0]['event_id'])
        experiments.correct_event(conn, event_id=events[1], expected_revision=1,
            changes={'raw_text': item['raw_text']}, reason='reaffirm source', principal_id='human')
        result = enrich_samples(conn, [dict(row) for row in conn.execute('SELECT * FROM samples')], ['P1'])
        records = result['samples'][0]['records']
        assert len(records) == 1
        assert set(records[0]['related_event_ids']) == set(events)
        assert {ref['event_id']: ref['revision'] for ref in records[0]['related_events']} == {
            events[0]: 1, events[1]: 2}
        assert enrich_samples(conn, [], ['P2'])['samples'] == []
    finally:
        conn.close()


@pytest.mark.parametrize('future_time', ['2026-10-24', '2026-10-24T10:00:00+08:00', '2026-10-24 10:00:00'])
def test_shared_timeline_matches_card_links_and_preserves_revision_sources(lab, future_time):
    context = lab.context('reader', memberships={'P1': 'member'})
    outsider = lab.context('outsider', memberships={'P2': 'member'})
    page = lab.add_page('P1')
    transcript = lab.add_transcript(page['page_id'], ['A and B share a future plan', 'A and B completed preparation'])
    conn = db.connect(lab.db_path)
    try:
        refs = [{'page_id': page['page_id'], 'transcript_revision': 1, 'block_id': transcript['block_ids'][0]}]
        def item(key, scope, date, kind='plan'):
            return {'item_key': key, 'event_kind': kind, 'raw_text': key,
                'occurred_at': date, 'unknowns': ['result not measured'], 'evidence_refs': refs,
                'fields': {'record_scope': scope, 'related_sample_labels': [
                    {'normalized': 'A'}, {'normalized': 'B'}]}}
        proposal = experiments.create_proposal(conn, project_id='P1', page_id=page['page_id'],
            transcript_revision=1, profile_id='chemistry_notebook', profile_version='1.0',
            created_by='human', operation_id='shared-timeline', items=[
                item('future', 'sample_knowledge', future_time),
                item('earlier', 'sample_knowledge', '2026-10-21', 'operation'),
                item('notice', 'source_metadata', None)])['proposal']
        future, earlier, _ = [i['event_id'] for i in proposal['items']]
        conn.execute("UPDATE event_revisions SET knowledge_as_of='2026-10-21 12:00:00'")
        samples = [dict(r) for r in conn.execute('SELECT * FROM samples')]
        cards = enrich_samples(conn, samples, ['P1'])['samples']
        for card in cards:
            timeline = query.sample_timeline(conn, context, sample_id=card['sample_id'])['timeline']
            assert [e['event_id'] for e in timeline] == [earlier, future]
            assert set(e['event_id'] for e in timeline) == set(e['event_id'] for e in card['records'])
            assert timeline[1]['evidence_refs'] == refs
            assert timeline[1]['occurred_at'] == timeline[1]['event_time_start'] == future_time
            assert timeline[1]['unknowns'] == ['result not measured']
            assert timeline[1]['provenance']['acceptance'] == 'default'
        sample_a = next(s for s in samples if s['label'] == 'A')['sample_id']
        sample_b = next(s for s in samples if s['label'] == 'B')['sample_id']
        def search_shared(**kwargs):
            return query.search(conn, context, project_ids=['P1'], sample_label='B',
                date_from='2026-10-24', date_to='2026-10-24', **kwargs)['candidates']
        assert [e['object_id'] for e in search_shared()] == [future]
        conn.execute("UPDATE samples SET aliases_json='[\"Bee\"]' WHERE sample_id=?", (sample_b,))
        alias_hits = query.search(conn, context, project_ids=['P1'], sample_label='Bee',
            date_from='2026-10-24', date_to='2026-10-24')['candidates']
        assert [e['object_id'] for e in alias_hits] == [future]
        experiments.correct_event(conn, event_id=future, expected_revision=1,
            changes={'sample_id': sample_a, 'sample_ids': [sample_a]}, reason='only A', principal_id='human')
        conn.execute("UPDATE event_revisions SET knowledge_as_of='2026-10-22 12:00:00' WHERE revision=2")
        current_a = query.sample_timeline(conn, context, sample_id=sample_a)['timeline'][-1]
        assert current_a['revision'] == 2 and current_a['is_corrected'] is True
        assert [e['event_id'] for e in query.sample_timeline(conn, context, sample_id=sample_b)['timeline']] == [earlier]
        assert search_shared() == []
        assert [e['object_id'] for e in search_shared(knowledge_as_of='2026-10-21 18:00:00')] == [future]
        past = query.sample_timeline(conn, context, sample_id=sample_b, knowledge_as_of='2026-10-21 18:00:00')
        assert [(e['event_id'], e['revision']) for e in past['timeline']] == [(earlier, 1), (future, 1)]
        with pytest.raises(query.QueryError):
            query.sample_timeline(conn, outsider, sample_id=sample_a)
    finally:
        conn.close()


def test_cards_follow_latest_subjects_and_keep_shared_sources(lab):
    context = lab.context('human', memberships={'P1':'member'})
    page = lab.add_page('P1')
    transcript = lab.add_transcript(page['page_id'], ['A and B were observed'])
    conn = db.connect(lab.db_path)
    try:
        result = experiments.create_proposal(conn, project_id='P1',page_id=page['page_id'],transcript_revision=1,
            profile_id='chemistry_notebook', profile_version='1.0', created_by='human', operation_id='cards', items=[{
            'item_key':'one','event_kind':'observation','raw_text':'A and B were observed',
            'fields':{'sample_label':{'normalized':'A'},'related_sample_labels':[{'normalized':'B'}],
                      'source_kind':'simulated_record','source_notice':'模拟实验资料'},
            'evidence_refs':[{'page_id':page['page_id'],'transcript_revision':1,'block_id':transcript['block_ids'][0]}]}])
        event = result['proposal']['items'][0]['event_id']
        def cards():
            return enrich_samples(conn,[dict(r) for r in conn.execute('SELECT * FROM samples')],['P1'])
        initial = cards()
        assert [s['event_count'] for s in initial['samples']] == [1,1]
        assert all(s['records'][0]['fields']['source_notice']=='模拟实验资料' for s in initial['samples'])
        experiments.correct_event(conn,event_id=event,expected_revision=1,changes={'sample_id':None,'sample_ids':[]},reason='detach',principal_id='human')
        detached = cards()
        assert all(not s['records'] for s in detached['samples'])
        assert len(detached['unassigned_records']) == 1  # Old field label cannot reattach an explicit correction.
        experiments.correct_event(conn,event_id=event,expected_revision=2,changes={'fields':{'record_scope':'source_metadata'}},reason='source note',principal_id='human')
        assert len(cards()['source_notes'])==1
        assert not cards()['unassigned_records']
        conn.commit()
    finally:
        conn.close()
    lab.add_transcript(page['page_id'],['new source'])
    conn=db.connect(lab.db_path)
    try:
        result=enrich_samples(conn,[],['P1'])
        assert result['source_notes'][0]['revision']==3
        assert result['source_notes'][0]['source_stale'] is True
        assert enrich_samples(conn,[],['P2'])['source_notes']==[]
    finally:
        conn.close()


def test_correction_bridge_preserves_other_blocks_and_rejects_wrong_project(monkeypatch):
    connection=bridge.CoreConnection('http://127.0.0.1:8876','unused','P1')
    blocks=[{'block_id':'a','block_kind':'paragraph','reading_order':0,'text':'original',
             'geometry':None,'original_geometry':{'untrusted':'derived only'}},
            {'block_id':'b','block_kind':'paragraph','reading_order':1,'text':'keep unchanged'}]
    page={'page_id':'pg','project_id':'P1','transcript':{'active_revision':3,'blocks':blocks}}
    monkeypatch.setattr(bridge,'invoke_read',lambda *a,**k: page)
    writes=[]
    monkeypatch.setattr(bridge,'invoke_internal_write',lambda c,capability,params: writes.append((capability,params)) or {'ok':True})
    body={'pageId':'pg','expectedRevision':3,'reason':'correct digit','corrections':[{'block_id':'a','text':'corrected'}]}
    assert bridge.invoke_knowledge_correction(connection,body,transcript=True)=={'ok':True}
    sent=writes[0][1]['blocks']
    assert sent[0]['text']=='corrected' and 'original_geometry' not in sent[0]
    assert sent[1]['text']==blocks[1]['text']
    assert all(new['block_id']!=old['block_id'] for new,old in zip(sent,blocks))
    assert blocks[0]['text']=='original'
    for changed in ({'expectedRevision':2},{'corrections':[{'block_id':'outside','text':'bad'}]}):
        with pytest.raises(bridge.BridgeError):
            bridge.invoke_knowledge_correction(connection,{**body,**changed},transcript=True)
    page['project_id']='P2'
    with pytest.raises(bridge.BridgeError) as exc:
        bridge.invoke_knowledge_correction(connection,body,transcript=True)
    assert exc.value.status==403
    assert len(writes)==1


def test_transcript_correction_survives_real_immutable_block_storage(lab,monkeypatch):
    from chemclaw_lab import transcribe
    context=lab.context('human',memberships={'P1':'member'})
    page=lab.add_page('P1')
    blocks=[{'block_id':f'original-{index}','block_kind':'paragraph','reading_order':index,'text':text,
        'coordinate_system':None,'source_page':1,'extractor':{'provider':'fixture','model':None,'version':'1'}}
        for index,text in enumerate(['dose 1 g','retain 2 g'])]
    conn=db.connect(lab.db_path)
    try:
        transcribe.import_transcript(conn,page_id=page['page_id'],blocks=blocks,provider='fixture',model=None,version='1')
        conn.commit()
    finally:conn.close()
    def read(c,cap,params):
        conn=db.connect(lab.db_path)
        try:return {'project_id':'P1','transcript':transcribe.get_page_transcript(conn,page_id=page['page_id'])}
        finally:conn.close()
    monkeypatch.setattr(bridge,'invoke_read',read)
    monkeypatch.setattr(bridge,'invoke_internal_write',lambda c,cap,params:lab.invoke(context,cap,params,operation_id='correct-transcript')['result'])
    result=bridge.invoke_knowledge_correction(bridge.CoreConnection('http://127.0.0.1:8876','unused','P1'),
        {'pageId':page['page_id'],'expectedRevision':1,'reason':'correct digit',
         'corrections':[{'block_id':'original-0','text':'dose 3 g'}]},transcript=True)
    assert [b['text'] for b in result['blocks']]==['dose 3 g','retain 2 g']
    assert lab.scalar("SELECT count(*) FROM transcript_runs")==2
    assert lab.scalar("SELECT count(*) FROM transcript_blocks")==4
    assert lab.scalar("SELECT json_extract(block_json,'$.text') FROM transcript_blocks WHERE block_id='original-0'")=='dose 1 g'
    assert lab.scalar('SELECT active_revision FROM page_transcripts')==2


def test_manual_extraction_queues_existing_agent_with_stable_page_revision(monkeypatch):
    connection=bridge.CoreConnection('http://127.0.0.1:8876','unused','P1')
    page={'page_id':'pg_one','project_id':'P1','mime':'image/png','transcript':{'active_revision':4}}
    monkeypatch.setattr(bridge,'invoke_read',lambda *a,**k:page)
    jobs=[]
    monkeypatch.setattr(bridge,'invoke_job',lambda c,op,**params:jobs.append((op,params)) or {'job':{'job_id':'retained'}})
    result=bridge.queue_page_knowledge(connection,'pg_one')
    assert result['job']['job_id']=='retained'
    assert jobs[0][1]['request_id']=='pageknowledge:pg_one:r4'
    assert jobs[0][1]['context']['pages']==[{'page_id':'pg_one','mime':'image/png'}]
    assert jobs[0][1]['attachments_manifest']==[]  # Reuse the saved transcript, with no new upload/OCR.
    page['project_id']='P2'
    with pytest.raises(bridge.BridgeError):bridge.queue_page_knowledge(connection,'pg_one')
    assert len(jobs)==1
