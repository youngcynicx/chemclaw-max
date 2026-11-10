from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .db import connect, init_db
from .inventory import (
    list_inventory_events,
    process_inventory_message,
    resolve_inventory_event,
    result_to_json,
)
from .seed import seed_from_json

_SOURCE_SEED = Path("demo_data/lab_seed.json")
_INSTALLED_SHARE = Path(sys.prefix) / "share" / "chemclaw-lab"
if _SOURCE_SEED.is_file():
    DEFAULT_DB = Path("demo_data/chemclaw_lab.db")
    DEFAULT_SEED = _SOURCE_SEED
else:
    DEFAULT_DB = Path("chemclaw_lab.db")
    DEFAULT_SEED = _INSTALLED_SHARE / "demo_data/lab_seed.json"


def _discover_benchmark_root() -> Path:
    source_root = Path(".")
    if (
        (source_root / "benchmark/cases").is_dir()
        and (source_root / "demo_data/lab_seed.json").is_file()
    ):
        return source_root
    return _INSTALLED_SHARE


DEFAULT_BENCHMARK_ROOT = _discover_benchmark_root()


def cmd_init(args: argparse.Namespace) -> None:
    init_db(args.db)
    print(f"Initialized database at {args.db}")


def cmd_seed(args: argparse.Namespace) -> None:
    init_db(args.db)
    seed_from_json(args.db, args.seed)
    print(f"Seeded database from {args.seed} into {args.db}")


def cmd_status(args: argparse.Namespace) -> None:
    init_db(args.db)
    conn = connect(args.db)
    try:
        tables = {
            "users": "SELECT COUNT(*) FROM users",
            "projects": "SELECT COUNT(*) FROM projects",
            "chemicals": "SELECT COUNT(*) FROM chemicals",
            "instruments": "SELECT COUNT(*) FROM instruments",
            "bookings": "SELECT COUNT(*) FROM bookings",
            "message_logs": "SELECT COUNT(*) FROM message_logs",
            "event_proposals": "SELECT COUNT(*) FROM event_proposals",
            "inventory_transactions": "SELECT COUNT(*) FROM inventory_transactions",
            "experiment_events": "SELECT COUNT(*) FROM experiment_events",
            "audit_logs": "SELECT COUNT(*) FROM audit_logs",
            "gateway_identities": "SELECT COUNT(*) FROM gateway_identities",
            "import_batches": "SELECT COUNT(*) FROM import_batches",
        }
        out = {}
        for name, sql in tables.items():
            out[name] = conn.execute(sql).fetchone()[0]
        print(json.dumps(out, indent=2))
    finally:
        conn.close()


def cmd_list_chemicals(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        rows = conn.execute(
            """
            SELECT chemical_id, name, formula, location, amount, unit, threshold
            FROM chemicals
            ORDER BY name
            """
        ).fetchall()
        for row in rows:
            low = " [LOW]" if row["amount"] <= row["threshold"] else ""
            print(
                f'{row["name"]} ({row["formula"] or "-"}) | '
                f'{row["amount"]} {row["unit"]} | {row["location"]}{low}'
            )
    finally:
        conn.close()


def cmd_list_bookings(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        rows = conn.execute(
            """
            SELECT b.booking_id, i.name AS instrument_name,
                   u.name AS user_name, p.name AS project_name,
                   b.start_time, b.end_time, b.status
            FROM bookings b
            JOIN instruments i ON i.instrument_id = b.instrument_id
            JOIN users u ON u.user_id = b.user_id
            LEFT JOIN projects p ON p.project_id = b.project_id
            ORDER BY b.start_time
            """
        ).fetchall()
        for row in rows:
            print(
                f'{row["instrument_name"]} | {row["start_time"]} -> {row["end_time"]} | '
                f'{row["user_name"]} | {row["project_name"] or "-"} | {row["status"]}'
            )
    finally:
        conn.close()


def cmd_ingest_message(args: argparse.Namespace) -> None:
    init_db(args.db)
    result = process_inventory_message(
        args.db,
        args.message,
        source=args.source,
        source_message_id=args.source_message_id,
        user_id=args.user_id,
        project_id=args.project_id,
        apply_approved=not args.dry_run,
    )
    print(result_to_json(result))


def cmd_list_events(args: argparse.Namespace) -> None:
    init_db(args.db)
    print(result_to_json(list_inventory_events(args.db, status=args.status)))


def cmd_confirm_event(args: argparse.Namespace) -> None:
    init_db(args.db)
    corrections = {
        "item_id": args.item_id,
        "amount": args.amount,
        "unit": args.unit,
        "transaction_type": args.transaction_type,
        "project_id": args.project_id,
        "user_id": args.user_id,
    }
    corrections = {key: value for key, value in corrections.items() if value is not None}
    result = resolve_inventory_event(
        args.db,
        args.event_id,
        approve=not args.reject,
        corrections=corrections,
        confirmed_by=args.confirmed_by,
    )
    print(result_to_json(result))


def cmd_map_identity(args: argparse.Namespace) -> None:
    from .gateway import upsert_gateway_identity

    init_db(args.db)
    result = upsert_gateway_identity(
        args.db,
        source=args.source,
        external_user_id=args.external_user_id,
        user_id=args.user_id,
        display_name=args.display_name,
    )
    print(result_to_json(result))


def cmd_export_excel(args: argparse.Namespace) -> None:
    from .excel import export_inventory_workbook

    output = export_inventory_workbook(args.db, args.output)
    print(f"Exported inventory projection to {output}")


def cmd_import_excel(args: argparse.Namespace) -> None:
    from .excel import import_inventory_workbook

    result = import_inventory_workbook(
        args.db,
        args.input,
        mode=args.mode,
        imported_by=args.imported_by,
    )
    print(result_to_json(result))


def cmd_serve_api(args: argparse.Namespace) -> None:
    from .api import serve_api

    api_key = os.environ.get(args.api_key_env) or None
    serve_api(
        args.db,
        host=args.host,
        port=args.port,
        api_key=api_key,
    )


def cmd_worker(args: argparse.Namespace) -> None:
    from .service_config import ServiceSettings
    from .worker import run_worker

    settings = ServiceSettings(storage_root=args.storage_root) if args.storage_root else None
    summary = run_worker(
        str(args.db),
        services=settings,
        poll_seconds=args.poll_seconds,
        once=args.once,
        stop_file=args.stop_file,
        health_file=args.health_file,
    )
    print(result_to_json(summary))


def cmd_ask_inventory(args: argparse.Namespace) -> None:
    from .qa import answer_inventory_question

    print(result_to_json(answer_inventory_question(args.db, args.question)))


def cmd_benchmark_run(args: argparse.Namespace) -> None:
    from .benchmarking import (
        load_benchmark_cases,
        run_benchmark,
        write_benchmark_reports,
    )

    cases = load_benchmark_cases(args.cases)
    report = run_benchmark(cases, repository_root=args.repository_root)
    if args.output_dir is not None:
        paths = write_benchmark_reports(
            report,
            args.output_dir,
            stem=args.stem,
        )
        print(
            result_to_json(
                {
                    "summary": report["summary"],
                    "reports": {
                        key: str(path) for key, path in paths.items()
                    },
                }
            )
        )
    else:
        print(result_to_json(report))
    if (
        args.require_all
        and report["summary"]["passed_cases"]
        != report["summary"]["total_cases"]
    ):
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chemclaw",
        description="ChemClaw-Lab local operations core",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-db", help="Initialize the SQLite database")
    p_init.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_init.set_defaults(func=cmd_init)

    p_seed = sub.add_parser("seed-demo", help="Seed the database with demo lab data")
    p_seed.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_seed.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    p_seed.set_defaults(func=cmd_seed)

    p_status = sub.add_parser("status", help="Show demo database object counts")
    p_status.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_status.set_defaults(func=cmd_status)

    p_chem = sub.add_parser("list-chemicals", help="List demo chemicals")
    p_chem.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_chem.set_defaults(func=cmd_list_chemicals)

    p_book = sub.add_parser("list-bookings", help="List demo instrument bookings")
    p_book.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_book.set_defaults(func=cmd_list_bookings)

    p_ingest = sub.add_parser(
        "ingest-message",
        help="Parse and apply a simple inventory update message",
    )
    p_ingest.add_argument("message", help="Raw lab message, for example: 'Used 5 g KBr'")
    p_ingest.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_ingest.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and audit without applying approved changes",
    )
    p_ingest.add_argument("--source", default="cli", help="Gateway source name")
    p_ingest.add_argument(
        "--source-message-id",
        help="Stable gateway message ID used to prevent duplicate application",
    )
    p_ingest.add_argument("--user-id")
    p_ingest.add_argument("--project-id")
    p_ingest.set_defaults(func=cmd_ingest_message)

    p_events = sub.add_parser("list-events", help="List persisted inventory event proposals")
    p_events.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_events.add_argument(
        "--status",
        choices=[
            "proposed",
            "pending_confirmation",
            "approved",
            "rejected",
            "applied",
            "superseded",
        ],
    )
    p_events.set_defaults(func=cmd_list_events)

    p_confirm = sub.add_parser(
        "confirm-event",
        help="Approve/correct or reject a persisted inventory event",
    )
    p_confirm.add_argument("event_id")
    p_confirm.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_confirm.add_argument("--reject", action="store_true")
    p_confirm.add_argument("--item-id")
    p_confirm.add_argument("--amount", type=float)
    p_confirm.add_argument("--unit")
    p_confirm.add_argument(
        "--transaction-type",
        choices=["use", "add", "adjust"],
    )
    p_confirm.add_argument("--project-id")
    p_confirm.add_argument("--user-id")
    p_confirm.add_argument("--confirmed-by", default="cli_user")
    p_confirm.set_defaults(func=cmd_confirm_event)

    p_identity = sub.add_parser(
        "map-identity",
        help="Map a gateway user identity to a ChemClaw lab user",
    )
    p_identity.add_argument("source")
    p_identity.add_argument("external_user_id")
    p_identity.add_argument("user_id")
    p_identity.add_argument("--display-name")
    p_identity.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_identity.set_defaults(func=cmd_map_identity)

    p_export = sub.add_parser(
        "export-excel",
        help="Generate an Excel projection from SQLite",
    )
    p_export.add_argument("output", type=Path)
    p_export.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_export.set_defaults(func=cmd_export_excel)

    p_import = sub.add_parser(
        "import-excel",
        help="Import an Inventory worksheet through the event/policy pipeline",
    )
    p_import.add_argument("input", type=Path)
    p_import.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_import.add_argument("--mode", choices=["create", "merge"], default="merge")
    p_import.add_argument("--imported-by", default="excel_admin")
    p_import.set_defaults(func=cmd_import_excel)

    p_api = sub.add_parser(
        "serve-api",
        help="Run the local ChemClaw HTTP API",
    )
    p_api.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_api.add_argument("--host", default="127.0.0.1")
    p_api.add_argument("--port", type=int, default=8765)
    p_api.add_argument(
        "--api-key-env",
        default="CHEMCLAW_API_KEY",
        help="Environment variable containing the API key",
    )
    p_api.set_defaults(func=cmd_serve_api)

    p_worker = sub.add_parser(
        "worker",
        help="Run the durable inbound/outbox worker (local/offline by default)",
    )
    p_worker.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_worker.add_argument("--storage-root", type=Path)
    p_worker.add_argument("--poll-seconds", type=float, default=1.0)
    p_worker.add_argument("--once", action="store_true", help="Run one pass and exit")
    p_worker.add_argument("--stop-file", type=Path)
    p_worker.add_argument("--health-file", type=Path)
    p_worker.set_defaults(func=cmd_worker)

    p_ask = sub.add_parser(
        "ask-inventory",
        help="Answer a read-only inventory question",
    )
    p_ask.add_argument("question")
    p_ask.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_ask.set_defaults(func=cmd_ask_inventory)

    p_benchmark = sub.add_parser(
        "benchmark-run",
        help="Run reproducible ChemLabOps-Bench cases",
    )
    p_benchmark.add_argument(
        "cases",
        nargs="*",
        type=Path,
        default=[DEFAULT_BENCHMARK_ROOT / "benchmark/cases"],
    )
    p_benchmark.add_argument(
        "--repository-root",
        type=Path,
        default=DEFAULT_BENCHMARK_ROOT,
    )
    p_benchmark.add_argument("--output-dir", type=Path)
    p_benchmark.add_argument("--stem", default="v0.4_baseline")
    p_benchmark.add_argument("--require-all", action="store_true")
    p_benchmark.set_defaults(func=cmd_benchmark_run)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
