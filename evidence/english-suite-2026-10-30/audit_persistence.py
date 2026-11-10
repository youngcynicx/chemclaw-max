"""Descriptive checks against retained API readbacks; no product writes/calls."""
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN = ROOT/'regression/run'
report = json.loads((RUN/'results.json').read_text(encoding='utf-8'))
final = json.loads((RUN/'final-live-readback.json').read_text(encoding='utf-8'))
cards = {c['label']:c for c in final['cards']['samples']}
page_names = {p['page_id']:p['filename'][:3] for p in final['pages']}
checks = []
specs = [
 ('product_mass',[248,215,162],'mg'),
 ('bet_surface_area',[1180,1620,740],'m²/g'),
 ('total_pore_volume',[.48,.74,.39],'cm³/g'),
 ('micropore_volume',[.44,.51,.21],'cm³/g'),
 ('t_decomposition',[515,490,435],'°C'),
 ('co2_uptake_298k_1bar',[1.85,3.42,1.52],'mmol/g'),
 ('co2_uptake_298k_0.15bar',[.52,1.28,.41],'mmol/g'),
 ('n2_uptake_298k_1bar',[.14,.16,.13],'mmol/g'),
 ('iast_selectivity_co2_n2_298k_1bar_15_85',[28.4,54.2,21],'dimensionless'),
 ('qst_zero_coverage',[26.5,34.8,29.1],'kJ/mol'),
 ('co2_breakthrough_dry',[23,41.5,16.8],'min/g'),
 ('cycle_retention_dry_5cycles',[99.1,98.4,94],'%'),
 ('working_capacity_retention_humid_5cycles',[97.8,94.6,38.5],'%'),
]
for metric, values, unit in specs:
    for index, expected in enumerate(values,1):
        label = f'Zr-BDC-{index:02}'
        key = metric
        if index == 2 and metric == 'co2_uptake_298k_1bar':
            key += '_annotation_conversion'
        if index == 2 and metric == 'co2_uptake_298k_0.15bar':
            key = 'co2_uptake_15kPa_298k'
        if index == 3 and metric == 'working_capacity_retention_humid_5cycles':
            key = 'uptake_loss_humid_5cycles'
        found = [e for e in cards[label]['records'] if key in e['fields']]
        actual = found[0]['fields'][key] if len(found)==1 else {}
        numeric = actual.get('value',actual.get('normalized'))
        checks.append({'sample':label,'metric':key,'expected':expected,'unit':unit,
            'actual':actual,'event_id':found[0]['event_id'] if len(found)==1 else None,
            'source_refs':found[0]['evidence_refs'] if len(found)==1 else [],
            'passed':len(found)==1 and numeric==expected and actual.get('unit')==unit
                and label in found[0]['fields']['subject_sample_labels'] and bool(found[0]['evidence_refs'])})
event = next(e for e in cards['Zr-BDC-03']['records'] if 'bet_after_humid_cycling' in e['fields'])
checks.append({'sample':'Zr-BDC-03','metric':'bet_after_humid_cycling','expected':430,'unit':'m²/g',
    'actual':event['fields']['bet_after_humid_cycling'],'event_id':event['event_id'],
    'source_refs':event['evidence_refs'],
    'passed':event['fields']['bet_after_humid_cycling']=={'unit':'m²/g','value':430}})

stages = []
previous = 0
for turn in report['turns']:
    s=json.loads((RUN/(turn['id']+'-readback.json')).read_text(encoding='utf-8'))
    count=s['counts']['event_revisions']
    stages.append({'id':turn['id'],'status':turn['status'],'seconds':turn['seconds'],
        'events_total':count,'events_added':count-previous,'samples':s['counts']['samples'],
        'same_source':bool(s['view_comparisons']) and all(x['consistent'] for x in s['view_comparisons']),
        'readback_sha256':hashlib.sha256((RUN/(turn['id']+'-readback.json')).read_bytes()).hexdigest()})
    previous=count

transitions=[]
targets=['evt_94b6dad90d195bea80606ca1e982aa1e','evt_efd8524b74bc59ed8757782a68c76097',
         'evt_7132244c14cf596fb86dc2e110ce9490']
for turn in report['turns'][:6]:
    s=json.loads((RUN/(turn['id']+'-readback.json')).read_text(encoding='utf-8'))
    for card in s['cards']['samples']:
        for e in card['records']:
            if e['event_id'] in targets:
                transitions.append({'page':turn['id'][7:10],'sample':card['label'],'event_id':e['event_id'],
                    'revision':e['revision'],'historical_state':e['fields'].get('state'),
                    'current_state':e.get('lifecycle_state')})

c=sqlite3.connect((RUN/'final.db').as_uri()+'?mode=ro',uri=True)
c.row_factory=sqlite3.Row
all_events=[]
for row in c.execute('SELECT entity_id,revision,field_json,evidence_refs_json,created_at FROM event_revisions'):
    f=json.loads(row['field_json']);refs=json.loads(row['evidence_refs_json']);
    all_events.append({'event_id':row['entity_id'],'revision':row['revision'],'created_at':row['created_at'],
                      'fields':f.get('fields',f),'evidence_refs':refs})
unique_events=c.execute('SELECT count(distinct entity_id) FROM event_revisions').fetchone()[0]
c.close()
source_coverage={page:sum(any(ref.get('page_id')==pid for ref in e['evidence_refs']) for e in all_events)
                 for pid,page in page_names.items()}
source_flags=Counter(e['fields'].get('source_kind','absent') for e in all_events)
result={'scope':'Selected descriptive checks of final persistent fields using frozen fact values; not a full semantic completeness score or unseen benchmark.',
    'numeric_checks':checks,'numeric_passed':sum(x['passed'] for x in checks),'numeric_checked':len(checks),
    'unique_events':unique_events,'event_revisions':len(all_events),'stages':stages,
    'page_linked_events':source_coverage,'source_kind_counts':dict(source_flags),
    'final_same_source':final['view_comparisons'],'cross_page_state_transitions':transitions,
    'findings':[
      {'kind':'false_ocr_correction','event_id':'evt_95b686a8e5445aaf8084ba2c179f558f','page':'P03',
       'detail':'Original image underlines150C; OCR fabricated crossed-out120C, persisted as correction_history. Current150C/12h value is correct.'},
      {'kind':'false_ocr_correction','event_id':'evt_5846fbc999245d46a4b8479e4780c303','page':'P06',
       'detail':'Original image underlines76.61cm3/g(STP); OCR fabricated old7.61. False correction is persisted and repeated in L1 despite correct final3.42mmol/g.'},
      {'kind':'incomplete_normalization','detail':'B inherited DMF/water quantities remain textual inheritance. Missing-linker01/02 values are preserved in annotation strings rather than normalized numeric properties.'},
      {'kind':'simulation_disclosure','detail':'Run/public instruction explicitly synthetic, but persisted event source_kind is absent or handwritten_lab_notebook in many records. Individual object disclosure is not consistently retained.'},
      {'kind':'pending_research_intent','event_id':'evt_9f955e65540252149e7ac9b3844a87a6',
       'detail':'03 larger-mesopore research rationale remains a pending plan. It is not the completed synthesis/activation task; should not be confused with unperformed experimental work.'}],
    'not_proven':['Complete semantic page coverage','Fresh independent-AI comprehension','Restart/recovery of unreadable P06 checkpoint','Human user study']}
with (ROOT/'evaluation/persistent_knowledge_audit.json').open('x',encoding='utf-8') as out:
    json.dump(result,out,ensure_ascii=False,indent=2)
print(json.dumps({k:result[k] for k in ['numeric_passed','numeric_checked','unique_events','event_revisions','page_linked_events','source_kind_counts']}))
print(json.dumps(stages))
