"""Serialize the current assistant's semantic review; arithmetic is exact.

No model calls or product inputs. The original frozen rubric is unchanged.
Grades are review judgments, not an automated semantic evaluator.
"""
from fractions import Fraction
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT.parent / 'regression/delivered-outputs'
rubric = json.loads((ROOT / 'frozen_question_rubric.json').read_text(encoding='utf-8'))
grades = {
 'L1': [[1,1,0,1,1], [1,1,1], [1,1,1,1,1], [1,1,1], [1,1,1,1,1], [1,.5]],
 'L2': [[.5,1,1], [1,1,1], [1,1,1,1,1], [1,1,1], [1,1,1], [.5,1,1]],
 'H1': [[1,1,1,1,1,1], [1,1,1], [1,1,1,1,1,1,1,1], [.5,1,1,1,1,1,1], [1,1,1]],
 'H2': [[1,1,.5], [1,1,1,1], [1,1,1,1], [0,.5,1,1,1], [1,1,1], [1,0]],
}
notes = {
 'L1.recipe': 'Oct12/batch, actual 233.5/166.0 mg, inheritance, TFA 2.30 mL/30 eq and 120 C/24 h are present. DMF 30 mL and water 100 uL are not stated; referring to the prior batch without quantities does not satisfy this atom.',
 'L1.recovery': 'Oct13 14:15, finer particles/9500 rpm, three DMF washes, MeOH48 h/three replacements are explicit.',
 'L1.rework': '120 C4 h,1660/~1180 bands,150 C+8 h/12 h total, reduced bands and215 mg all correct.',
 'L1.characterization': 'Oct16 framework/reo attribution, Oct17 BET1620 and hierarchical pores,1.38 missing/Zr6 correct.',
 'L1.performance': '298K1bar3.42,0.15bar1.28,Qst34.8,IAST54.2 with conditions,41.5min/g,dry98.4%,humid94.6% and150C recovery all present.',
 'L1.integrity': 'Chronology is coherent. Half credit on problem-to-correction links: the claimed Oct19 7.61-to76.61 correction is not in the original image (red underline misread by OCR). Actual TFA/activation/centrifugation corrections are supported. Deduct here only; correct final uptake is not penalized again.',
 'L2.synthesis': '03 TFA60eq/4.60mL andwater0.5mL are correct, but the comparison calls both01/02 TFA30eq;01 used AA. Half credit on the loading/comparison atom only.160C36h and notebook-based etching interpretation otherwise present.',
 'L2.workup': 'Acetone24h, later180C12h activation, andOH3674 loss/dehydroxylation are present. Unsupported mechanistic elaboration is handled once under causal_quality.',
 'L2.degradation': 'Apparent2.35,55% crystallinity/amorphization,fragmentation/cracks,tentative30.2degree ZrO2,Td435 all present.',
 'L2.pores': 'BET740,Vmicro0.21,weak0.85nm andbroad2.6-3.2nm pores with lost accessible microporosity are correct.',
 'L2.performance': '298K1bar1.52/IAST21,50%RHfivecycle loss38.5%/retained61.5%,posthumidBET430 and source-attributed hydrolysis all present.',
 'L2.causal_quality': 'Half on evidence-linked chain: low final yield is incorrectly treated as observed mass disappearance during activation without a preactivation mass; OH loss is overextended to loss of nodes. The general chain is otherwise supported. Residual Qst29.1 and loss of useful microporosity are explained. Acetone role is explicitly tentative and multiple-variable limits stated, so no extra penalty on atom3. Some cited IDs belong to other samples (e.g.95b686/832f447 are01/shared01-02); qualitative fact content is still correct, but citations need repair.',
 'H1.recipes': 'All mass pairs,modulator identities/volumes/loadings,water/DMF including explicit same-as01 references,temperatures anddurations are correct.',
 'H1.workup': 'MeOH48h vsacetone24h,02two-stage activation and248/215/162mg correct.',
 'H1.structure': 'All requested BET/pore volumes/pore sizes/defect counts/Td/phase andmorphology are present;03impurity tentative anddefect count apparent.',
 'H1.adsorption': 'All tabulated values andconditions are correct. Half on298K1bar capacity atom because the prose calls01 lowest-performing while the table and03 discussion show03lowest. No repeated penalty on other performance atoms.',
 'H1.normalization': 'All masses actually reported aremg; no packed mass included.03conversion and02STP/0.15bar conversion correct. One readable comparison table. Parenthetical original15kPa/76.61STP retained alongside normalized primary values is allowed.',
 'H2.recipe_axis': 'AA30eq vsTFA30eq and03TFA60eq/water0.5/160C36h correct. Half on joint workup differences:180Cactivation noted but acetone24h versusMeOH48h omitted.02activation table also incorrectly says150C12h rather than120C4h+150C8h; causality consequence assessed under causal_limits.',
 'H2.moderate_defects': '0.30to1.38,retainedframework/reo,bimodal0.85/1.85,BET1180to1620 andQst26.5to34.8 all present.',
 'H2.excess_defects': '2.35 apparent,connectivity/micropore collapse,BET740/disorderedpores,crystallinity/morphology/OHloss,Td435/wetfailure all present.',
 'H2.performance': '0.15bar sequence0.52/1.28/0.41 absent.IAST28.4/54.2/21 present but mixture15:85 and298K1bar IAST condition omitted (half).Capacity/breakthrough order,Qst03above01,andwetfractionalretention nuance present.',
 'H2.volcano': 'Rise/fall,accessibility/affinity versusintegrity tradeoff andexplicit three-point/trend/not-fitted caveat present. No penalty solely for qualitative volcano terminology.',
 'H2.causal_limits': 'Mechanisms ultimately labeled notebook interpretations/not proven (atom1full). Atom2zero: explicitly says01vs02 isolate modulator strength despite different activation protocols and uses an unestablished optimum/connectivity threshold. This is the primary deduction for the causal-control claim, not repeated in volcano.',
}
scores = {}
for question, items in rubric['questions'].items():
    answer = OUTPUTS / (question + '.md')
    assert answer.is_file() and len(items) == len(grades[question])
    rows = []
    for item, values in zip(items, grades[question]):
        assert len(values) == len(item['equal_weight_atoms'])
        assert all(value in [0,.5,1] for value in values)
        exact = Fraction(item['points']) * sum(Fraction(str(v)) for v in values) / len(values)
        rows.append({**item, 'grades': values, 'earned_exact': str(exact), 'earned': float(exact),
                     'evidence_and_judgment': notes[item['id']]})
    total = sum(Fraction(row['earned_exact']) for row in rows)
    scores[question] = {'earned_exact': str(total), 'earned': float(total), 'possible':25, 'items':rows,
        'delivered_sha256': hashlib.sha256(answer.read_bytes()).hexdigest()}
total = sum(Fraction(score['earned_exact']) for score in scores.values())
report = {'review_date':'2026-10-30','evaluator':'Current assistant, non-blind; no independent evaluator claimed.',
          'rubric_sha256':hashlib.sha256((ROOT/'frozen_question_rubric.json').read_bytes()).hexdigest(),
          'denominator_changed':False,'na_atoms':[], 'possible':100,
          'earned_exact':str(total),'earned':float(total),'questions':scores,
          'scope':'Only the four delivered answers; persistence, checkpoint validity and provenance separately audited.'}
with (ROOT/'scored_items.json').open('x',encoding='utf-8') as out:
    json.dump(report,out,ensure_ascii=False,indent=2)
print(json.dumps({'total':float(total),'exact':str(total),'questions':{q:s['earned'] for q,s in scores.items()}}))
