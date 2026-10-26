"""Personal local transport. It neither reads 1C databases nor executes tools."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from accounts import API_RESOURCE, ChatGPTAccount, ProtectedStore
from protocol import ProtocolError, chat_to_responses, read_sse, responses_to_chat


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class PersonalAdapter:
    def __init__(self, store, account, http=None):
        self.store = store
        self.account = account
        self.http = http or requests.Session()
        self.inference_lock = threading.Lock()

    def models(self):
        glm = self.store.read("glm-client") or {}
        personal_models = [{"id": "glm/" + model, "object": "model", "display_name": model}
                           for model in glm.get("models", [])]
        if self.store.read("chatgpt") is None and personal_models:
            return {"object": "list", "data": personal_models}
        token = self.account.access_token()
        response = self.http.get(API_RESOURCE + "/models", headers={"Authorization": "Bearer " + token},
                                 timeout=30, allow_redirects=False)
        if response.status_code != 200:
            raise ProtocolError("models_unavailable", "ChatGPT model catalog is unavailable")
        catalog = response.json().get("models")
        if not isinstance(catalog, list):
            raise ProtocolError("invalid_catalog", "ChatGPT returned an unsupported model catalog")
        return {"object": "list", "data": [{"id": model["slug"], "object": "model",
            "display_name": model.get("display_name", model["slug"])} for model in catalog
            if model.get("visibility") == "list" and isinstance(model.get("slug"), str)] + personal_models}

    def complete(self, request_id, body):
        try:
            request_id = str(uuid.UUID(request_id))
        except ValueError as error:
            raise ProtocolError("invalid_request_id", "A UUID X-Iikona-Request-ID is required") from error
        request_hash = digest(body)
        receipt_name = "request-" + request_id
        with self.inference_lock:
            connection = self.connection_identity(body)
            previous = self.store.read(receipt_name)
            if previous:
                if previous.get("connection") != connection:
                    raise ProtocolError("request_connection_changed", "The request belongs to another personal connection")
                if previous["request_hash"] != request_hash:
                    raise ProtocolError("request_id_conflict", "Request ID was used for different content")
                if previous.get("completion"):
                    return previous["completion"]
                raise ProtocolError("previous_outcome_unknown", "Inspect the previous request before sending again",
                    response_id=previous.get("response_id", ""), outcome=previous.get("outcome", "unknown"))
            if isinstance(body, dict) and str(body.get("model", "")).startswith("glm/"):
                return self._complete_glm(receipt_name, request_id, request_hash, body, connection)
            plain = chat_to_responses(body)
            native = dict(plain)
            native["input"] = self._restore_reasoning(plain, connection)
            # Resolve authorization before recording an attempted inference.
            token = self.account.access_token()
            receipt = {"request_hash": request_hash, "outcome": "unknown", "response_id": "",
                       "connection": connection, "model": plain["model"]}
            self.store.write(receipt_name, receipt)
            try:
                with self.http.post(API_RESOURCE + "/responses", json=native,
                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                    stream=True, timeout=(15, 300), allow_redirects=False) as response:
                    if response.status_code != 200:
                        receipt["http_status"] = response.status_code
                        receipt["outcome"] = "received" if response.status_code < 500 else "unknown"
                        code = "subscription_limit" if response.status_code == 429 else "provider_http_error"
                        raise ProtocolError(code, "Provider rejected the request; no automatic resend",
                                            outcome=receipt["outcome"])
                    response.encoding = "utf-8"
                    def captured_lines():
                        for line in response.iter_lines():
                            self.store.append_raw("wire-" + request_id, line + b"\n")
                            yield line
                    def remember_event(event):
                        upstream = event.get("response") or {}
                        if upstream.get("id"):
                            receipt["response_id"] = upstream["id"]
                    raw = read_sse(captured_lines(), remember_event)
                self.store.write("upstream-" + request_id, raw)
                allowed = {tool["name"] for tool in native.get("tools", [])}
                completion = responses_to_chat(raw, allowed)
                self._save_reasoning(plain, native, raw, completion, connection)
                receipt.update(outcome="received", response_id=raw.get("id", ""), completion=completion)
                self.store.write(receipt_name, receipt)
                return completion
            except ProtocolError as error:
                receipt.update(outcome=error.outcome, response_id=error.response_id, error=error.code)
                self.store.write(receipt_name, receipt)
                raise
            except (requests.RequestException, ValueError, KeyError, TypeError) as error:
                receipt["cause_type"] = type(error).__name__
                receipt["cause_message"] = str(error)
                self.store.write(receipt_name, receipt)
                raise ProtocolError("transport_interrupted", "The provider outcome is unknown; no automatic resend",
                                    response_id=receipt["response_id"], outcome="unknown") from error

    def connection_identity(self, body):
        if isinstance(body, dict) and str(body.get("model", "")).startswith("glm/"):
            settings = self.store.read("glm-client")
            if not settings or not settings.get("connection_id"):
                raise ProtocolError("glm_not_configured", "Configure the personal GLM client first")
            return "glm:" + settings["connection_id"]
        account = self.store.read("chatgpt")
        if not account or not all(account.get(key) for key in ("issuer", "client_id", "subject", "ext_agent_host_id")):
            raise ProtocolError("login_required", "Sign in with ChatGPT first")
        if "chatgpt.tokens.use.direct" not in account.get("scopes", []):
            raise ProtocolError("plan_permission_required", "ChatGPT plan usage was not authorized")
        return "chatgpt:" + digest({key: account[key] for key in ("issuer", "client_id", "subject", "ext_agent_host_id")})

    def request_receipt(self, request_id):
        receipt = self.store.read("request-" + request_id)
        if receipt is not None and receipt.get("connection") != self.connection_identity({"model": receipt.get("model", "")}):
            raise ProtocolError("request_connection_changed", "The request belongs to another personal connection")
        return receipt

    def _complete_glm(self, receipt_name, request_id, request_hash, body, connection):
        from glm_client import GLMClient
        receipt = {"request_hash": request_hash, "outcome": "unknown", "response_id": "",
                   "connection": connection, "model": body["model"]}
        self.store.write(receipt_name, receipt)
        try:
            completion = GLMClient(self.store).complete(body, request_id)
            receipt.update(outcome="received", response_id=completion["id"], completion=completion)
            self.store.write(receipt_name, receipt)
            return completion
        except ProtocolError as error:
            receipt.update(outcome=error.outcome, response_id=error.response_id, error=error.code)
            self.store.write(receipt_name, receipt)
            raise

    def _restore_reasoning(self, plain, connection):
        items = plain["input"]
        for item in reversed(items):
            if item.get("type") != "function_call_output":
                continue
            saved = self.store.read("tool-" + digest(item["call_id"]))
            if saved is None:
                raise ProtocolError("continuation_missing", "The tool continuation is unavailable; inspect the dialog")
            prefix = saved["replay_prefix"]
            if saved.get("connection") != connection or saved["model"] != plain["model"] or items[:len(prefix)] != prefix:
                raise ProtocolError("continuation_mismatch", "The tool result belongs to another model request")
            return saved["native_prefix"] + items[len(prefix):]
        return items

    def _save_reasoning(self, plain, native, raw, completion, connection):
        message = completion["choices"][0]["message"]
        calls = message.get("tool_calls", [])
        if not calls:
            return
        replay = list(plain["input"])
        if message.get("content"):
            replay.append({"role": "assistant", "content": message["content"]})
        replay.extend({"type": "function_call", "call_id": call["id"],
            "name": call["function"]["name"], "arguments": call["function"]["arguments"]} for call in calls)
        saved = {"model": plain["model"], "connection": connection, "replay_prefix": replay,
                 "native_prefix": native["input"] + raw["output"]}
        for call in calls:
            self.store.write("tool-" + digest(call["id"]), saved)


class AdapterHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        # Callback URLs and authorization headers must never enter access logs.
        pass

    @property
    def adapter(self):
        return self.server.adapter

    def reply(self, status, value, content_type="application/json; charset=utf-8"):
        raw = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, dict) else value.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        expected = "Bearer " + self.adapter.account.host["pairing_token"]
        actual = self.headers.get("Authorization", "")
        return secrets.compare_digest(actual, expected)

    def dispatch(self, method):
        parsed = urlsplit(self.path)
        if method == "GET" and parsed.path == "/auth/callback":
            query = parse_qs(parsed.query, keep_blank_values=True)
            if any(len(values) != 1 for values in query.values()):
                raise ProtocolError("invalid_callback", "Duplicate callback parameters")
            with self.adapter.inference_lock:
                self.adapter.account.finish_login({key: values[0] for key, values in query.items()})
            return self.reply(200, "ChatGPT connected to Iikona. You may close this tab.", "text/plain; charset=utf-8")
        if not self.authorized():
            return self.reply(401, {"error": {"code": "pairing_required", "message": "Personal pairing is required"}})
        if method == "GET" and parsed.path == "/connection/status":
            return self.reply(200, self.adapter.account.status())
        if method == "GET" and parsed.path == "/v1/models":
            return self.reply(200, self.adapter.models())
        if method == "GET" and parsed.path.startswith("/requests/"):
            try:
                request_id = str(uuid.UUID(parsed.path.removeprefix("/requests/")))
            except ValueError as error:
                raise ProtocolError("invalid_request_id", "A request UUID is required") from error
            receipt = self.adapter.request_receipt(request_id)
            if receipt is None:
                return self.reply(404, {"error": {"code": "request_not_found", "message": "Request not found"}})
            return self.reply(200, {key: receipt[key] for key in
                ("outcome", "response_id", "error", "http_status", "completion") if key in receipt})
        if method == "POST" and parsed.path == "/connection/login":
            callback = f"http://127.0.0.1:{self.server.server_port}/auth/callback"
            with self.adapter.inference_lock:
                login = self.adapter.account.start_login(callback)
            return self.reply(200, login)
        if method == "POST" and parsed.path == "/connection/logout":
            with self.adapter.inference_lock:
                self.adapter.account.logout()
            return self.reply(200, self.adapter.account.status())
        if method == "POST" and parsed.path == "/maintenance/receipts":
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 4096:
                raise ValueError("Invalid maintenance request size")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict) or set(body) - {"older_than_days", "apply_plan_sha256"}:
                raise ValueError("Invalid receipt maintenance parameters")
            from receipt_maintenance import maintain_receipts
            with self.adapter.inference_lock:
                result = maintain_receipts(self.adapter.store, body.get("older_than_days", 90),
                                           body.get("apply_plan_sha256"))
            return self.reply(200, result)
        if method == "POST" and parsed.path == "/v1/chat/completions":
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 16 * 1024 * 1024:
                raise ProtocolError("invalid_size", "Request body must be between 1 byte and 16 MiB")
            body = json.loads(self.rfile.read(size))
            request_id = self.headers.get("X-Iikona-Request-ID", "")
            return self.reply(200, self.adapter.complete(request_id, body))
        return self.reply(404, {"error": {"code": "unknown_route", "message": "Unknown adapter route"}})

    def handle_request(self, method):
        try:
            self.dispatch(method)
        except ProtocolError as error:
            status = 409 if error.outcome == "unknown" else 400
            self.reply(status, {"error": {"code": error.code, "message": str(error)},
                               "iikona": {"outcome": error.outcome, "response_id": error.response_id}})
        except (ValueError, TypeError):
            self.reply(400, {"error": {"code": "invalid_request", "message": "Invalid request"}})
        except (requests.RequestException, OSError):
            self.reply(503, {"error": {"code": "adapter_unavailable", "message": "Personal connection is unavailable"}})

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, help="Dedicated private directory, never an existing Codex home")
    parser.add_argument("--port", type=int, default=18743)
    parser.add_argument("--stop-file", help="Optional private supervisor signal for graceful shutdown")
    args = parser.parse_args()
    store = ProtectedStore(args.data)
    account = ChatGPTAccount(store)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), AdapterHandler)
    server.daemon_threads = False
    server.adapter = PersonalAdapter(store, account)
    stopped = threading.Event()
    if args.stop_file:
        stop_file = Path(args.stop_file)
        def supervisor():
            while not stopped.wait(0.5):
                if stop_file.exists():
                    server.shutdown()
                    return
        threading.Thread(target=supervisor, daemon=True).start()
    print(json.dumps({"address": f"127.0.0.1:{server.server_port}", "connected": account.status()["connected"],
                      "pairing_record": str(store.directory / "host.dpapi")}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        server.server_close()


if __name__ == "__main__":
    main()
