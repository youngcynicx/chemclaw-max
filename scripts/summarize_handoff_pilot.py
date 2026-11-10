"""Export measured pilot rows, separating answers, saved state and API usage."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def tool_counts(result):
    """Deduplicate nested delegation copies by recorded call identity."""
    calls = {}
    children = set()

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for call in value.get("toolCalls", []):
                identity = call.get("operation_id") or call.get("toolCallId")
                if identity:
                    calls[identity] = call
            for child in value.get("delegations", []):
                if child.get("run_id"):
                    children.add(child["run_id"])
            for key in ("toolCalls", "delegations", "result"):
                if key in value:
                    visit(value[key])

    visit(result)
    return {"recorded_tool_calls": len(calls),
            "recorded_tool_errors": sum(bool(call.get("isError")) or call.get("result", {}).get("ok") is False for call in calls.values()),
            "delegated_runs": len(children)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    pilot = json.loads((args.run_dir / "pilot.json").read_text(encoding="utf-8"))
    rows = []
    for condition, data in pilot["conditions"].items():
        for turn in data.get("turns", []):
            state = turn.get("snapshot", {}).get("counts", {})
            row = {"condition": condition, "stage": turn["stage"], "status": turn["status"],
                   "seconds": turn.get("seconds"), "samples": state.get("samples"),
                   "event_revision_rows": state.get("event_revisions"),
                   "proposals": state.get("memory_proposals"),
                   "provider_requests": 1 if condition == "direct" and turn["status"] == "completed" else None,
                   "provider_tokens": None}
            row.update(tool_counts(turn.get("result", {})))
            rows.append(row)
    output = args.run_dir / "evaluation"
    output.mkdir(exist_ok=True)
    with (output / "measured-stages.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    external = []
    for path in sorted(args.run_dir.glob("external-*/*/summary.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        external.append({"path": str(path.relative_to(args.run_dir)),
                         **{key: data.get(key) for key in ("status", "actor", "mode", "model_calls", "tool_calls", "core_requests", "usage", "elapsed_seconds")}})
    summary = {"case_id": pilot["case_id"], "conditions": [
        {"condition": name, "completed_stages": sum(row["status"] == "completed" for row in rows if row["condition"] == name),
         "seconds": round(sum(row.get("seconds") or 0 for row in rows if row["condition"] == name), 2),
         "recorded_tool_calls": sum(row["recorded_tool_calls"] for row in rows if row["condition"] == name),
         "recorded_tool_errors": sum(row["recorded_tool_errors"] for row in rows if row["condition"] == name),
         "delegated_runs": sum(row["delegated_runs"] for row in rows if row["condition"] == name)}
        for name in pilot["conditions"]], "external_runs": external,
        "cost_note": "Pi provider requests/tokens were not exported in this pilot and remain null. Direct requests are one per completed stage. Recorded tool calls deduplicate returned runtime call IDs and exclude staging/human actions. External-agent elapsed time includes manual orchestration; compare API timing separately."}
    (output / "measured-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
