"""Explicit continuation boundaries against isolated Core; no external provider calls."""
import base64
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO/'src'), str(REPO/'frontend'), str(REPO/'scripts')]
from chemclaw_lab import auth, db
from chemclaw_lab.api import create_api_server
from chemclaw_lab.service_config import ServiceSettings
from configure_agent_runtime import configure, DEFAULT_ROLES
import dev_server as bridge


class ContinuationCursorTests(unittest.TestCase):
    def setUp(self):
        self.connection = bridge.CoreConnection('http://127.0.0.1:1', 'fixture', 'P1')
        self.source = {'job_id':'job_old','context':{'pages':[{'page_id':'p1','mime':'image/jpeg'}]},
            'result':{'capture_progress':{'target_page_ids':['p1'], 'pages':[{'page_id':'p1','transcript_revision':1,
                'linked_block_ids':['b1','b2']}], 'saved_event_refs':[{'event_id':'e1','revision':1}]}}}
        self.page = {'page_id':'p1','project_id':'P1','mime':'image/jpeg',
            'transcript':{'active_revision':1,'blocks':[{'block_id':'b1'},{'block_id':'b2'}]}}
        self.event = {'entity_id':'e1','revision':1,'field_json':{'project_id':'P1','page_id':'p1'},
            'evidence_refs':[{'page_id':'p1','transcript_revision':1,'block_id':'b1'}]}

    def call(self, page=None, event=None):
        def read(connection, capability, params):
            return {'page':page or self.page} if capability=='evidence.page.read' else event or self.event
        with patch.object(bridge,'invoke_read',side_effect=read) as calls:
            context=bridge.capture_continuation_context(self.connection,self.source)
        return context,calls

    def test_cursor_is_rebuilt_from_current_readbacks_not_previous_claims(self):
        context,calls=self.call()
        cursor=context['capture_continuation']['previous_progress']['pages'][0]
        self.assertEqual(cursor['linked_block_ids'],['b1'])
        self.assertEqual(cursor['unlinked_block_ids'],['b2'])
        self.assertIsNone(context['capture_continuation']['previous_progress']['knowledge_complete'])
        self.assertEqual(calls.call_count,2)

    def test_cancelled_receipt_without_its_full_journal_cannot_start_writable_continuation(self):
        self.source['status'] = 'cancelled'
        self.source['result'] = {'status': 'cancelled', 'text': 'Stopped.'}
        with patch.object(bridge, 'invoke_read') as read:
            with self.assertRaises(bridge.BridgeError) as caught:
                bridge.capture_continuation_context(self.connection, self.source)
        self.assertEqual(caught.exception.code, 'continuation_requires_review')
        read.assert_not_called()

    def test_sanitized_checkpoint_limit_receipt_does_not_prove_no_unknown_effects(self):
        self.source['result'] = {'status': 'partial', 'data': {'retained_checkpoints': {'run_ids': ['old-run']}}}
        with patch.object(bridge, 'invoke_read') as read:
            with self.assertRaises(bridge.BridgeError) as caught:
                bridge.capture_continuation_context(self.connection, self.source)
        self.assertEqual(caught.exception.code, 'continuation_requires_review')
        read.assert_not_called()

    def test_changed_transcript_invalidates_old_block_links_but_keeps_object_for_readback(self):
        page=copy.deepcopy(self.page);page['transcript']={'active_revision':2,'blocks':[{'block_id':'b-new'}]}
        context,_=self.call(page=page)
        continuation=context['capture_continuation'];cursor=continuation['previous_progress']['pages'][0]
        self.assertEqual(continuation['source_changed_page_ids'],['p1'])
        self.assertTrue(cursor['source_changed']);self.assertEqual(cursor['linked_block_ids'],[])
        self.assertEqual(cursor['unlinked_block_ids'],['b-new'])
        self.assertEqual(continuation['revalidated_event_refs'],[{'event_id':'e1','revision':1}])

    def test_incomplete_target_index_without_original_scope_cannot_silently_select_a_subset(self):
        self.source['context']['pages'] = []
        self.source['result']['capture_progress']['target_page_ids_complete'] = False
        with patch.object(bridge, 'invoke_read') as read:
            with self.assertRaises(bridge.BridgeError) as caught:
                bridge.capture_continuation_context(self.connection, self.source)
        self.assertEqual(caught.exception.code, 'continuation_scope_too_large')
        self.assertEqual(caught.exception.status, 409)
        read.assert_not_called()

    def test_authorized_foreign_project_page_or_event_cannot_enter_current_project(self):
        page=copy.deepcopy(self.page);page['project_id']='P2'
        with self.assertRaises(bridge.BridgeError) as caught:self.call(page=page)
        self.assertEqual(caught.exception.status,403)
        event=copy.deepcopy(self.event);event['field_json']['project_id']='P2'
        with self.assertRaises(bridge.BridgeError) as caught:self.call(event=event)
        self.assertEqual(caught.exception.status,403)

    def test_missing_prior_object_stops_before_a_new_write_attempt(self):
        def read(c,cap,args):
            if cap=='evidence.page.read':return {'page':self.page}
            raise bridge.BridgeError('not_found','private detail',404)
        with patch.object(bridge,'invoke_read',side_effect=read),self.assertRaises(bridge.BridgeError) as caught:
            bridge.capture_continuation_context(self.connection,self.source)
        self.assertEqual(caught.exception.code,'continuation_requires_review')
        self.assertNotIn('private detail',caught.exception.message)

    def test_bounded_previous_progress_remains_explicitly_incomplete(self):
        progress=self.source['result']['capture_progress'];progress['truncated']=True
        progress['saved_event_refs']=[{'event_id':f'e{i}','revision':1} for i in range(101)]
        def read(c,cap,args):
            return {'page':self.page} if cap=='evidence.page.read' else {**self.event,'entity_id':args['event_id']}
        with patch.object(bridge,'invoke_read',side_effect=read) as calls:
            context=bridge.capture_continuation_context(self.connection,self.source)
        seed=context['capture_continuation']['previous_progress']
        self.assertTrue(seed['truncated']);self.assertTrue(seed['previous_progress_truncated'])
        self.assertEqual(seed['candidate_event_refs_omitted_count'],1);self.assertEqual(calls.call_count,101)
        self.assertEqual(seed['target_page_ids'],['p1']);self.assertIsNone(seed['knowledge_complete'])

    def test_large_source_inventory_fits_context_without_dropping_target_pages(self):
        self.source['context']['pages']=[{'page_id':f'p{i}','mime':'image/jpeg','transcript_revision':1} for i in range(100)]
        self.source['result']={}
        def read(c,cap,args):
            return {'page':{'page_id':args['page_id'],'project_id':'P1','mime':'image/jpeg',
                'transcript':{'active_revision':1,'blocks':[{'block_id':f"{args['page_id']}-b{i}"} for i in range(150)]}}}
        with patch.object(bridge,'invoke_read',side_effect=read):context=bridge.capture_continuation_context(self.connection,self.source)
        seed=context['capture_continuation']['previous_progress']
        self.assertLess(len(json.dumps(context,ensure_ascii=False).encode()),60000)
        self.assertEqual(len(context['pages']),100);self.assertEqual(len(seed['target_page_ids']),100)
        self.assertTrue(seed['truncated']);self.assertTrue(any(not p['source_inventory_complete'] for p in seed['pages']))

    def test_mismatched_read_response_identity_never_becomes_cursor_evidence(self):
        event=copy.deepcopy(self.event);event['entity_id']='e-other'
        with self.assertRaises(bridge.BridgeError) as caught:self.call(event=event)
        self.assertEqual(caught.exception.code,'core_invalid_response')


class ContinuationCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='chemclaw_continue_')
        cls.database=Path(cls.tmp.name)/'isolated.db';db.init_db(cls.database)
        cls.key=secrets.token_urlsafe(24);cls.other_key=secrets.token_urlsafe(24)
        c=db.connect(cls.database)
        try:
            for project in ('proj_continue','proj_other'):
                c.execute('INSERT INTO projects(project_id,name) VALUES(?,?)',(project,'Continuation fixture'))
            for principal,key in [('researcher',cls.key),('other_researcher',cls.other_key)]:
                auth.register_principal(c,principal_id=principal,kind='human',display_name='Fixture')
                for project in ('proj_continue','proj_other'):
                    auth.grant_membership(c,principal_id=principal,project_id=project,role='member')
                auth.register_credential(c,credential_id=principal+'_key',principal_id=principal,secret=key)
            c.commit()
        finally:c.close()
        configure(cls.database,list(DEFAULT_ROLES))
        cls.core=create_api_server(str(cls.database),port=0,service_settings=ServiceSettings(storage_root=Path(cls.tmp.name)/'storage'))
        cls.connection=bridge.CoreConnection(f'http://127.0.0.1:{cls.core.server_port}',cls.key,'proj_continue')
        cls.model=bridge.ModelConfig('http://127.0.0.1:1/v1','offline-fixture','fixture-model-only')
        cls.node_patch=patch.object(bridge,'_pi_node',return_value='fixture-node');cls.node_patch.start()
        # Submission is tested separately from the real worker method below.
        cls.scheduler_patch=patch.object(bridge.AgentJobWorker,'register',return_value=None);cls.scheduler_patch.start()
        cls.front=bridge.BridgeServer(('127.0.0.1',0),REPO/'frontend/dist',default_connection=cls.connection,default_model=cls.model)
        for server in (cls.core,cls.front):threading.Thread(target=server.serve_forever,daemon=True).start()
        status,result,headers=cls.request('GET','/api/session')
        assert status==200,result
        cls.headers={'Cookie':headers['Set-Cookie'].split(';',1)[0],'Content-Type':'application/json',
            'Origin':f'http://127.0.0.1:{cls.front.server_port}','X-ChemClaw-Session':result['csrfToken']}

    @classmethod
    def tearDownClass(cls):
        for server in (cls.front,cls.core):server.shutdown();server.server_close()
        cls.scheduler_patch.stop();cls.node_patch.stop();cls.tmp.cleanup()

    @classmethod
    def request(cls,method,path,payload=None,headers=None):
        client=http.client.HTTPConnection('127.0.0.1',cls.front.server_port,timeout=10)
        try:
            client.request(method,path,None if payload is None else json.dumps(payload).encode(),headers or {})
            response=client.getresponse();return response.status,json.loads(response.read()),dict(response.getheaders())
        finally:client.close()

    def source(self,extra_result=None):
        raw=('Sample CONT-'+uuid.uuid4().hex+'; recorded value and unknown condition.').encode()
        staged=bridge.stage_agent_attachments(self.connection,self.model,[{'name':'note.txt','mime':'text/plain','data_b64':base64.b64encode(raw).decode()}])
        pages=[{**p,'transcript_revision':1} for p in staged['pages']]
        source=bridge.invoke_job(self.connection,'submit',request_id='source:'+uuid.uuid4().hex,text='Organize this isolated source.',
            attachments_manifest=[],context={'pages':pages,'object_refs':staged['object_refs']})['job']
        claim=bridge.invoke_job(self.connection,'claim',job_id=source['job_id'],worker_id='fixture_source',lease_seconds=60)
        return bridge.invoke_job(self.connection,'complete',job_id=source['job_id'],worker_id='fixture_source',claim_epoch=claim['started']['claim_epoch'],
            result={'status':'partial','text':'Source budget exhausted.','error_code':'run_timeout',**(extra_result or {})})['job']

    def post(self,source,**changes):
        payload={'requestId':'continuation:'+source['job_id'],'sessionId':source['session_id'],**changes}
        return self.request('POST',f"/api/core/jobs/{source['job_id']}/continue",payload,self.headers)

    def read(self,job_id):return bridge.invoke_job(self.connection,'read',job_id=job_id)['job']

    def business(self):
        c=db.connect(self.database)
        try:
            return {table:hashlib.sha256(json.dumps([list(r) for r in c.execute(f'SELECT * FROM {table} ORDER BY rowid')],ensure_ascii=False).encode()).hexdigest()
                for table in ('samples','event_revisions','memory_proposals','evidence_pages','page_transcripts','transcript_blocks')}
        finally:c.close()

    def execute(self,job,callback):
        worker=bridge.AgentJobWorker.__new__(bridge.AgentJobWorker)
        worker.worker_id='fixture_continue';worker.lock=threading.Lock();worker.contexts={self.connection:self.model}
        worker.active={};worker.stopping=threading.Event()
        claim=bridge.invoke_job(self.connection,'claim',job_id=job['job_id'],worker_id=worker.worker_id,lease_seconds=60)
        with patch.object(bridge,'run_pi_agent',side_effect=callback) as host,patch.object(bridge,'vision_ocr_page',side_effect=AssertionError('Continuation must not OCR')) as ocr:
            worker._execute(self.connection,self.model,claim)
        return self.read(job['job_id']),claim,host,ocr

    def test_new_job_is_unique_and_old_job_sources_ocr_and_knowledge_do_not_change(self):
        source=self.source();before=self.business()
        status,result,_=self.post(source);self.assertEqual(status,202,result)
        child=result['job'];self.assertNotEqual(child['job_id'],source['job_id']);self.assertEqual(child['session_id'],source['session_id'])
        status,again,_=self.post(source);self.assertEqual(status,202,again);self.assertEqual(child['job_id'],again['job']['job_id'])
        seen={}
        def host(c,model,started,message,context,**kwargs):
            seen.update(context=context,started=started,resume=kwargs.get('resume'))
            return {'status':'completed','text':'Existing source reused; fixture complete.'}
        finished,claim,host,ocr=self.execute(child,host)
        self.assertEqual(finished['status'],'completed');self.assertEqual(host.call_count,1);ocr.assert_not_called()
        self.assertNotEqual(claim['started']['run_id'],source['run_id'])
        self.assertNotIn(source['run_id'],seen['resume']['runs'])
        self.assertEqual(self.read(source['job_id']),source);self.assertEqual(self.business(),before)
        self.assertEqual(child['attachments_manifest'],[]);self.assertNotIn(self.key,json.dumps(seen['context']))

    def test_queued_version_change_is_refreshed_without_new_ocr(self):
        source=self.source();status,result,_=self.post(source);self.assertEqual(status,202,result)
        page_id=source['context']['pages'][0]['page_id']
        block=copy.deepcopy(bridge.invoke_read(self.connection,'evidence.page.read',{'page_id':page_id})['page']['transcript']['blocks'][0])
        for computed in ('original_geometry','positioning'):block.pop(computed,None)
        block.update(block_id='updated-block',text='Changed current source, do not reuse prior cursor.',reading_order=0)
        bridge.invoke_internal_write(self.connection,'evidence.transcript.import',{'page_id':page_id,'provider':'fixture','model':'offline',
            'version':'1','raw_output':'changed current source','needs_human':False,
            'blocks':[block]})
        seen={}
        def host(c,model,started,message,context,**kwargs):seen.update(context);return {'status':'completed','text':'Checked current revision.'}
        finished,_,_,ocr=self.execute(result['job'],host)
        self.assertEqual(finished['status'],'completed');ocr.assert_not_called()
        continuation=seen['capture_continuation'];self.assertEqual(continuation['source_changed_page_ids'],[page_id])
        self.assertEqual(continuation['previous_progress']['pages'][0]['transcript_revision'],2)
        self.assertEqual(continuation['previous_progress']['pages'][0]['linked_block_ids'],[])
        self.assertEqual(self.read(source['job_id']),source)

    def test_unconfirmed_write_blocks_continuation_without_new_job(self):
        source=self.source({'toolCalls':[{'name':'memory.proposal.create','state':'pending','result':{'action_status':'unknown'}}]})
        before=bridge.invoke_job(self.connection,'list')['jobs']
        status,result,_=self.post(source);self.assertEqual(status,409,result)
        self.assertEqual(result['error']['code'],'continuation_requires_review')
        self.assertEqual(bridge.invoke_job(self.connection,'list')['jobs'],before)

    def test_wrong_session_forged_cursor_and_another_principal_are_rejected(self):
        source=self.source()
        status,result,_=self.post(source,sessionId='another-session');self.assertEqual(status,403,result)
        status,result,_=self.post(source,capture_progress={'pages':[]});self.assertEqual(status,400,result)
        other=bridge.CoreConnection(self.connection.endpoint,self.other_key,'proj_continue')
        with self.assertRaises(bridge.BridgeError) as caught:
            bridge.submit_capture_continuation(other,{'requestId':'continuation:'+source['job_id'],'sessionId':source['session_id']},source['job_id'])
        self.assertEqual(caught.exception.status,404)
        self.assertEqual(self.read(source['job_id']),source)

    def test_concurrent_clicks_converge_on_one_core_job(self):
        source=self.source()
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies=list(pool.map(lambda _:self.post(source),range(2)))
        self.assertEqual([r[0] for r in replies],[202,202],replies)
        self.assertEqual(replies[0][1]['job']['job_id'],replies[1][1]['job']['job_id'])
        rows=bridge.invoke_job(self.connection,'list',request_id='continuation:'+source['job_id'])['jobs']
        self.assertEqual(len(rows),1);self.assertEqual(self.read(source['job_id']),source)

    def test_new_route_rejects_missing_browser_authority_and_wrong_project(self):
        source=self.source();payload={'requestId':'continuation:'+source['job_id'],'sessionId':source['session_id']}
        status,_,_=self.request('POST',f"/api/core/jobs/{source['job_id']}/continue",payload)
        self.assertIn(status,(401,403))
        other=bridge.CoreConnection(self.connection.endpoint,self.key,'proj_other')
        with self.assertRaises(bridge.BridgeError) as caught:
            bridge.submit_capture_continuation(other,payload,source['job_id'])
        self.assertEqual(caught.exception.status,404)
        self.assertFalse(bridge.invoke_job(self.connection,'list',request_id=payload['requestId'])['jobs'])

    def test_missing_queued_transcript_stops_without_model_or_ocr(self):
        source=self.source();status,result,_=self.post(source);self.assertEqual(status,202,result)
        page_id=source['context']['pages'][0]['page_id']
        c=db.connect(self.database)
        try:c.execute('DELETE FROM page_transcripts WHERE page_id=?',(page_id,));c.commit()
        finally:c.close()
        finished,_,host,ocr=self.execute(result['job'],lambda *a,**k: self.fail('No model without the saved transcript'))
        self.assertEqual(finished['status'],'partial');host.assert_not_called();ocr.assert_not_called()
        self.assertEqual(finished['result']['error_code'],'continuation_transcript_missing')

    def test_actual_node_core_checkpoint_and_next_explicit_job_keep_progress(self):
        source=self.source();before=self.business()
        status,result,_=self.post(source);self.assertEqual(status,202,result)
        real_host=bridge.run_pi_agent
        class ControlledProvider(BaseHTTPRequestHandler):
            calls=0
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length','0')))
                type(self).calls+=1
                if type(self).calls==1:
                    delta={'role':'assistant','tool_calls':[{'index':0,'id':'explicit-fixture','type':'function','function':{
                        'name':'agent.respond','arguments':json.dumps({'status':'partial','knowledge_intent':'organize',
                            'text':'Saved sources retained; this controlled boundary fixture writes no knowledge.'})}}]}
                    reason='tool_calls'
                else:delta={'role':'assistant','content':'Controlled turn finished.'};reason='stop'
                events=[{'id':'fixture-completion','object':'chat.completion.chunk','created':0,'model':'fixture-stream',
                    'choices':[{'index':0,'delta':delta,'finish_reason':None}]},
                    {'id':'fixture-completion','object':'chat.completion.chunk','created':0,'model':'fixture-stream',
                     'choices':[{'index':0,'delta':{},'finish_reason':reason}],
                     'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}]
                body=(''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n').encode()
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        provider=ThreadingHTTPServer(('127.0.0.1',0),ControlledProvider)
        threading.Thread(target=provider.serve_forever,daemon=True).start()
        self.addCleanup(provider.server_close);self.addCleanup(provider.shutdown)
        controlled_model=bridge.ModelConfig(f'http://127.0.0.1:{provider.server_port}/v1','fixture-stream','offline-loopback-only')
        def host(connection,model,started,message,context,**kwargs):
            return real_host(connection,controlled_model,started,message,context,**kwargs)
        from _node_runtime import find_node
        node = find_node()
        with patch.object(bridge,'_pi_node',return_value=node):
            finished,claim,_,ocr=self.execute(result['job'],host)
        self.assertEqual(finished['status'],'partial',finished['result'].get('error_code'))
        self.assertGreaterEqual(ControlledProvider.calls,1)
        progress=finished['result']['capture_progress']
        self.assertEqual(progress['version'],1);self.assertIsNone(progress['knowledge_complete'])
        self.assertEqual(progress['target_page_ids'],[source['context']['pages'][0]['page_id']])
        self.assertTrue(progress['pages'][0]['unlinked_block_ids'])
        c=db.connect(self.database)
        try:self.assertGreaterEqual(c.execute('SELECT count(*) FROM assistant_runtime_checkpoints WHERE run_id=?',(claim['started']['run_id'],)).fetchone()[0],1)
        finally:c.close()
        status,next_job,_=self.post(finished);self.assertEqual(status,202,next_job)
        self.assertNotEqual(next_job['job']['job_id'],finished['job_id'])
        self.assertEqual(next_job['job']['context']['capture_continuation']['source_job_id'],finished['job_id'])
        self.assertEqual(self.read(source['job_id']),source);self.assertEqual(self.business(),before)
        ocr.assert_not_called()


if __name__=='__main__':unittest.main()
