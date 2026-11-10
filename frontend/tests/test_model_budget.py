"""Model configuration failures must be caught before jobs save or start."""
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dev_server as bridge


class ModelBudgetTests(unittest.TestCase):
    def setUp(self):
        self.body = {"baseUrl": "https://open.bigmodel.cn/api/paas/v4",
                     "model": "glm-5.3-flash", "apiKey": "fixture-model-key"}

    def test_optional_config_keeps_existing_clients_and_explicit_budgets(self):
        original = bridge.validate_model_config(self.body, 4174)
        self.assertIsNone(original.reasoning_effort)
        self.assertEqual(original.max_tokens, 16384)
        selected = bridge.validate_model_config({**self.body, "reasoningEffort": "high",
            "maxTokens": 24000, "contextWindow": 160000}, 4174)
        self.assertEqual((selected.reasoning_effort, selected.max_tokens, selected.context_window),
                         ("high", 24000, 160000))

    def test_invalid_or_unsupported_budgets_reject_before_execution(self):
        invalid = [{"maxTokens": True}, {"maxTokens": "16384"}, {"maxTokens": -1},
            {"maxTokens": 140000}, {"contextWindow": 8192},
            {"maxTokens": 16384, "contextWindow": 16384},
            {"reasoningEffort": "off"}, {"reasoningEffort": "medium"},
            {"model": "glm-4v-flash", "reasoningEffort": "low"},
            {"baseUrl": "https://example.com/v1", "reasoningEffort": "low"}]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(bridge.BridgeError) as err:
                bridge.validate_model_config({**self.body, **changes}, 4174)
            self.assertEqual(err.exception.code, "invalid_model_config")

    def test_missing_required_field_with_optional_field_is_validated(self):
        body = {"baseUrl": self.body["baseUrl"], "apiKey": "fixture", "visionModel": "vision"}
        with self.assertRaises(bridge.BridgeError) as err:
            bridge.validate_model_config(body, 4174)
        self.assertEqual(err.exception.code, "invalid_model_config")

    def test_probe_native_effort_is_not_injected_into_other_vision_model(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"choices": [
            {"message": {"content": "OK"}, "finish_reason": "stop"}]}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        config = bridge.validate_model_config(self.body, 4174)
        with patch.object(bridge, "build_opener", return_value=opener):
            self.assertEqual(bridge.call_model(config, [{"role": "user", "content": "OK"}]), "OK")
            native = json.loads(opener.open.call_args.args[0].data)
            self.assertEqual(native["reasoning_effort"], "low")
            self.assertEqual(native["thinking"]["type"], "enabled")
            self.assertEqual(native["max_tokens"], 16384)
            bridge.call_model(config, [], model="glm-4v-flash", max_tokens=1024)
            vision = json.loads(opener.open.call_args.args[0].data)
            self.assertNotIn("reasoning_effort", vision)
            self.assertNotIn("thinking", vision)

    def transport(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"choices": [
            {"message": {"content": "OK"}, "finish_reason": "stop"}]}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        return opener

    def test_native_same_model_probe_and_ocr_keep_max_reasoning_and_configured_budget(self):
        config = bridge.validate_model_config({**self.body, "reasoningEffort": "max", "maxTokens": 16384,
            "visionModel": "glm-5.3-flash", "visionBaseUrl": self.body["baseUrl"] + "/chat/completions",
            "visionApiKey": "fixture-independent-vision-key"}, 4174)
        opener = self.transport()
        with patch.object(bridge, "build_opener", return_value=opener):
            self.assertEqual(bridge.probe_model(config), {"test": "ok", "visionTest": "ok"})
            blocks, _ = bridge._vision_text_blocks(config, {"page_id": "fixture", "mime": "image/png", "data_b64": "AA=="})
        self.assertEqual(len(blocks), 1)
        self.assertEqual(opener.open.call_count, 3)
        for index, call in enumerate(opener.open.call_args_list):
            request = call.args[0]
            body = json.loads(request.data)
            self.assertEqual(body["model"], "glm-5.3-flash")
            self.assertEqual(body["reasoning_effort"], "max")
            self.assertEqual(body["thinking"], {"type": "enabled"})
            self.assertEqual(body["max_tokens"], 16384)
            self.assertEqual(call.kwargs["timeout"], 240 if index == 2 else 30)
            self.assertEqual(request.get_header("Authorization"), "Bearer " +
                ("fixture-model-key" if index == 0 else "fixture-independent-vision-key"))
        self.assertIsInstance(json.loads(opener.open.call_args_list[2].args[0].data)["messages"][0]["content"], list)

    def test_old_glm_vision_keeps_1024_budget_without_main_max_reasoning(self):
        config = bridge.validate_model_config({**self.body, "reasoningEffort": "max", "visionModel": "glm-4v-flash",
            "visionApiKey": "fixture-legacy-vision-key"}, 4174)
        opener = self.transport()
        with patch.object(bridge, "build_opener", return_value=opener):
            bridge.probe_model(config)
            bridge._vision_text_blocks(config, {"page_id": "fixture", "mime": "image/png", "data_b64": "AA=="})
        main = json.loads(opener.open.call_args_list[0].args[0].data)
        self.assertEqual(main["reasoning_effort"], "max")
        self.assertEqual(main["max_tokens"], 16384)
        for call in opener.open.call_args_list[1:]:
            body = json.loads(call.args[0].data)
            self.assertEqual(body["model"], "glm-4v-flash")
            self.assertEqual(body["max_tokens"], 1024)
            self.assertNotIn("reasoning_effort", body)
            self.assertNotIn("thinking", body)
            self.assertEqual(call.args[0].get_header("Authorization"), "Bearer fixture-legacy-vision-key")

    def test_foreign_same_name_vision_does_not_inherit_native_reasoning_or_new_budget(self):
        config = bridge.validate_model_config({**self.body, "reasoningEffort": "max", "visionModel": "glm-5.3-flash",
            "visionBaseUrl": "https://vision.example/v1", "visionApiKey": "fixture-foreign-key"}, 4174)
        opener = self.transport()
        with patch.object(bridge, "build_opener", return_value=opener):
            bridge.probe_model(config)
            bridge._vision_text_blocks(config, {"page_id": "fixture", "mime": "image/png", "data_b64": "AA=="})
        for call in opener.open.call_args_list[1:]:
            request = call.args[0]
            body = json.loads(request.data)
            self.assertEqual(request.full_url, "https://vision.example/v1/chat/completions")
            self.assertEqual(request.get_header("Authorization"), "Bearer fixture-foreign-key")
            self.assertEqual(body["max_tokens"], 1024)
            self.assertNotIn("reasoning_effort", body)
            self.assertNotIn("thinking", body)

    def test_truncated_vision_reply_never_becomes_a_saved_transcript(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"choices": [
            {"message": {"content": "partial note"}, "finish_reason": "length"}]}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        config = bridge.ModelConfig("https://open.bigmodel.cn/api/paas/v4", "glm-5.3-flash",
                                    "fixture", vision_model="glm-4v-flash")
        with patch.object(bridge, "build_opener", return_value=opener), \
             patch.object(bridge, "invoke_read", return_value={"mime": "image/jpeg", "data_b64": "AA=="}), \
             patch.object(bridge, "invoke_internal_write") as write, self.assertRaises(bridge.ModelError) as err:
            bridge.vision_ocr_page(bridge.CoreConnection("http://127.0.0.1:1", "fixture", "P1"), config, "page1")
        self.assertEqual(err.exception.code, "model_output_truncated")
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
