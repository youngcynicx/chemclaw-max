"""Verify the SHA256 manifest of the published source/evidence snapshot."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]

def main():
    manifest = json.loads((ROOT/'SNAPSHOT_MANIFEST.json').read_text(encoding='utf-8'))
    failures = []
    for entry in manifest['files']:
        path = (ROOT/entry['path']).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            failures.append(entry['path'])
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            failures.append(entry['path'])
    print(json.dumps({'checked':len(manifest['files']),'mismatches':failures},ensure_ascii=False,indent=2))
    return int(bool(failures))

if __name__ == '__main__':
    raise SystemExit(main())
