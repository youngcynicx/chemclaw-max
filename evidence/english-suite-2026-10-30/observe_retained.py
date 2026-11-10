"""Read-only audit retaining invalid checkpoints instead of hiding/fixing them."""
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
SOURCE = REPO / 'work/research-loop-2026-10-26/summarize_run_observations.py'
spec = importlib.util.spec_from_file_location('original_observer', SOURCE)
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)
decode = observer.decoded_checkpoint_summaries
failures = []


def tolerant_decode(checkpoints):
    result = []
    for checkpoint in checkpoints:
        try:
            result.extend(decode([checkpoint]))
        except ValueError:
            failures.append(checkpoint['run_id'])
            result.append({'run_id': checkpoint['run_id'], 'decode_error': True})
    return result


observer.decoded_checkpoint_summaries = tolerant_decode
report = observer.build_report(ROOT / 'regression')
report['checkpoint_decode_failures'] = failures
report['checkpoint_decode_complete'] = not failures
report['observer_source_sha256'] = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
report['scope_notes'].append(
    'One checkpoint is unreadable by the production decoder; no recovery or alteration attempted. '
    'Observable provider rows also come from actual run/job results and the OCR ledger. '
    'Request counts are observed counts; absence of additional requests in the invalid checkpoint is unverified.')
with (ROOT / 'regression/provider-observations.json').open('x', encoding='utf-8') as out:
    json.dump(report, out, ensure_ascii=False, indent=2)
print(json.dumps({'provider': report['provider'], 'invalid_checkpoints': failures,
                  'tool_attempts': report['journal_tool_attempt_count'],
                  'tool_errors': report['journal_tool_error_count'],
                  'observation_conflicts': len(report['provider_observation_conflicts'])}))
