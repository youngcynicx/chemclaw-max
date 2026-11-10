"""Configuration must reach the actual transport, including a separate vision API."""
import http.client
import json
from pathlib import Path
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dev_server as bridge


class ModelProfilesTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append({'path': self.path, 'body': body,
                                 'auth': self.headers.get('Authorization')})
                if body['model'] == 'eyes' and body.get('max_tokens', 0) > 1024:
                    payload = {'error': {'message': 'vision output budget exceeds 1024'}}
                    status = 400
                elif body['model'] == 'rejected':
                    payload = {'error': {'message': 'unsupported model'}}
                    status = 400
                elif self.path.endswith('/responses'):
                    payload = {'status': 'completed', 'output': [{'type': 'message',
                        'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'OK'}]}]}
                    status = 200
                else:
                    payload = {'choices': [{'finish_reason': 'stop', 'message': {'content': 'OK'}}]}
                    status = 200
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        threading.Thread(target=self.provider.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.provider.server_port}/v1'
        self.addCleanup(self.provider.server_close)
        self.addCleanup(self.provider.shutdown)

    def test_responses_prefix_and_explicit_effort_reach_the_wire(self):
        config = bridge.validate_model_config({'baseUrl': self.base + '/responses',
            'protocol': 'openai-responses', 'model': 'custom-model', 'apiKey': 'main-fixture',
            'reasoningEffort': 'medium', 'maxTokens': 2000, 'contextWindow': 32000}, 4174)
        self.assertEqual(config.base_url, self.base)
        self.assertEqual(bridge.call_model(config, [{'role': 'user', 'content': 'OK'}]), 'OK')
        request = self.requests[-1]
        self.assertEqual(request['path'], '/v1/responses')
        self.assertEqual(request['body']['reasoning'], {'effort': 'medium'})
        self.assertEqual(request['body']['max_output_tokens'], 2000)
        self.assertNotIn('reasoning_effort', request['body'])
        self.assertEqual(request['auth'], 'Bearer main-fixture')

    def test_separate_vision_transport_does_not_inherit_main_reasoning_or_secret(self):
        config = bridge.validate_model_config({'baseUrl': self.base, 'model': 'chat',
            'apiKey': 'main-fixture', 'reasoningFormat': 'deepseek', 'reasoningEffort': 'high',
            'visionBaseUrl': self.base + '/vision/chat/completions', 'visionModel': 'eyes',
            'visionApiKey': 'vision-fixture'}, 4174)
        bridge.call_model(config, [{'role': 'user', 'content': 'OK'}], vision=True, max_tokens=1024)
        request = self.requests[-1]
        self.assertEqual(request['path'], '/v1/vision/chat/completions')
        self.assertEqual(request['auth'], 'Bearer vision-fixture')
        self.assertEqual(request['body']['model'], 'eyes')
        self.assertNotIn('thinking', request['body'])
        self.assertNotIn('reasoning_effort', request['body'])
        public = json.dumps(bridge.public_model_config(config))
        self.assertNotIn('main-fixture', public)
        self.assertNotIn('vision-fixture', public)
        self.assertNotIn('main-fixture', repr(config))

    def test_blank_keys_reuse_only_the_same_normalized_endpoint(self):
        old = bridge.validate_model_config({'baseUrl': 'https://example.com/v1',
            'model': 'a', 'apiKey': 'private-fixture'}, 4174)
        same = bridge.validate_model_config({'baseUrl': 'https://example.com/v1/chat/completions',
            'model': 'b', 'apiKey': ''}, 4174, old)
        self.assertEqual(same.api_key, old.api_key)
        for changes in ({'baseUrl': 'https://different.example/v1'},
                        {'visionBaseUrl': 'https://different.example/v1'}):
            with self.subTest(changes=changes), self.assertRaises(bridge.BridgeError):
                bridge.validate_model_config({'baseUrl': old.base_url, 'model': 'b',
                    'apiKey': '', **changes}, 4174, old)

    def test_local_no_auth_and_invalid_protocol_combinations(self):
        config = bridge.validate_model_config({'baseUrl': self.base, 'model': 'local',
            'apiKey': '', 'reasoningFormat': 'none', 'maxTokens': 1024, 'contextWindow': 8192}, 4174)
        bridge.call_model(config, [{'role': 'user', 'content': 'OK'}])
        self.assertIsNone(self.requests[-1]['auth'])
        self.assertNotIn('reasoning_effort', self.requests[-1]['body'])
        for changes in ({'baseUrl': self.base + '/responses'},
                        {'protocol': 'openai-responses', 'reasoningFormat': 'qwen'},
                        {'reasoningFormat': 'qwen', 'reasoningEffort': 'high'},
                        {'maxTokens': True}):
            with self.subTest(changes=changes), self.assertRaises(bridge.BridgeError):
                bridge.validate_model_config({'baseUrl': self.base, 'model': 'local',
                    'apiKey': '', **changes}, 4174)

    def test_failed_vision_probe_keeps_active_model_and_returns_public_status(self):
        server = bridge.BridgeServer(('127.0.0.1', 0), Path(__file__).resolve().parents[1] / 'dist')
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        self.addCleanup(client.close)
        client.request('GET', '/api/session')
        response = client.getresponse()
        session = json.loads(response.read())
        headers = {'Content-Type': 'application/json',
            'Origin': f'http://127.0.0.1:{server.server_port}',
            'Cookie': response.getheader('Set-Cookie').split(';', 1)[0],
            'X-ChemClaw-Session': session['csrfToken']}

        def configure(payload):
            client.request('POST', '/api/model/configure', json.dumps(payload), headers)
            response = client.getresponse()
            return response.status, json.loads(response.read())

        body = {'baseUrl': self.base, 'model': 'active-model', 'apiKey': 'main-fixture',
            'visionModel': 'eyes', 'visionApiKey': 'vision-fixture'}
        status, result = configure(body)
        self.assertEqual(status, 200)
        self.assertEqual(result['visionTest'], 'ok')
        status, error = configure({**body, 'model': 'replacement', 'visionModel': 'rejected'})
        self.assertEqual(status, 502)
        self.assertNotIn('fixture', json.dumps(error))
        client.request('GET', '/api/model/status', headers=headers)
        response = client.getresponse()
        active = json.loads(response.read())
        self.assertEqual(active['model'], 'active-model')
        self.assertEqual(active['visionModel'], 'eyes')
        self.assertNotIn('fixture', json.dumps(active))


if __name__ == '__main__':
    unittest.main()
