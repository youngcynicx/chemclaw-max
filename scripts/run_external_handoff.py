"""Prepare/export, execute one read tool, finish, or run an external chat AI."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from chemclaw_lab.external_handoff import (  # noqa: E402
    HandoffError, _read_json, execute_tool, finish_run, prepare_run, run_model,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "run"):
        command = subs.add_parser(name)
        command.add_argument("--reader-config", required=True, type=Path)
        command.add_argument("--task-file", required=True, type=Path)
        command.add_argument("--out", required=True, type=Path)
        command.add_argument("--tools", choices=("chemclaw", "none"), default="chemclaw")
        if name == "run":
            command.add_argument("--model-config", required=True, type=Path)
            command.add_argument("--max-turns", type=int, default=12)
            command.add_argument("--max-tool-calls", type=int, default=48)
    command = subs.add_parser("execute")
    command.add_argument("--run-dir", required=True, type=Path)
    command.add_argument("--reader-config", required=True, type=Path)
    command.add_argument("--request-file", required=True, type=Path)
    command.add_argument("--stdout", choices=("summary", "full"), default="summary")
    command.add_argument("--output", type=Path, help="Optional complete tool result JSON copy")
    command = subs.add_parser("finish")
    command.add_argument("--run-dir", required=True, type=Path)
    command.add_argument("--answer-file", required=True, type=Path)
    command.add_argument("--actor", required=True)
    command.add_argument("--usage-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_run(args.reader_config, args.task_file, args.out, tools=args.tools)
        elif args.command == "execute":
            result = execute_tool(args.run_dir, args.reader_config, _read_json(args.request_file))
            if args.output:
                with args.output.open("x", encoding="utf-8") as stream:
                    stream.write(json.dumps(result["response"], ensure_ascii=False, indent=2))
            if args.stdout == "summary":
                response = result.pop("response")
                result.update(action_status=response.get("action_status"), error=response.get("error"))
        elif args.command == "finish":
            result = finish_run(args.run_dir, args.answer_file, actor=args.actor,
                                usage=_read_json(args.usage_file) if args.usage_file else None)
        else:
            result = run_model(args.reader_config, args.model_config, args.task_file, args.out,
                               tools=args.tools, max_turns=args.max_turns, max_tool_calls=args.max_tool_calls)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (HandoffError, OSError):
        print("External handoff failed; check the run summary and the supplied paths/configuration.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
