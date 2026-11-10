"""Benchmark output is public evidence, never internal provider state."""
import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("notebook_benchmark", Path(__file__).resolve().parents[2] / "scripts/benchmark_notebook_images.py")
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_public_export_decodes_receipts_and_removes_private_provider_data():
    receipt = {"items_json": json.dumps([{"raw_text": "real source", "unknown": None}]),
        "result_json": json.dumps({"text": "answer", "api_key": "other-secret", "run_token": "child-secret",
            "reasoning_content": "hidden-provider-state", "content": [
                {"type": "thinking", "thinking": "hidden-provider-state", "thinkingSignature": "signature"},
                {"type": "text", "text": "public answer"}], "usage": {"reasoning": 12}})}
    public = benchmark.public_export(receipt)
    assert public["items"][0]["raw_text"] == "real source"
    assert public["items"][0]["unknown"] is None
    assert public["result"]["content"] == [{"type": "text", "text": "public answer"}]
    assert public["result"]["usage"]["reasoning"] == 12
    encoded = json.dumps(public)
    assert "secret" not in encoded and "hidden-provider-state" not in encoded and "signature" not in encoded


def test_atomic_export_is_parseable_and_redacts_known_transport_key(tmp_path):
    path = tmp_path / "results.json"
    benchmark.atomic_save(path, benchmark.public_export({"text": "known-transport-key", "value": 1}, ("known-transport-key",)))
    assert json.loads(path.read_text(encoding="utf-8")) == {"text": "[redacted]", "value": 1}
    assert not path.with_name("results.json.tmp").exists()
