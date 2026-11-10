"""Compare the existing human sample-card projection with external AI context.

Read-only observation for an idle experimental stage. No model calls or writes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from chemclaw_lab.clients import ReadOnlyQueryClient


def compare_views(card, context):
    human = {}
    for record in card.get("records", []):
        for identity in record.get("related_events") or [record]:
            human[identity["event_id"]] = {**record, "revision": identity["revision"]}
    machine = {record["event_id"]: record for record in context.get("events", [])}
    mismatches = []
    if set(human) != set(machine):
        mismatches.append({"kind": "event_ids", "human_only": sorted(set(human) - set(machine)),
                           "ai_only": sorted(set(machine) - set(human))})
    for event_id in sorted(set(human) & set(machine)):
        left, right = human[event_id], machine[event_id]
        for key in ("revision", "event_kind", "raw_text", "fields", "unknown", "unknowns", "source_stale", "lifecycle_state"):
            if left.get(key) != right.get(key):
                mismatches.append({"event_id": event_id, "field": key})
        refs = lambda event: sorted(json.dumps(ref, sort_keys=True) for ref in event.get("evidence_refs", []))
        if refs(left) != refs(right):
            mismatches.append({"event_id": event_id, "field": "evidence_refs"})
    if card.get("current_state", []) != context.get("current_state", []):
        mismatches.append({"kind": "current_state"})
    return {"sample_id": card["sample_id"], "label": card["label"],
            "human_events": len(human), "ai_events": len(machine),
            "matched_events": len(set(human) & set(machine)),
            "context_status": context.get("status"),
            "fingerprint": context.get("snapshot", {}).get("fingerprint"),
            "mismatches": mismatches, "consistent": not mismatches and context.get("status") == "complete"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reader-config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.reader_config.read_text(encoding="utf-8-sig"))
    client = ReadOnlyQueryClient(config["base_url"], config["api_key"], timeout=60)
    cards = client.invoke("query.samples.list", {"project_ids": [config["project_id"]]})["result"]["samples"]
    checks = [compare_views(card, client.get_sample_context(config["project_id"], card["sample_id"],
              budget={"max_bytes": 1048576})) for card in cards]
    result = {"human_projection": "query.samples.list default expanded; same capability as /api/core/samples",
              "ai_projection": "query.sample.context", "samples": checks,
              "consistent": bool(checks) and all(check["consistent"] for check in checks),
              "model_calls": 0, "note": "Sequential read observation at an idle stage; no human task timing study."}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"consistent": result["consistent"], "samples": len(checks),
                      "matched_event_appearances": sum(check["matched_events"] for check in checks),
                      "out": str(args.out.resolve())}, ensure_ascii=False))
    return 0 if result["consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
