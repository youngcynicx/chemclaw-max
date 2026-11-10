"""Run Core API + frontend read bridge together for manual browser testing."""
from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "frontend"))

from chemclaw_lab import db  # noqa: E402
from chemclaw_lab.api import create_api_server  # noqa: E402
from chemclaw_lab.service_config import ServiceSettings  # noqa: E402
import dev_server as bridge  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--core-port", type=int, default=8876)
    parser.add_argument("--front-port", type=int, default=4174)
    parser.add_argument("--project", default="proj_lab")
    parser.add_argument("--core-api-key", default="")
    parser.add_argument("--model-key", default="")
    parser.add_argument("--model-base", default="https://open.bigmodel.cn/api/paas/v4")
    parser.add_argument("--model", default="glm-5.3-flash")
    parser.add_argument("--model-protocol", choices=("openai-chat", "openai-responses"), default="openai-chat")
    parser.add_argument("--vision-model", default="glm-4v-flash")
    parser.add_argument("--vision-base", default="")
    parser.add_argument("--vision-key", default="")
    parser.add_argument("--reasoning-effort", default=None)
    parser.add_argument("--reasoning-format", default="auto")
    parser.add_argument("--max-tokens", type=int, default=16384)
    parser.add_argument("--context-window", type=int, default=128000)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.config is not None:
        import json
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        args.core_api_key = config.get("coreApiKey", args.core_api_key)
        args.model_key = config.get("modelKey", args.model_key)
        args.model_base = config.get("modelBase", args.model_base)
        args.model = config.get("model", args.model)
        args.model_protocol = config.get("modelProtocol", args.model_protocol)
        args.vision_model = config.get("visionModel", args.vision_model)
        args.vision_base = config.get("visionBase", args.vision_base)
        args.vision_key = config.get("visionKey", args.vision_key)
        args.reasoning_effort = config.get("modelReasoningEffort", args.reasoning_effort)
        args.reasoning_format = config.get("modelReasoningFormat", args.reasoning_format)
        args.max_tokens = config.get("modelMaxTokens", args.max_tokens)
        args.context_window = config.get("modelContextWindow", args.context_window)
        args.project = config.get("project", args.project)

    if not args.db.is_file():
        raise SystemExit(f"Core database not found: {args.db}")
    db.init_db(args.db)

    settings = ServiceSettings(storage_root=REPO / "data")
    core = create_api_server(
        str(args.db), port=args.core_port, service_settings=settings
    )
    threading.Thread(target=core.serve_forever, daemon=True).start()

    default_connection = None
    if args.core_api_key:
        default_connection = bridge.CoreConnection(
            endpoint=f"http://127.0.0.1:{args.core_port}",
            api_key=args.core_api_key,
            project_id=args.project,
        )
        bridge.describe_connection(default_connection)

    default_model = None
    if args.model_key or urlsplit(args.model_base).hostname in {"127.0.0.1", "localhost", "::1"}:
        default_model = bridge.validate_model_config(
            {
                "baseUrl": args.model_base,
                "model": args.model,
                "protocol": args.model_protocol,
                "visionModel": args.vision_model,
                "visionBaseUrl": args.vision_base,
                "visionApiKey": args.vision_key,
                "apiKey": args.model_key,
                "reasoningFormat": args.reasoning_format,
                "maxTokens": args.max_tokens,
                "contextWindow": args.context_window,
                **({"reasoningEffort": args.reasoning_effort} if args.reasoning_effort is not None else {}),
            },
            args.front_port,
        )
        tests = bridge.probe_model(default_model)
        default_model = replace(default_model, vision_test=tests["visionTest"])

    front = bridge.BridgeServer(
        ("127.0.0.1", args.front_port), REPO / "frontend" / "dist",
        default_connection=default_connection,
        default_model=default_model,
    )
    threading.Thread(target=front.serve_forever, daemon=True).start()

    url = f"http://127.0.0.1:{args.front_port}/"
    print(f"Core API: http://127.0.0.1:{args.core_port}", flush=True)
    print(f"Frontend: {url}", flush=True)
    print(f"Project: {args.project}", flush=True)
    print(f"Auto Core connect: {bool(default_connection)}", flush=True)
    print(f"Auto model: {default_model.model if default_model else None}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        front.shutdown(); front.server_close()
        core.shutdown(); core.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
