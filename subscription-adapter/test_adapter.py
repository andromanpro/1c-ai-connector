"""Contract and failure tests based on documented OAuth and Responses events."""
import copy
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
import requests

from accounts import AUTHORITY, ChatGPTAccount, DIRECT_SCOPE, ProtectedStore
from protocol import ProtocolError, chat_to_responses, read_sse, responses_to_chat
import server as adapter_server
from server import AdapterHandler, PersonalAdapter


class MemoryStore:
    def __init__(self):
        self.values = {}

    def read(self, name):
        return copy.deepcopy(self.values.get(name))

    def write(self, name, value):
        self.values[name] = copy.deepcopy(value)

    def delete(self, name):
        self.values.pop(name, None)

    def append_raw(self, name, raw):
        self.values.setdefault(name, []).append(raw)


def event(kind, response):
    return "data: " + json.dumps({"type": kind, "response": response}) + "\n\n"


def completed(text="Ответ", output=None):
    return {"id": "resp-contract", "status": "completed", "model": "test-model", "created_at": 1792750000,
        "output": output if output is not None else [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}}


class SinkHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        raw = self.server.replies.pop(0).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ProtocolTests(unittest.TestCase):
    def test_only_supplied_context_and_tools_are_sent(self):
        body = {"model": "test-model", "messages": [{"role": "system", "content": "Правила эксперта"},
            {"role": "user", "content": "Покажи справочники"}],
            "tools": [{"type": "function", "function": {"name": "list_metadata", "parameters": {"type": "object"}}}]}
        result = chat_to_responses(body)
        self.assertEqual(result["input"], body["messages"])
        self.assertEqual([tool["name"] for tool in result["tools"]], ["list_metadata"])
        self.assertFalse(result["store"])
        self.assertTrue(result["stream"])
        self.assertNotIn("instructions", result)

    def test_historical_call_and_result_keep_identifiers(self):
        body = {"model": "test-model", "messages": [{"role": "user", "content": "Список"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function",
                "function": {"name": "list_metadata", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "Справочник.Номенклатура"}]}
        result = chat_to_responses(body)
        self.assertEqual(result["input"][1]["call_id"], "call-1")
        self.assertEqual(result["input"][2], {"type": "function_call_output", "call_id": "call-1",
                                            "output": "Справочник.Номенклатура"})

    def test_missing_or_foreign_tool_results_are_rejected(self):
        for messages in [[{"role": "tool", "tool_call_id": "foreign", "content": "x"}],
            [{"role": "assistant", "tool_calls": [{"id": "x", "type": "function",
                "function": {"name": "tool", "arguments": "{}"}}]}]]:
            with self.subTest(messages=messages), self.assertRaises(ProtocolError):
                chat_to_responses({"model": "test-model", "messages": messages})

    def test_builtins_and_unsupported_content_are_rejected(self):
        with self.assertRaises(ProtocolError):
            chat_to_responses({"model": "test-model", "messages": [{"role": "user", "content": "x"}],
                               "tools": [{"type": "web_search"}]})
        with self.assertRaises(ProtocolError):
            chat_to_responses({"model": "test-model", "messages": [{"role": "user", "content": [{"type": "image_url"}]}]})

    def test_delta_without_completed_is_not_success(self):
        raw = event("response.created", {"id": "resp-unknown"}) + 'data: {"type":"response.output_text.delta","delta":"42"}\n\n'
        with self.assertRaises(ProtocolError) as caught:
            read_sse(raw.splitlines())
        self.assertEqual(caught.exception.response_id, "resp-unknown")
        self.assertEqual(caught.exception.outcome, "unknown")

    def test_late_subscription_error_overrides_partial_text(self):
        raw = 'data: {"type":"response.output_text.delta","delta":"42"}\n\n'
        raw += event("response.failed", {"id": "resp-limit", "error": {"code": "subscription_sharing_usage_limit_exceeded"}})
        with self.assertRaises(ProtocolError) as caught:
            read_sse(raw.splitlines())
        self.assertEqual(caught.exception.code, "subscription_sharing_usage_limit_exceeded")

    def test_siwc_completed_items_are_read_when_terminal_output_is_empty(self):
        item = {"type": "function_call", "id": "fc-real-shape", "status": "completed",
                "name": "kii_echo", "call_id": "call-real-shape", "arguments": '{"text":"Iikona"}'}
        raw = 'data: ' + json.dumps({"type": "response.output_item.done", "output_index": 0, "item": item}) + '\n\n'
        raw += event("response.completed", completed(output=[]))
        parsed = read_sse(raw.splitlines())
        result = responses_to_chat(parsed, {"kii_echo"})
        self.assertEqual(result["choices"][0]["message"]["tool_calls"][0]["id"], "call-real-shape")
        with self.assertRaises(ProtocolError):
            read_sse(raw.split('data: {"type": "response.completed"')[0].splitlines())

    def test_usage_and_unknown_cost_are_distinct(self):
        raw = completed()
        result = responses_to_chat(read_sse(event("response.completed", raw).splitlines()), set())
        self.assertEqual(result["usage"]["total_tokens"], 13)
        self.assertIsNone(result["iikona"]["cost"])
        self.assertNotIn("cost", result["usage"])

    def test_unoffered_tool_is_rejected(self):
        raw = completed(output=[{"type": "function_call", "name": "shell", "call_id": "x", "arguments": "{}"}])
        with self.assertRaises(ProtocolError):
            responses_to_chat(raw, {"list_metadata"})


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
        self.sink.requests, self.sink.replies = [], []
        self.worker = threading.Thread(target=self.sink.serve_forever, daemon=True)
        self.worker.start()
        self.old_resource = adapter_server.API_RESOURCE
        adapter_server.API_RESOURCE = f"http://127.0.0.1:{self.sink.server_port}/v1"
        self.store = MemoryStore()
        self.account = ChatGPTAccount(self.store)
        self.store.write("chatgpt", {"access_token": "local-sink-only", "expires_at": time.time() + 3600,
            "issuer": AUTHORITY, "client_id": "oaiapp_test", "subject": "first-user",
            "ext_agent_host_id": "urn:uuid:test-host", "scopes": [DIRECT_SCOPE]})
        http = requests.Session()
        http.trust_env = False
        self.adapter = PersonalAdapter(self.store, self.account, http)
        self.body = {"model": "test-model", "messages": [{"role": "user", "content": "Только этот текст"}]}

    def tearDown(self):
        adapter_server.API_RESOURCE = self.old_resource
        self.sink.shutdown()
        self.sink.server_close()
        self.worker.join(timeout=3)

    def test_real_http_request_and_idempotent_result(self):
        self.sink.replies.append(event("response.completed", completed()))
        request_id = str(uuid.uuid4())
        first = self.adapter.complete(request_id, self.body)
        second = self.adapter.complete(request_id, self.body)
        self.assertEqual(first, second)
        self.assertEqual(len(self.sink.requests), 1)
        self.assertEqual(self.sink.requests[0]["input"], self.body["messages"])
        self.assertNotIn("tools", self.sink.requests[0])

    def test_interrupted_request_is_not_resent(self):
        self.sink.replies.append(event("response.created", {"id": "resp-uncertain"}))
        request_id = str(uuid.uuid4())
        for _ in range(2):
            with self.assertRaises(ProtocolError):
                self.adapter.complete(request_id, self.body)
        self.assertEqual(len(self.sink.requests), 1)
        self.assertTrue(self.store.values["wire-" + request_id])

    def test_invalid_answer_is_preserved_before_content_validation(self):
        upstream = completed(output=[])
        self.sink.replies.append(event("response.completed", upstream))
        request_id = str(uuid.uuid4())
        with self.assertRaises(ProtocolError):
            self.adapter.complete(request_id, self.body)
        self.assertEqual(self.store.read("upstream-" + request_id), upstream)
        self.assertTrue(self.store.values["wire-" + request_id])

    def test_other_personal_connection_cannot_restore_a_tool(self):
        body = {"model": "test-model", "messages": [{"role": "user", "content": "List"},
            {"role": "assistant", "content": None, "tool_calls": [{"type": "function", "id": "foreign-call",
                "function": {"name": "list_metadata", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "foreign-call", "content": "Nomenclature"}]}
        with self.assertRaises(ProtocolError):
            self.adapter.complete(str(uuid.uuid4()), body)
        self.assertEqual(self.sink.requests, [])

    def test_glm_uses_its_supported_client_without_chatgpt_authorization(self):
        self.store.delete("chatgpt")
        self.store.write("glm-client", {"connection_id": "personal-glm"})
        result = {"id": "glm-local", "choices": [{"message": {"role": "assistant", "content": "GLM"}}]}
        request_id = str(uuid.uuid4())
        body = {**self.body, "model": "glm/glm-5.3"}
        with patch("glm_client.GLMClient.complete", return_value=result) as complete:
            self.assertEqual(self.adapter.complete(request_id, body), result)
            self.assertEqual(self.adapter.complete(request_id, body), result)
            self.assertEqual(complete.call_count, 1)
        self.assertEqual(self.sink.requests, [])

    def test_glm_model_catalog_does_not_require_a_chatgpt_account(self):
        self.account.logout()
        self.store.write("glm-client", {"connection_id": "personal-glm", "models": ["glm-5.3"]})
        self.assertEqual(self.adapter.models()["data"], [
            {"id": "glm/glm-5.3", "object": "model", "display_name": "glm-5.3"}])
        self.assertEqual(self.sink.requests, [])

    def test_logout_cannot_replay_or_read_a_saved_answer(self):
        self.sink.replies.append(event("response.completed", completed()))
        request_id = str(uuid.uuid4())
        self.adapter.complete(request_id, self.body)
        self.account.logout()
        with self.assertRaises(ProtocolError):
            self.adapter.complete(request_id, self.body)
        with self.assertRaises(ProtocolError):
            self.adapter.request_receipt(request_id)
        self.assertEqual(len(self.sink.requests), 1)

    def test_account_switch_cannot_replay_an_answer_or_tool_continuation(self):
        self.body["tools"] = [{"type": "function", "function": {
            "name": "list_metadata", "parameters": {"type": "object"}}}]
        output = [{"type": "function_call", "name": "list_metadata", "call_id": "call-other", "arguments": "{}"}]
        self.sink.replies.append(event("response.completed", completed(output=output)))
        request_id = str(uuid.uuid4())
        answer = self.adapter.complete(request_id, self.body)
        account = self.store.read("chatgpt")
        account["subject"] = "second-user"
        self.store.write("chatgpt", account)
        for operation in [lambda: self.adapter.complete(request_id, self.body),
                          lambda: self.adapter.request_receipt(request_id)]:
            with self.assertRaises(ProtocolError):
                operation()
        next_body = copy.deepcopy(self.body)
        next_body["messages"] += [answer["choices"][0]["message"],
            {"role": "tool", "tool_call_id": "call-other", "content": "Nomenclature"}]
        with self.assertRaises(ProtocolError):
            self.adapter.complete(str(uuid.uuid4()), next_body)
        self.assertEqual(len(self.sink.requests), 1)

    def test_glm_reconfiguration_cannot_replay_an_old_answer(self):
        self.store.write("glm-client", {"connection_id": "first-profile"})
        request_id = str(uuid.uuid4())
        body = {**self.body, "model": "glm/glm-5.3"}
        result = {"id": "glm-local", "choices": [{"message": {"role": "assistant", "content": "GLM"}}]}
        with patch("glm_client.GLMClient.complete", return_value=result) as complete:
            self.adapter.complete(request_id, body)
            self.store.write("glm-client", {"connection_id": "second-profile"})
            with self.assertRaises(ProtocolError):
                self.adapter.complete(request_id, body)
            with self.assertRaises(ProtocolError):
                self.adapter.request_receipt(request_id)
            self.assertEqual(complete.call_count, 1)

    def test_reuse_of_request_id_for_different_content_is_rejected(self):
        self.sink.replies.append(event("response.completed", completed()))
        request_id = str(uuid.uuid4())
        self.adapter.complete(request_id, self.body)
        with self.assertRaises(ProtocolError):
            self.adapter.complete(request_id, {**self.body, "model": "another"})
        self.assertEqual(len(self.sink.requests), 1)

    def test_native_reasoning_is_restored_for_1c_tool_result(self):
        tool = {"type": "function", "function": {"name": "list_metadata", "parameters": {"type": "object"}}}
        self.body["tools"] = [tool]
        first_output = [{"type": "reasoning", "id": "rs-1", "encrypted_content": "opaque-provider-context"},
            {"type": "function_call", "name": "list_metadata", "call_id": "call-1", "arguments": "{}"}]
        self.sink.replies.extend([event("response.completed", completed(output=first_output)),
                                  event("response.completed", completed(text="Номенклатура"))])
        answer = self.adapter.complete(str(uuid.uuid4()), self.body)
        next_body = copy.deepcopy(self.body)
        next_body["messages"] += [answer["choices"][0]["message"],
            {"role": "tool", "tool_call_id": "call-1", "content": "Справочник.Номенклатура"}]
        final = self.adapter.complete(str(uuid.uuid4()), next_body)
        self.assertEqual(final["choices"][0]["message"]["content"], "Номенклатура")
        self.assertEqual(self.sink.requests[1]["input"][1:3], first_output)
        self.assertEqual(self.sink.requests[1]["input"][-1]["call_id"], "call-1")


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.account = ChatGPTAccount(self.store)

    def test_state_mismatch_preserves_pending_login(self):
        self.account.start_login("http://127.0.0.1:1455/auth/callback")
        with self.assertRaises(ProtocolError):
            self.account.finish_login({"state": "wrong", "code": "unused"})
        self.assertIsNotNone(self.account.pending)

    def test_denied_consent_leaves_existing_account_unchanged(self):
        previous = {"client_id": "oaiapp_saved", "subject": "original", "id_token": "saved"}
        self.store.write("chatgpt", previous)
        self.account.start_login("http://127.0.0.1:1455/auth/callback")
        state = self.account.pending["state"]
        with self.assertRaises(ProtocolError):
            self.account.finish_login({"state": state, "error": "access_denied"})
        self.assertEqual(self.store.read("chatgpt"), previous)

    def test_direct_permission_is_required(self):
        with self.assertRaises(ProtocolError):
            ChatGPTAccount._token_record({"access_token": "local", "expires_in": 3600, "scope": "openid"})

    def test_identity_checks_signature_audience_issuer_and_nonce(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk["kid"] = "test-key"
        class Reply:
            def __init__(self, data): self.data = data
            def raise_for_status(self): pass
            def json(self): return self.data
        class Http:
            def get(self, url, **_):
                return Reply({"issuer": AUTHORITY, "jwks_uri": AUTHORITY + "/jwks"}
                    if "openid-configuration" in url else {"keys": [jwk]})
        self.account.http = Http()
        claims = {"sub": "person", "iss": AUTHORITY, "aud": "oaiapp_test", "nonce": "nonce",
                  "exp": int(time.time()) + 300, "iat": int(time.time())}
        token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})
        self.assertEqual(self.account._validate_identity(token, "oaiapp_test", "nonce")["sub"], "person")
        for field, replacement in [("aud", "other"), ("iss", "https://other.invalid"), ("nonce", "wrong"), ("exp", 1)]:
            wrong = jwt.encode({**claims, field: replacement}, key, algorithm="RS256", headers={"kid": "test-key"})
            with self.subTest(field=field), self.assertRaises(ProtocolError):
                self.account._validate_identity(wrong, "oaiapp_test", "nonce")

    def test_dpapi_roundtrip_does_not_write_plaintext(self):
        with tempfile.TemporaryDirectory(prefix="iikona-dpapi-") as directory:
            store = ProtectedStore(directory)
            store.write("probe", {"secret": "test-secret-not-a-credential"})
            self.assertNotIn(b"test-secret", (Path(directory) / "probe.dpapi").read_bytes())
            self.assertEqual(store.read("probe"), {"secret": "test-secret-not-a-credential"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
