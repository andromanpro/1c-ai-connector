"""Personal GLM Coding Plan through its supported Codex client, one model round."""
from pathlib import Path
import json
import os
import queue
import subprocess
import threading
import time
import uuid

from protocol import ProtocolError, chat_to_responses

DISABLED = ["hooks", "apps", "remote_plugin", "browser_use", "browser_use_external", "computer_use",
    "shell_tool", "unified_exec", "js_repl", "multi_agent", "skill_search", "view_image",
    "workspace_dependencies", "sleep_tool", "code_mode_host", "code_mode", "code_mode_only",
    "goals", "plugins", "memories", "mentions_v2", "skill_mcp_dependency_install",
    "in_app_chat", "in_app_browser", "in_app_local_automation", "tool_suggest"]


def toml_lines(data, prefix=()):
    result = []
    if prefix:
        result.append("[" + ".".join(json.dumps(key) for key in prefix) + "]")
    for key, value in data.items():
        if value is not None and not isinstance(value, dict):
            result.append(json.dumps(key) + "=" + json.dumps(value, ensure_ascii=False))
    for key, value in data.items():
        if isinstance(value, dict):
            result.extend(toml_lines(value, prefix + (key,)))
    return result


class GLMClient:
    def __init__(self, store):
        self.store = store

    def complete(self, body, request_id):
        settings = self.store.read("glm-client")
        if not settings:
            raise ProtocolError("glm_not_configured", "Configure the personal GLM client first")
        model = body["model"].removeprefix("glm/")
        if model not in settings["models"]:
            raise ProtocolError("glm_model_unavailable", "The GLM model is not configured for this personal connection")
        native = chat_to_responses({**body, "model": model})
        system_instructions = []
        developer_instructions = []
        conversation = []
        for item in native["input"]:
            if item.get("role") == "system":
                system_instructions.append(item["content"])
            elif item.get("role") == "developer":
                developer_instructions.append(item["content"])
            else:
                conversation.append(item)
        home = self.store.directory / ("glm-request-" + str(uuid.uuid4()))
        home.mkdir()
        scratch = home / "scratch"
        scratch.mkdir()
        config = {"model": model, "model_provider": "zai-coding", "model_providers": {
            "zai-coding": {**settings["provider"], "request_max_retries": 0, "stream_max_retries": 0}}}
        profile = home / "config.toml"
        profile.write_text("\n".join(toml_lines(config)), encoding="utf-8")
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith("CODEX_") and not key.upper().endswith("_API_KEY")}
        environment["CODEX_HOME"] = str(home)
        argv = [settings["binary"], "app-server", "--stdio"]
        overrides = {"project_doc_max_bytes": 0, "web_search": "disabled", "notify": [],
                     "features.skip_host_skill_discovery": True,
                     "skills.include_instructions": False, "skills.bundled.enabled": False}
        overrides.update({"features." + name: False for name in DISABLED})
        for name, value in overrides.items():
            argv += ["-c", name + "=" + json.dumps(value)]
        events = queue.Queue()
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=scratch, env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
        self.store.write("glm-process-" + request_id, {"pid": process.pid, "model": model, "forced": False,
                                                     "client_binary": settings["binary"]})

        def read(pipe, channel):
            for line in pipe:
                self.store.append_raw("glm-" + request_id + "-" + channel, line)
                if channel == "stdout":
                    try:
                        events.put(json.loads(line))
                    except ValueError:
                        events.put({"invalid_line": True})
            if channel == "stdout":
                events.put({"eof": True})
        readers = [threading.Thread(target=read, args=(pipe, channel), daemon=True)
                   for pipe, channel in [(process.stdout, "stdout"), (process.stderr, "stderr")]]
        for reader in readers:
            reader.start()
        serial = 0
        pending_events = []

        def send(method, params, response=True):
            nonlocal serial
            serial += 1
            request = {"method": method, "params": params}
            if response:
                request["id"] = serial
            process.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
            process.stdin.flush()
            if not response:
                return None
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                event = events.get(timeout=max(.1, deadline - time.monotonic()))
                if event.get("id") == serial:
                    if "error" in event:
                        raise ProtocolError("glm_client_error", "The supported GLM client rejected the operation")
                    return event["result"]
                if event.get("eof") or event.get("invalid_line"):
                    raise ProtocolError("glm_client_closed", "GLM client closed before the operation completed")
                pending_events.append(event)
            raise ProtocolError("glm_client_timeout", "GLM client did not acknowledge the operation", outcome="unknown")

        thread_id = turn_id = None
        try:
            send("initialize", {"clientInfo": {"name": "iikona_personal", "version": "0.1.0"},
                                "capabilities": {"experimentalApi": True}})
            send("initialized", {}, False)
            tools = [{"type": "function", "name": tool["name"], "description": tool["description"],
                      "inputSchema": tool["parameters"]} for tool in native.get("tools", [])]
            started = send("thread/start", {"cwd": str(scratch), "model": model, "modelProvider": "zai-coding",
                "baseInstructions": "\n\n".join(system_instructions) or
                    "You are a model connected to Iikona. Follow the supplied conversation. Only request tools supplied by Iikona.",
                "developerInstructions": "\n\n".join(developer_instructions),
                "ephemeral": True, "approvalPolicy": "never", "sandbox": "read-only",
                "dynamicTools": tools, "selectedCapabilityRoots": [], "allowProviderModelFallback": False})
            thread_id = started["thread"]["id"]
            if started.get("instructionSources") != []:
                raise ProtocolError("glm_ambient_context", "GLM client loaded unexpected instruction files")
            inventory = send("mcpServerStatus/list", {"threadId": thread_id, "limit": 100, "detail": "toolsAndAuthOnly"})
            if inventory.get("data") or inventory.get("nextCursor"):
                raise ProtocolError("glm_ambient_tools", "GLM client loaded unexpected MCP tools")
            items = conversation
            input_items = []
            if items and items[-1].get("role") == "user":
                input_items = [{"type": "text", "text": items.pop()["content"]}]
            if items:
                items = [{"type": "message", "role": item["role"], "content": [{
                    "type": "output_text" if item["role"] == "assistant" else "input_text",
                    "text": item["content"]}]} if "role" in item else item for item in items]
                send("thread/inject_items", {"threadId": thread_id, "items": items})
            params = {"threadId": thread_id, "input": input_items, "effort": settings.get("effort", "high")}
            started_turn = send("turn/start", params)
            turn_id = started_turn["turn"]["id"]
            deadline = time.monotonic() + 300
            message = None
            while time.monotonic() < deadline:
                event = pending_events.pop(0) if pending_events else events.get(timeout=max(.1, deadline - time.monotonic()))
                method = event.get("method")
                params = event.get("params", {})
                if event.get("eof") or event.get("invalid_line"):
                    raise ProtocolError("glm_interrupted", "GLM stream ended before completion", outcome="unknown")
                if method == "item/tool/call":
                    name = params.get("tool")
                    if name not in {tool["name"] for tool in tools}:
                        raise ProtocolError("glm_unoffered_tool", "GLM requested a tool not supplied by 1C", outcome="received")
                    arguments = params.get("arguments")
                    call_id = params.get("callId")
                    if not isinstance(arguments, dict) or not isinstance(call_id, str):
                        raise ProtocolError("glm_invalid_tool", "GLM returned an invalid tool request", outcome="received")
                    message = {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}
                    # 1C owns tool execution and the next model round. Stop this
                    # client's waiting turn without answering or executing its call.
                    send("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                    break
                if "id" in event and method:
                    raise ProtocolError("glm_client_interaction", "GLM requested an unsupported client interaction", outcome="received")
                if method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                    message = {"role": "assistant", "content": params["item"].get("text", "")}
                if method == "turn/completed":
                    if params.get("turn", {}).get("status") != "completed":
                        raise ProtocolError("glm_turn_failed", "GLM turn did not complete", outcome="received")
                    break
            else:
                raise ProtocolError("glm_timeout", "GLM result is unknown; no automatic resend", outcome="unknown")
            if not message or not (message.get("content") or message.get("tool_calls")):
                raise ProtocolError("glm_empty_response", "GLM returned no answer or tool request", outcome="received")
            return {"id": "glm_" + thread_id + "_" + turn_id, "model": model, "object": "chat.completion",
                "choices": [{"index": 0, "message": message,
                             "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                "iikona": {"billing": "subscription", "cost": None, "outcome": "received"}}
        except queue.Empty as error:
            raise ProtocolError("glm_timeout", "GLM client result is unknown; no automatic resend", outcome="unknown") from error
        finally:
            if thread_id and turn_id and process.poll() is None:
                try:
                    send("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                except (OSError, ProtocolError, queue.Empty):
                    pass
            process.stdin.close()
            try:
                code = process.wait(timeout=20)
                for reader in readers:
                    reader.join(timeout=5)
                process.stdout.close()
                process.stderr.close()
                self.store.write("glm-process-exit-" + request_id, {"pid": process.pid, "exit_code": code, "forced": False})
                profile.unlink()
                # Client-owned logs remain protected in its private home.
            except subprocess.TimeoutExpired:
                self.store.write("glm-process-exit-" + request_id, {"pid": process.pid, "still_exiting": True, "forced": False})
