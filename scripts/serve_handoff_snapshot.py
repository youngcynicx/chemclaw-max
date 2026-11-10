"""Serve a retained isolated pilot database for external readers, without models.

Reuse the saved reader's loopback endpoint; start this as a hidden detached
process when the experiment owner's terminal is about to close.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from chemclaw_lab.api import create_api_server
from chemclaw_lab.service_config import ServiceSettings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--reader-config", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    args = parser.parse_args()
    if not args.db.is_file():
        raise SystemExit("Retained database must already exist.")
    config = json.loads(args.reader_config.read_text(encoding="utf-8-sig"))
    endpoint = urlsplit(config["base_url"])
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or not endpoint.port:
        raise SystemExit("This local pilot helper requires an explicit loopback HTTP port.")
    if args.stop_file.exists():
        raise SystemExit("Stop file already exists; select a fresh service lifecycle path.")
    server = create_api_server(str(args.db.resolve()), port=endpoint.port,
                              service_settings=ServiceSettings(storage_root=args.db.parent / "storage"))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    args.ready_file.write_text(json.dumps({"endpoint": config["base_url"], "model_calls": 0,
                                          "db": str(args.db.resolve())}), encoding="utf-8")
    try:
        while not args.stop_file.exists():
            time.sleep(0.5)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
