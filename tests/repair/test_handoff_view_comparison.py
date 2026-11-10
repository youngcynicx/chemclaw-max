"""Research observation must detect lost revisions and merged display rows."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

spec = spec_from_file_location("handoff_view_comparison", Path(__file__).resolve().parents[2] / "scripts/check_handoff_views.py")
module = module_from_spec(spec)
spec.loader.exec_module(module)


def test_display_merge_preserves_distinct_event_revisions():
    card = {"sample_id": "a", "label": "A", "records": [{"event_id": "one", "revision": 2,
            "fields": {"v": 38}, "related_events": [{"event_id": "one", "revision": 2},
            {"event_id": "two", "revision": 1}]}]}
    context = {"status": "complete", "events": [{"event_id": "one", "revision": 2, "fields": {"v": 38}},
               {"event_id": "two", "revision": 1, "fields": {"v": 38}}]}
    result = module.compare_views(card, context)
    assert result["consistent"] and result["matched_events"] == 2
    context["events"][0]["revision"] = 1
    assert module.compare_views(card, context)["mismatches"] == [{"event_id": "one", "field": "revision"}]


def test_partial_context_or_missing_event_is_not_consistent():
    card = {"sample_id": "a", "label": "A", "records": [{"event_id": "one", "revision": 1}]}
    assert not module.compare_views(card, {"status": "complete", "events": []})["consistent"]
    assert not module.compare_views(card, {"status": "partial", "events": card["records"]})["consistent"]
