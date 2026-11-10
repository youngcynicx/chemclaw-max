"""Session sources carry exact authorized text, never assistant recollections."""
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dev_server as bridge


class SourceContextTests(unittest.TestCase):
    def setUp(self):
        self.connection = bridge.CoreConnection("http://127.0.0.1:1", "fixture", "P1")

    def page(self, page_id, project="P1"):
        return {"page": {"page_id": page_id, "project_id": project, "active_revision": 2,
            "transcript": {"active_revision": 2, "blocks": [{"block_id": page_id + "_b",
                "text": "~~20 mg~~ → 25 mg; 120 C 4 h then 150 C 8 h",
                "reading_order": 0, "quality_flags": ["needs_human"],
                "extractor": {"large": "metadata" * 1000}}]}}}

    def test_history_sources_are_reread_and_keep_original_conditions_and_revisions(self):
        history = [{"role": "user", "context": {"pages": [{"page_id": "old"}]}},
                   {"role": "assistant", "text": "WRONG_SUMMARY", "context": {"pages": [{"page_id": "old"}]}}]
        with patch.object(bridge, "invoke_read", side_effect=lambda c, name, args: self.page(args["page_id"])) as read:
            packed = bridge.session_source_context(self.connection, {"history": history},
                {"pages": [{"page_id": "current"}], "source_pages": ["untrusted"]})
        self.assertEqual(read.call_count, 2)
        self.assertEqual([p["page_id"] for p in packed["source_pages"]], ["old", "current"])
        source = packed["source_pages"][0]
        self.assertEqual(source["transcript_revision"], 2)
        self.assertEqual(source["blocks"][0]["text"], self.page("old")["page"]["transcript"]["blocks"][0]["text"])
        self.assertEqual(source["blocks"][0]["quality_flags"], ["needs_human"])
        self.assertNotIn("extractor", source["blocks"][0])
        self.assertNotIn("WRONG_SUMMARY", json.dumps(packed))

    def test_denied_cross_project_or_untranscribed_pages_are_not_supplied(self):
        def read(connection, name, args):
            if args["page_id"] == "denied":
                raise bridge.BridgeError("forbidden", "private upstream detail", 403)
            if args["page_id"] == "empty":
                return {"page": {"project_id": "P1", "transcript": {}}}
            return self.page(args["page_id"], "P2")
        with patch.object(bridge, "invoke_read", side_effect=read):
            packed = bridge.session_source_context(self.connection, {},
                {"pages": [{"page_id": p} for p in ("denied", "other", "empty")]})
        self.assertEqual(packed["source_pages"], [])
        self.assertEqual(packed["source_omitted_pages"], ["denied", "other", "empty"])
        self.assertNotIn("private", json.dumps(packed))

    def test_limits_omit_whole_pages_and_cancellation_does_not_fetch_sources(self):
        context = {"pages": [{"page_id": p} for p in ("old", "a", "b")]}
        with patch.object(bridge, "invoke_read", side_effect=lambda c, name, args: self.page(args["page_id"])) as read:
            packed = bridge.session_source_context(self.connection, {}, context, max_pages=2, max_bytes=1)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(packed["source_pages"], [])
        self.assertEqual(packed["source_omitted_pages"], ["old", "a", "b"])
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(bridge, "invoke_read") as read:
            packed = bridge.session_source_context(self.connection, {}, context, cancelled=cancelled)
        read.assert_not_called()
        self.assertEqual(packed["source_omitted_pages"], ["old", "a", "b"])


if __name__ == "__main__":
    unittest.main()
