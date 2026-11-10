"""Bounded, retained OCR-reuse development trial; never a new image benchmark.

Uses the existing Core/Pi integration and an empty isolated project. No model
retries, OCR calls, answer input, production settings changes or job replay.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

import run_handoff_case as pilot
from check_handoff_views import compare_views
from chemclaw_lab.clients import ReadOnlyQueryClient

REPO = pilot.REPO
PROMPT = (
    "Organize this supplied simulated laboratory notebook page into persistent "
    "sample knowledge. Preserve sample identity, source-supported facts, units, "
    "conditions, corrections, plans versus performed work, and specific unknowns. "
    "This input reuses an earlier real OCR transcript for development; it is not "
    "a new image/OCR evaluation. Read back stored knowledge and report actual "
    "coverage and remaining gaps. Answer in English."
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--source-json', type=Path, required=True)
    parser.add_argument('--page-index', type=int, default=0)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--effort', choices=('max', 'high', 'low'), required=True)
    parser.add_argument('--timeout', type=int, default=300, choices=range(180, 301))
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    root = args.run_dir.resolve()
    # Exclusive output creation is the no-replay boundary, including preparation.
    root.mkdir(parents=True, exist_ok=False)
    os.environ['CHEMCLAW_PROVIDER_AUDIT_PATH'] = str(root / 'provider-requests.jsonl')
    original = json.loads(args.source_json.read_text(encoding='utf-8'))
    page = original['pages'][args.page_index]
    transcript = page['transcript']
    source = ('SIMULATED RECORD; reused real OCR transcript; no new OCR.\n' +
              '\n'.join(block['text'] for block in transcript['blocks']))
    (root / 'source.txt').write_text(source, encoding='utf-8')
    pilot.atomic_save(root / 'source-provenance.json', {
        'original_readback': str(args.source_json.resolve()),
        'original_readback_sha256': sha(args.source_json), 'page': page,
        'mapping': 'All original blocks concatenated in order as a text attachment; new page/block IDs.',
        'new_ocr': False, 'development_only': True})
    frozen = {}
    for folder, pattern in [('runtime/pi', '*.mjs'), ('runtime/pi/prompts', '*.md'),
                            ('runtime/agents', '*.json'), ('frontend', '*.py'),
                            ('src/chemclaw_lab', '*.py')]:
        for path in (REPO / folder).glob(pattern):
            frozen[path.relative_to(REPO).as_posix()] = sha(path)
    protocol = {
        'phase': 'prepared', 'reasoning_effort': args.effort, 'max_tokens': 16384,
        'context_window': 128000, 'timeout_seconds': args.timeout,
        'closing_reserve_seconds': min(120, args.timeout * .15),
        'prompt': PROMPT, 'source_sha256': sha(root / 'source.txt'),
        'product_sha256': frozen, 'runner_sha256': sha(__file__),
        'diagnostic_seconds': 90, 'attempts': 1, 'automatic_continuation': False,
        'limitations': ['Development OCR reuse, not unseen validation.',
                       'One observation per setting; no speed superiority claim.',
                       'Isolated text attachment and Core/Pi run; not browser job/OCR end-to-end.',
                       'Card/context consistency is API evidence, not an independent AI or human study.']}
    pilot.atomic_save(root / 'protocol.json', protocol)
    if args.prepare_only:
        print(json.dumps({'prepared': True, 'source_sha256': protocol['source_sha256']}))
        return 0
    settings = json.loads(args.config.read_text(encoding='utf-8-sig'))
    model = pilot.bridge.validate_model_config({
        'baseUrl': settings['modelBase'], 'model': settings['model'],
        'apiKey': settings['modelKey'], 'protocol': 'openai-chat',
        'reasoningFormat': 'zai', 'reasoningEffort': args.effort,
        'maxTokens': 16384, 'contextWindow': 128000}, 0)
    hidden = [settings['modelKey']]
    del settings
    pilot.PROJECT = 'simulated_fast_capture_development'
    lab = pilot.Condition(root, 'multi', model, hidden)
    conn = pilot.db.connect(lab.database)
    try:
        for row in conn.execute('SELECT agent_id,manifest_json FROM agent_manifests WHERE enabled=1').fetchall():
            manifest = json.loads(row['manifest_json'])
            manifest['timeout_s'] = args.timeout
            conn.execute('UPDATE agent_manifests SET manifest_json=? WHERE agent_id=?',
                         (json.dumps(manifest), row['agent_id']))
        conn.commit()
    finally:
        conn.close()
    lab.report['notes'] = 'One bounded multi-role development trial; three enabled roles, actual usage observed.'
    lab.report['enabled_manifests'] = None  # Old Condition copy preceded isolated timeout override.
    lab.report['protocol'] = protocol
    lab.save()
    stop = threading.Event()
    started = time.monotonic()
    metrics = {'first_delegation_observed_s': None, 'first_write_observed_s': None,
               'no_write_at_90s': None, 'poll_interval_s': .5, 'poll_errors': 0}

    def monitor():
        while not stop.is_set():
            elapsed = round(time.monotonic() - started, 3)
            try:
                c = sqlite3.connect(lab.database.resolve().as_uri() + '?mode=ro', uri=True)
                try:
                    children = c.execute("SELECT count(*) FROM agent_runs WHERE agent_id!='orchestrator'").fetchone()[0]
                    events = c.execute('SELECT count(*) FROM event_revisions').fetchone()[0]
                finally:
                    c.close()
                if children and metrics['first_delegation_observed_s'] is None:
                    metrics['first_delegation_observed_s'] = elapsed
                if events and metrics['first_write_observed_s'] is None:
                    metrics['first_write_observed_s'] = elapsed
                if elapsed >= 90 and metrics['no_write_at_90s'] is None:
                    metrics['no_write_at_90s'] = not bool(events)
                    print('DIAGNOSTIC ' + json.dumps(metrics), flush=True)
                pilot.atomic_save(root / 'timing.json', metrics)
            except Exception:
                metrics['poll_errors'] += 1
            stop.wait(.5)

    worker = threading.Thread(target=monitor, daemon=True)
    worker.start()
    try:
        protocol['phase'] = 'running'
        pilot.atomic_save(root / 'protocol.json', protocol)
        turn = lab.turn('P01', source, PROMPT)
        reader_config = json.loads(Path(lab.report['reader_config']).read_text(encoding='utf-8'))
        reader = ReadOnlyQueryClient(reader_config['base_url'], reader_config['api_key'], timeout=60)
        cards = pilot.bridge.invoke_read(lab.connection, 'query.samples.list', {'project_ids': [pilot.PROJECT]})
        contexts = [reader.get_sample_context(pilot.PROJECT, card['sample_id'], budget={'max_bytes': 1048576})
                    for card in cards['samples']]
        comparisons = [compare_views(card, context) for card, context in zip(cards['samples'], contexts)]
        pilot.atomic_save(root / 'readback.json', pilot.public_export({
            'cards': cards, 'contexts': contexts, 'comparisons': comparisons,
            'nonempty_consistent': bool(comparisons) and all(c['consistent'] for c in comparisons)}, tuple(hidden)))
        protocol.update(phase='retained', result_status=turn['status'], total_seconds=turn['seconds'])
    except BaseException as error:
        protocol.update(phase='harness_failed', error_type=type(error).__name__)
        raise
    finally:
        stop.set()
        worker.join(timeout=3)
        pilot.atomic_save(root / 'timing.json', metrics)
        lab.server.shutdown()
        lab.server.server_close()
        protocol['servers_stopped'] = True
        protocol['product_unchanged'] = all(sha(REPO / name) == value for name, value in frozen.items())
        pilot.atomic_save(root / 'protocol.json', protocol)
    print(json.dumps({'status': protocol['result_status'], **metrics}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
