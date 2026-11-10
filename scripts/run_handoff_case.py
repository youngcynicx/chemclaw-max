"""Retained, text-only synthetic handoff pilot through the real Pi/Core path.

No benchmark answer file is loaded. --config supplies model credentials only;
all research data and identities are created below --run-dir. Model turns run
once and failures are retained. Multi initial pauses until continue-multi exists.
"""
from __future__ import annotations
import argparse
import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / 'src'), str(REPO / 'frontend'), str(REPO / 'scripts')]
from chemclaw_lab import auth, db
from chemclaw_lab.api import create_api_server
from chemclaw_lab.service_config import ServiceSettings
from configure_agent_runtime import configure
from create_external_reader import create_external_reader
from benchmark_notebook_images import public_export, atomic_save
import dev_server as bridge

ROLES = ('orchestrator', 'b_capture', 'b_query')
PROJECT = 'simulated_handoff'


def single_pi(connection, model, started, message, context):
    """Same production host/model/gateway, capture prompt and no delegate tool."""
    context = {**context, **bridge.session_source_context(connection, started, context)}
    timeout = started['manifest']['timeout_s']
    env = {**os.environ, 'GATEWAY_URL': connection.endpoint, 'AGENT_TOKEN': started['run_token'],
        'AGENT_ROLE': 'b_capture', 'PI_BASE_URL': bridge._normalize_model_base(model.base_url, bridge._model_protocol(model.protocol)),
        'PI_API_KEY': model.api_key, 'PI_MODEL': model.model,
        'PI_PROTOCOL': 'openai-responses' if model.protocol == 'openai-responses' else 'openai-completions',
        'PI_REASONING_FORMAT': model.reasoning_format, 'PI_MAX_TOKENS': str(model.max_tokens),
        'PI_CONTEXT_WINDOW': str(model.context_window), 'PI_RUN_TIMEOUT_MS': str(int(timeout * 1000)), 'PI_MOCK': '0'}
    env.pop('PI_REASONING_EFFORT', None)
    if model.reasoning_effort is not None:
        env['PI_REASONING_EFFORT'] = model.reasoning_effort
    process = subprocess.Popen([bridge._pi_node(), str(REPO / 'runtime/pi/server.mjs')],
        cwd=REPO / 'runtime/pi', env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding='utf-8')
    ready = queue.Queue()
    def read_port():
        for line in process.stdout:
            if line.startswith('PI_SERVER_PORT='):
                ready.put(int(line.strip().split('=', 1)[1])); return
        ready.put(None)
    threading.Thread(target=read_port, daemon=True).start()
    try:
        port = ready.get(timeout=20)
        if not port:
            raise RuntimeError('single Pi host did not start')
        body = {'session_id': started['session_id'], 'message': message,
            'history': started.get('history', []), 'context': context, 'durable': False}
        request = Request(f'http://127.0.0.1:{port}/run', data=json.dumps(body, ensure_ascii=False).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        with urlopen(request, timeout=timeout + 15) as response:
            return json.loads(response.read())
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)


class Condition:
    def __init__(self, root, kind, model, hidden, resume=False):
        self.root, self.kind, self.model, self.hidden = root / kind, kind, model, hidden
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'case.db'
        recovering = self.database.exists() and resume
        if self.database.exists() and not recovering:
            raise RuntimeError(f'{kind} database already exists; refusing to repeat model turns')
        if not recovering:
            db.init_db(self.database)
        self.host_key = secrets.token_urlsafe(32)
        hidden.append(self.host_key)
        conn = db.connect(self.database)
        try:
            if not recovering:
                conn.execute('INSERT INTO projects(project_id,name) VALUES(?,?)', (PROJECT, 'SIMULATED carbon handoff pilot'))
                auth.register_principal(conn, principal_id='simulated_researcher', kind='human', display_name='Simulated pilot operator')
                auth.grant_membership(conn, principal_id='simulated_researcher', project_id=PROJECT, role='member')
            elif conn.execute("SELECT count(*) FROM agent_runs WHERE status IN ('running','queued')").fetchone()[0]:
                raise RuntimeError('Refusing to resume while model runs remain active')
            auth.register_credential(conn, credential_id='pilot_host_'+secrets.token_hex(8), principal_id='simulated_researcher', secret=self.host_key)
            conn.commit()
        finally:
            conn.close()
        if not recovering:
            configure(self.database, list(ROLES))
        conn = db.connect(self.database)
        try:
            rows = conn.execute('SELECT agent_id,manifest_json FROM agent_manifests WHERE enabled=1').fetchall()
            manifests = {row['agent_id']: json.loads(row['manifest_json']) for row in rows}
            self.reachable = sorted(set().union(*(set(value['required_capabilities']) for value in manifests.values())))
            if kind == 'single' and not recovering:
                manifest = manifests['orchestrator']
                manifest['required_capabilities'] = self.reachable
                manifest['max_delegation_depth'] = 0
                conn.execute("UPDATE agent_manifests SET manifest_json=? WHERE agent_id='orchestrator'", (json.dumps(manifest),))
                conn.execute("UPDATE agent_manifests SET enabled=0 WHERE agent_id!='orchestrator'")
            conn.commit()
            self.manifests = [dict(row) for row in conn.execute('SELECT agent_id,manifest_json FROM agent_manifests WHERE enabled=1')]
        finally:
            conn.close()
        self.server = create_api_server(str(self.database), port=0, service_settings=ServiceSettings(storage_root=self.root / 'storage'))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{self.server.server_port}'
        self.connection = bridge.CoreConnection(url, self.host_key, PROJECT)
        reader_path = self.root / ('reader-'+secrets.token_hex(4)+'.json' if recovering else 'reader.json')
        create_external_reader(self.database, PROJECT, reader_path, url)
        hidden.append(json.loads(reader_path.read_text())['api_key'])
        self.session_id = None
        self.report = {'condition': kind, 'project_id': PROJECT, 'endpoint': url, 'reader_config': str(reader_path),
            'turns': [], 'reachable_capabilities': self.reachable, 'enabled_manifests': self.manifests,
            'runtime': 'production runtime/pi/server.mjs', 'system_prompt_role': 'b_capture' if kind == 'single' else 'orchestrator',
            'notes': 'Single uses the multi tree reachable capability union on its root, max delegation depth zero. Multi uses shipped three-role manifests. Same root 60 tool-call / 300 s ceiling; actual total model calls and child deadlines may differ.'}

        if recovering:
            self.report = json.loads((self.root / 'results.json').read_text(encoding='utf-8'))
            self.report['endpoint'] = url
            self.report['reader_config'] = str(reader_path)
            self.session_id = next((turn['session_id'] for turn in reversed(self.report['turns']) if turn.get('session_id')), None)
            self.report.setdefault('server_recoveries', []).append({'endpoint':url,'reader_config':str(reader_path)})

    def save(self):
        atomic_save(self.root / 'results.json', public_export(self.report, tuple(self.hidden)))

    def snapshot(self, stage):
        conn = db.connect(self.database)
        try:
            counts = {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                for table in ('samples','event_revisions','memory_proposals','event_confirmations','inventory_transactions','agent_runs')}
            target = sqlite3.connect(self.root / f'{stage}.db')
            try:
                conn.backup(target)
            finally:
                target.close()
            runs = [dict(row) for row in conn.execute('SELECT agent_id,status,used_tool_calls,budget_tool_calls FROM agent_run_tokens')]
        finally:
            conn.close()
        return {'counts': counts, 'agent_tokens': runs,
            'cards': bridge.invoke_read(self.connection, 'query.samples.list', {'project_ids':[PROJECT]})}

    def correct_mass(self, staged):
        cards = bridge.invoke_read(self.connection, 'query.samples.list', {'project_ids':[PROJECT]})['samples']
        sample = next(card for card in cards if card['label'] == 'CCX-A')
        def paths(value, path=()):
            if isinstance(value, dict):
                if value.get('normalized') == 36 and value.get('unit') == 'mg':
                    yield path
                for key, child in value.items():
                    yield from paths(child, path + (key,))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    yield from paths(child, path + (index,))
        choices = [(record, path) for record in sample['records'] for path in paths(record['fields'])]
        if len(choices) != 1:
            raise RuntimeError('Cannot uniquely identify simulated original 36 mg field; manual event/path selection is required')
        record, field_path = choices[0]
        page_id = staged['pages'][0]['page_id'] if isinstance(staged['pages'][0],dict) else staged['pages'][0]
        page = bridge.invoke_read(self.connection,'evidence.page.read',{'page_id':page_id,'view':'agent'})['page']
        refs = [{'page_id':page_id,'transcript_revision':page['active_revision'],'block_id':block['block_id']}
            for block in page['transcript']['blocks'] if '38 mg' in block['text']]
        if not refs:
            raise RuntimeError('Correction source block absent; refusing to guess')
        fields = json.loads(json.dumps(record['fields']))
        value = fields
        for key in field_path:
            value = value[key]
        value.update(normalized=38,raw='38 mg',unit='mg')
        params = {'event_id':record['event_id'],'expected_revision':record['revision'],
            'reason':'SIMULATED人工复核：初始质量36 mg录入错误，更正为38 mg，不是新称量',
            'changes':{'fields':fields,'raw_text':record['raw_text'].replace('36','38')},
            'correction_source':{'source_kind':'simulated_human_correction','evidence_refs':refs}}
        return {'selection':{'event_id':record['event_id'],'field_path':list(field_path),'basis':'unique currently stored value 36 mg for CCX-A; experiment-only human correction'},'request':params,'receipt':bridge.invoke_internal_write(self.connection,'memory.event.correct',params)}

    def turn(self, stage, source, prompt):
        prior = next((item for item in self.report['turns'] if item['stage'] == stage), None)
        if prior and prior.get('status') != 'harness_failed':
            return prior
        if prior and prior.get('run_id'):
            raise RuntimeError('A model was already started for the failed turn; do not rerun it automatically')
        if prior:
            self.report.setdefault('harness_recoveries', []).append(prior)
            self.report['turns'].remove(prior)
        start = time.monotonic()
        entry = {'stage':stage,'prompt':prompt,'source_text':source,'status':'running'}
        self.report['turns'].append(entry); self.save()
        print(f'START {self.kind} {stage}', flush=True)
        try:
            staged = prior.get('staged') if prior else None
            if not staged:
                staged = bridge.stage_agent_attachments(self.connection,self.model,[{'name':stage+'.txt','mime':'text/plain',
                    'data_b64':base64.b64encode(source.encode()).decode()}])
            entry['staged'] = staged
            if stage == 'correction':
                entry['human_correction'] = self.correct_mass(staged)
            started = bridge.invoke_internal_write(self.connection,'assistant.agent.start',{
                'project_id':PROJECT,'text':prompt,'session_id':self.session_id,
                'context':{'pages':staged['pages'],'object_refs':staged['object_refs']}})
            self.session_id = started['session_id']
            entry.update(session_id=self.session_id,run_id=started['run_id'])
            self.save()
            context = {'project_id':PROJECT,'task_id':started['task_id'],'run_id':started['run_id'],
                'pages':staged['pages'],'object_refs':staged['object_refs']}
            runner = single_pi if self.kind == 'single' else bridge.run_pi_agent
            result = runner(self.connection,self.model,started,prompt,context)
            result['data'] = {'pages':staged['pages']}
            result['object_refs'] = staged['object_refs'] + result.get('object_refs',[])
            finished = bridge.invoke_internal_write(self.connection,'assistant.agent.finish',{
                'project_id':PROJECT,'run_id':started['run_id'],'claim_epoch':started['claim_epoch'],'result':result})
            entry.update(status=result.get('status'),result=result,finish=finished)
        except Exception as error:
            entry.update(status='harness_failed',error=str(error))
        entry['seconds'] = round(time.monotonic()-start,2)
        entry['snapshot'] = self.snapshot(stage)
        self.save()
        print(f'FINISH {self.kind} {stage} {entry["status"]}', flush=True)
        return entry


    def audit_existing(self, message, audit_id='structured_delivery_audit_v1'):
        """A separate generic continuation: existing sources only, no new upload."""
        audits = self.report.setdefault('audits', [])
        if any(entry['id'] == audit_id for entry in audits):
            raise RuntimeError('Audit id already exists; refusing to repeat a model continuation')
        turns_hash = hashlib.sha256(json.dumps(self.report['turns'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        start = time.monotonic()
        entry = {'id':audit_id,'message':message,'attachments':[],'status':'running',
            'scope':'Separate generic structured-delivery continuation; original final model turn is preserved.'}
        audits.append(entry);self.save()
        print(f'START {self.kind} audit {audit_id}',flush=True)
        try:
            listed = bridge.invoke_read(self.connection,'evidence.pages.list',{'project_ids':[PROJECT]})
            pages = [{'page_id':page['page_id']} for page in listed['pages']]
            entry['source_pages'] = pages
            started = bridge.invoke_internal_write(self.connection,'assistant.agent.start',{
                'project_id':PROJECT,'text':message,'session_id':self.session_id,
                'context':{'pages':pages,'object_refs':[]}})
            self.session_id = started['session_id']
            entry.update(session_id=self.session_id,run_id=started['run_id'])
            self.save()
            context = {'project_id':PROJECT,'task_id':started['task_id'],'run_id':started['run_id'],
                'pages':pages,'object_refs':[]}
            runner = single_pi if self.kind == 'single' else bridge.run_pi_agent
            result = runner(self.connection,self.model,started,message,context)
            result['data'] = {'pages':pages}
            finished = bridge.invoke_internal_write(self.connection,'assistant.agent.finish',{
                'project_id':PROJECT,'run_id':started['run_id'],'claim_epoch':started['claim_epoch'],'result':result})
            entry.update(status=result.get('status'),result=result,finish=finished)
        except Exception as error:
            entry.update(status='harness_failed',error=str(error))
        entry['seconds'] = round(time.monotonic()-start,2)
        entry['snapshot'] = self.snapshot('audit')
        entry['original_turns_unchanged'] = turns_hash == hashlib.sha256(json.dumps(self.report['turns'],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        self.save()
        print(f'FINISH {self.kind} audit {audit_id} {entry["status"]}',flush=True)
        return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--fixture-dir',type=Path,default=REPO/'benchmark/handoff_v1/input')
    parser.add_argument('--auto-advance',action='store_true',help='Run selected conditions without waiting for independent initial readers')
    parser.add_argument('--conditions',nargs='+',choices=('single','multi','direct'),default=('multi','single','direct'),
        help='Select a subset; default runs multi, single and direct. Only selected conditions are created or invoked.')
    parser.add_argument('--keep-serving',action='store_true')
    parser.add_argument('--resume',action='store_true',help='Recover stopped retained labs; never repeat a started model turn')
    parser.add_argument('--legacy-normalizer',type=Path,help='Freeze the pilot normalizer from a pre-fix source file, in memory only')
    parser.add_argument('--audit-existing',help='Separate generic continuation using existing sources only; requires --resume --conditions single')
    args = parser.parse_args()
    selected_conditions = list(dict.fromkeys(args.conditions))
    if args.audit_existing and (not args.resume or selected_conditions != ['single']):
        parser.error('--audit-existing requires --resume --conditions single')
    root = args.run_dir.resolve(); root.mkdir(parents=True,exist_ok=True)
    compatibility = None
    if args.legacy_normalizer:
        from chemclaw_lab import experiments
        source = args.legacy_normalizer.read_text(encoding='utf-8-sig')
        function = next(node for node in ast.parse(source).body if isinstance(node,ast.FunctionDef) and node.name=='_normalize_proposal_items')
        module = ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[]))
        exec(compile(module,str(args.legacy_normalizer),'exec'),experiments.__dict__)
        compatibility = {'function':'experiments._normalize_proposal_items','scope':'in-memory isolated harness only',
            'source_file':str(args.legacy_normalizer.resolve()),'sha256':hashlib.sha256(args.legacy_normalizer.read_bytes()).hexdigest()}

    if not (root/'fixture/tasks.json').is_file():
        if args.resume:
            raise RuntimeError('Resume requires retained fixture inputs')
        fixture_source=args.fixture_dir.resolve()
        frozen_spec=json.loads((fixture_source/'tasks.json').read_text(encoding='utf-8-sig'))
        names=['tasks.json']+[stage['source'] for stage in frozen_spec['stages']]
        if any(Path(name).name!=name for name in names):
            raise RuntimeError('Fixture filenames must be plain local filenames')
        (root/'fixture').mkdir(parents=True,exist_ok=True)
        for name in names:
            target=root/'fixture'/name
            if target.exists():
                raise RuntimeError('Refusing to overwrite partial frozen inputs')
            shutil.copyfile(fixture_source/name,target)
    spec = json.loads((root/'fixture/tasks.json').read_text(encoding='utf-8-sig'))
    settings = json.loads(args.config.read_text(encoding='utf-8-sig'))
    model = bridge.validate_model_config({'baseUrl':settings['modelBase'],'model':settings['model'],
        'apiKey':settings.get('modelKey',''),'protocol':settings.get('modelProtocol','openai-chat'),
        'reasoningFormat':settings.get('modelReasoningFormat','auto'),'reasoningEffort':settings.get('modelReasoningEffort') or 'auto',
        'maxTokens':settings.get('modelMaxTokens',16384),'contextWindow':settings.get('modelContextWindow',128000)},0)
    hidden = list(filter(None,[settings.get('modelKey'),settings.get('visionKey')]))
    del settings
    inputs = []
    for stage in spec['stages']:
        source = (root/'fixture'/stage['source']).read_text(encoding='utf-8-sig')
        questions = [task for task in spec['tasks'] if task['id'] in stage['task_ids']]
        prompt = stage['organize']+'\n\n'+ '\n'.join(task['id']+'：'+task['prompt'] for task in questions)
        inputs.append((stage['id'],source,prompt))
    top = {'case_id':spec['case_id'],'model':model.model,'reasoning_format':model.reasoning_format,
        'reasoning_effort':model.reasoning_effort,'max_tokens_per_response':model.max_tokens,'context_window':model.context_window,
        'requested_conditions':selected_conditions,'input_policy':'Only frozen original text and tasks form each user turn. No evaluation answers or development reports are loaded.',
        'status':'running','conditions':{},'model_calls':None,'token_usage':None,'usage_note':'Production public Pi receipts and direct helper do not expose complete provider usage; no model-call or token count is inferred.','source_hashes':{str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((root/'fixture').iterdir())},
        'limitations':['One case and one run per condition; no significance claim.',
            'Same model and per-response generation ceiling, not equal aggregate tokens or model calls.',
            'Direct receives all original material available at its stage, without prior answers, Core or persistent memory.',
            'Direct answers only; persistence/versions/permissions apply only to Core conditions.',
            'No reference answers loaded; failures retained without answer-based retries.']}
    if args.resume:
        top = json.loads((root/'pilot.json').read_text(encoding='utf-8'))
        top['status'] = 'auditing_existing' if args.audit_existing else 'resuming'
        if not args.audit_existing:
            top.setdefault('harness_recoveries',[]).append('Only pre-model harness failures may be recovered; completed model turns are not resubmitted.')
        if (root/'stop').exists():
            (root/'stop').unlink()
    if args.audit_existing:
        top['audit_requested_conditions'] = selected_conditions
    else:
        top['requested_conditions'] = selected_conditions
    if compatibility:
        top['product_compatibility_override'] = compatibility
    def save(): atomic_save(root/'pilot.json',public_export(top,tuple(hidden)))
    def gate(file):
        while not (root/file).exists():
            if (root/'stop').exists(): return False
            time.sleep(1)
        return True
    servers=[]
    try:
        if args.audit_existing:
            single=Condition(root,'single',model,hidden,resume=True);servers.append(single.server)
            top['conditions']['single']=single.report;save()
            single.audit_existing(args.audit_existing);top['status']='audit_finished';save()
            if args.keep_serving:
                print('SERVING_UNTIL_STOP_FILE',flush=True)
                while not (root/'stop').exists():time.sleep(1)
            return 0
        multi = None
        if 'multi' in selected_conditions:
            multi=Condition(root,'multi',model,hidden,resume=args.resume);servers.append(multi.server)
            top['conditions']['multi']=multi.report;save()
            multi.turn(*inputs[0]);save()
            (root/'multi-initial-ready').write_text('Initial stage retained; add continue-multi to advance.',encoding='utf-8')
            print('MULTI_INITIAL_READY '+multi.report['reader_config'],flush=True)
        if 'single' in selected_conditions:
            single=Condition(root,'single',model,hidden,resume=args.resume);servers.append(single.server)
            top['conditions']['single']=single.report;save()
            for entry in inputs: single.turn(*entry);save()
        if 'direct' in selected_conditions:
            direct=top['conditions'].get('direct') or {'condition':'direct','turns':[],'model':model.model,'persistent_state':False,'tools':[]}
            top['conditions']['direct']=direct
            original=[]
            for stage,source,prompt in inputs:
                original.append(source)
                if any(turn['stage']==stage for turn in direct['turns']):
                    continue
                start=time.monotonic()
                text='以下为逐阶段提供的完整模拟原始记录。你没有ChemClaw或持久状态；仅据原文回答后面的任务。不要声称已保存数据。\n\n'+'\n\n'.join(original)+'\n\n任务：\n'+prompt.split('\n\n',1)[-1]
                result={'stage':stage,'prompt':text,'status':'running'};direct['turns'].append(result);save()
                print('START direct '+stage,flush=True)
                try:
                    answer=bridge.call_model(model,[{'role':'user','content':text}],timeout=300)
                    result.update(status='completed',text=answer)
                except Exception as error:
                    result.update(status='failed',error=str(error))
                result['seconds']=round(time.monotonic()-start,2);save()
                print('FINISH direct '+stage+' '+result['status'],flush=True)
        if multi:
            top['status']='awaiting_multi_advance';save()
            print('WAITING_CONTINUE_MULTI',flush=True)
            if args.auto_advance or gate('continue-multi'):
                for entry in inputs[1:]:multi.turn(*entry);save()
                top['status']='finished';save()
                (root/'multi-final-ready').write_text('Final stage retained.',encoding='utf-8')
                print('MULTI_FINAL_READY',flush=True)
        else:
            top['status']='finished';save()
        if args.keep_serving:
            print('SERVING_UNTIL_STOP_FILE',flush=True)
            while not (root/'stop').exists():time.sleep(1)
    except Exception as error:
        top.update(status='harness_failed',error=str(error));save()
        print(str(public_export({'error':str(error)},tuple(hidden))),flush=True)
        return 1
    finally:
        for server in servers:server.shutdown();server.server_close()
    return 0

if __name__=='__main__':raise SystemExit(main())
