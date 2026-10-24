"""Optional real-client contract against a local Responses endpoint, without inference.

Set IIKONA_GLM_TEST_CLIENT to the supported client binary. No account is needed.
The assertion observes the HTTP body emitted by that client, not an adapter DTO.
"""
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
import uuid

if os.environ.get("IIKONA_GLM_TEST_SOURCE"):
    sys.path.insert(0, os.environ["IIKONA_GLM_TEST_SOURCE"])
from glm_client import GLMClient


def reply_stream():
    message = {"id": "msg-fixture", "type": "message", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": "wire_ok", "annotations": []}]}
    response = {"id": "resp-fixture", "object": "response", "status": "completed", "model": "glm-5.3",
                "output": [message], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    events = [
        {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
        {"type": "response.output_item.added", "output_index": 0, "item": {**message, "status": "in_progress", "content": []}},
        {"type": "response.output_text.delta", "item_id": message["id"], "output_index": 0, "content_index": 0, "delta": "wire_ok"},
        {"type": "response.output_item.done", "output_index": 0, "item": message},
        {"type": "response.completed", "response": response}]
    return "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events).encode()


class Store:
    def __init__(self, directory, settings):
        self.directory = Path(directory)
        self.values = {"glm-client": settings}

    def read(self, name):
        return copy.deepcopy(self.values.get(name))

    def write(self, name, value):
        self.values[name] = copy.deepcopy(value)

    def append_raw(self, name, raw):
        self.values.setdefault(name, []).append(raw)


@unittest.skipUnless(os.environ.get("IIKONA_GLM_TEST_CLIENT"), "Set IIKONA_GLM_TEST_CLIENT for native wire verification")
class GLMWireTests(unittest.TestCase):
    def capture(self, messages):
        captured = []
        wire = reply_stream()

        class Sink(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="iikona-glm-wire-") as folder:
                settings = {"binary": os.environ["IIKONA_GLM_TEST_CLIENT"], "models": ["glm-5.3"], "provider": {
                    "name": "Local wire fixture", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    "wire_api": "responses", "supports_websockets": False}}
                store = Store(folder, settings)
                request_id = str(uuid.uuid4())
                answer = GLMClient(store).complete({"model": "glm/glm-5.3", "messages": messages}, request_id)
                self.assertEqual(answer["choices"][0]["message"]["content"], "wire_ok")
                self.assertEqual(store.read("glm-process-exit-" + request_id)["exit_code"], 0)
            self.assertEqual(len(captured), 1)
            destination = os.environ.get("IIKONA_GLM_TEST_OUTPUT")
            if destination:
                path = Path(destination)
                path.mkdir(parents=True, exist_ok=True)
                (path / (self._testMethodName + ".json")).write_text(json.dumps(captured[0], ensure_ascii=False, indent=2), encoding="utf-8")
            return captured[0]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_system_and_developer_instructions_reach_actual_request(self):
        body = self.capture([{"role": "system", "content": "SYSTEM_CONTRACT_77"},
                             {"role": "developer", "content": "DEVELOPER_CONTRACT_78"},
                             {"role": "user", "content": "USER_DATA_79"}])
        self.assertEqual(body["instructions"], "SYSTEM_CONTRACT_77")
        by_role = lambda role: json.dumps([m for m in body["input"] if m.get("role") == role])
        self.assertIn("DEVELOPER_CONTRACT_78", by_role("developer"))
        self.assertIn("USER_DATA_79", by_role("user"))
        self.assertNotIn("SYSTEM_CONTRACT_77", by_role("user"))
        self.assertNotIn("<skills_instructions>", json.dumps(body))

    def test_system_instruction_does_not_replace_conversation_history(self):
        body = self.capture([{"role": "system", "content": "SYSTEM_HISTORY_81"},
                             {"role": "user", "content": "OLD_USER_82"},
                             {"role": "assistant", "content": "OLD_ASSISTANT_83"},
                             {"role": "user", "content": "NEW_USER_84"}])
        self.assertEqual(body["instructions"], "SYSTEM_HISTORY_81")
        visible = json.dumps(body["input"])
        for text in ["OLD_USER_82", "OLD_ASSISTANT_83", "NEW_USER_84"]:
            self.assertIn(text, visible)
        self.assertNotIn("SYSTEM_HISTORY_81", visible)
        self.assertNotIn("<skills_instructions>", visible)


if __name__ == "__main__":
    unittest.main()
