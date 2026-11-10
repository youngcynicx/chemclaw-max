"""Run repository engineering tests with local fixtures, never paid model calls."""
from __future__ import annotations
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from _node_runtime import find_node

ROOT = Path(__file__).resolve().parents[1]

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'test-results')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    node = find_node()
    if not (ROOT/'runtime/pi/node_modules/@earendil-works/pi-agent-core').exists():
        parser.error('Install the runtime dependencies first: cd runtime/pi && npm ci')
    env = dict(os.environ, PYTHONPATH=str(ROOT/'src'), CHEMCLAW_NODE_EXE=node)
    groups = [
        ('core', [sys.executable, '-m', 'pytest', '-q', 'tests']),
        ('frontend-python', [sys.executable, '-m', 'pytest', '-q', 'frontend/tests']),
        ('runtime-node', [node, '--test', *[str(p.relative_to(ROOT)) for p in sorted((ROOT/'runtime/pi').glob('*.test.mjs'))]]),
        ('frontend-ui', [node, 'frontend/tests/test_jobs_ui.mjs']),
        ('frontend-markdown-settings', [node, 'frontend/tests/test_markdown_settings.mjs']),
    ]
    results = []
    for name, command in groups:
        print(f'Running {name}...', flush=True)
        started = time.monotonic()
        with (output/f'{name}.log').open('w', encoding='utf-8') as log:
            result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        entry = {'suite':name, 'command':command, 'exit_code':result.returncode,
                 'seconds':round(time.monotonic()-started,3), 'log':f'{name}.log'}
        results.append(entry)
        print(f'{name}: exit {result.returncode}, {entry["seconds"]} seconds', flush=True)
    report = {'completed_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'scope':'Engineering tests with isolated fixtures/local controlled providers; no live LLM evaluation.',
              'python':sys.version, 'node':subprocess.check_output([node,'--version'],text=True).strip(),
              'results':results}
    (output/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return int(any(r['exit_code'] for r in results))

if __name__ == '__main__':
    raise SystemExit(main())
