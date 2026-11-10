"""Finalize read-only evidence, timing and preservation checks."""
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parent
REPO=ROOT.parents[1]
RUN=ROOT/'regression/run'
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
read=lambda p:json.loads(Path(p).read_text(encoding='utf-8-sig'))
m=read(RUN/'manifest.json');r=read(RUN/'results.json');before=read(ROOT/'preparation.json')
c=sqlite3.connect((RUN/'final.db').as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
runs=[dict(x) for x in c.execute('SELECT run_id,agent_id,trace_id,started_at,finished_at FROM agent_runs')]
jobs={x['job_id']:dict(x) for x in c.execute('SELECT job_id,run_id FROM assistant_jobs')}
events=[dict(x) for x in c.execute('SELECT entity_id,created_at FROM event_revisions')]
active={t:c.execute(f"SELECT count(*) FROM {t} WHERE status IN ('queued','running','cancelling')").fetchone()[0] for t in ['assistant_jobs','agent_runs']}
active['tokens']=c.execute("SELECT count(*) FROM agent_run_tokens WHERE status='active'").fetchone()[0];c.close()
parse=lambda s:datetime.fromisoformat(s.replace('Z','+00:00')).replace(tzinfo=timezone.utc)
by_id={x['run_id']:x for x in runs};timing=[]
for t in r['turns']:
    root=by_id[jobs[t['job_id']]['run_id']]
    start=parse(t['started_at_utc']);end=parse(t['finished_at_utc'])
    children=[x for x in runs if x['trace_id']==root['trace_id'] and x['run_id']!=root['run_id']]
    writes=[x for x in events if start.replace(microsecond=0)<=parse(x['created_at'])<=end]
    timing.append({'id':t['id'],'seconds':t['seconds'],
        'first_child_start_s':round((min(parse(x['started_at']) for x in children)-start).total_seconds(),2) if children else None,
        'first_event_created_s':round((min(parse(x['created_at']) for x in writes)-start).total_seconds(),2) if writes else None,
        'note':'Database timestamps have second precision; intervals include OCR/startup. Sequential job windows; not exact model reasoning time.'})
first=parse(r['turns'][0]['started_at_utc']);last=parse(r['turns'][-1]['finished_at_utc'])
cp=read(ROOT/'regression/checkpoint-read-audit.json')
decoded={x['run_id']:x for x in cp if x['valid']}
budget=[]
for root in [x for x in runs if x['agent_id']=='orchestrator']:
    members=[x for x in runs if x['trace_id']==root['trace_id']]
    complete=all(x['run_id'] in decoded for x in members)
    deadlines={decoded[x['run_id']]['execution_budget']['deadline_ms'] for x in members if x['run_id'] in decoded}
    budget.append({'root':root['run_id'],'runs':len(members),'decode_complete':complete,
                   'shared_deadline_verified':complete and len(deadlines)==1})
main=Path('D:/Chem/ChemClaw-Lab-S10-Repair-test/ui-demo.db')
c=sqlite3.connect(main.as_uri()+'?mode=ro',uri=True);c.execute('BEGIN');business={}
for t in before['main_business']:
    rows=sorted(json.dumps(list(row),ensure_ascii=False) for row in c.execute(f'SELECT * FROM {t}'))
    business[t]={'count':len(rows),'sha256':hashlib.sha256('\n'.join(rows).encode()).hexdigest()}
main_active={t:c.execute(f"SELECT count(*) FROM {t} WHERE status IN ('queued','running','cancelling')").fetchone()[0] for t in ['assistant_jobs','agent_runs']};c.close()
sys.path.insert(0,str(REPO/'scripts'));from benchmark_health_guard import capture_health
health=capture_health({'core':'http://127.0.0.1:8876/health','frontend':'http://127.0.0.1:4174/'},phase='after')
(ROOT/'health-after.json').write_text(json.dumps(health,indent=2),encoding='utf-8')
frozen=read(ROOT/'evaluation/current-freeze.json')
score=read(ROOT/'evaluation/scored_items.json')
scores_valid=True
for q,s in score['questions'].items():
    exact=sum(Fraction(x['points'])*sum(Fraction(str(v)) for v in x['grades'])/len(x['grades']) for x in s['items'])
    scores_valid &= exact==Fraction(s['earned_exact']) and s['delivered_sha256']==sha(ROOT/'regression/delivered-outputs'/f'{q}.md')
scores_valid &= sum(Fraction(s['earned_exact']) for s in score['questions'].values())==Fraction(score['earned_exact'])
result={'evaluated_at_utc':datetime.now(timezone.utc).isoformat(),
    'task_wall_sum_seconds':sum(t['seconds'] for t in r['turns']),
    'first_submission_to_last_completion_seconds':(last-first).total_seconds(),
    'first_submission_utc':first.isoformat(),'last_completion_utc':last.isoformat(),
    'cleanup_finished_utc':m['finished_at_utc'],
    'barrier_wait_excluded':True,'per_task_timing':timing,'budget_traces':budget,
    'checkpoint_valid_count':len(decoded),'checkpoint_count':len(cp),
    'checks':{'ten_terminal_jobs':len(r['turns'])==10 and all(t['status'] in ['completed','partial','failed','needs_review','clarification_needed','cancelled'] for t in r['turns']),
      'isolated_no_active':not any(active.values()),'servers_stopped':m['servers_stopped'],
      'product_unchanged':m['product_unchanged'],'runner_unchanged':m['runner_unchanged'],
      'inputs_unchanged':m['inputs_unchanged'],'public_protocol_unchanged':m['public_protocol_unchanged'],
      'main_business_unchanged':business==before['main_business'],
      'private_config_unchanged':sha('D:/Chem/ChemClaw-Lab-S10-Repair-test/ui-config.json')==before['private_config_sha256'],
      'frozen_evaluation_unchanged':all(sha(ROOT/'evaluation'/n)==s for n,s in frozen['files'].items()),
      'score_arithmetic_and_output_hashes':scores_valid,
      'all_checkpoints_decodable':len(decoded)==len(cp)},
    'main_active':main_active,
    'health_before':read(ROOT/'health-before.json'),'health_after':health,
    'limits':'P06 checkpoint decode failure retained; only 9/10 trace budgets fully verified. Health is before/after point observations, not continuous availability.'}
with (ROOT/'regression/final-integrity.json').open('x',encoding='utf-8') as out:json.dump(result,out,indent=2)
print(json.dumps({'checks':result['checks'],'task_seconds':result['task_wall_sum_seconds'],
                  'first_to_last_seconds':result['first_submission_to_last_completion_seconds'],
                  'main_active':main_active,'timing':timing[:6]}))
