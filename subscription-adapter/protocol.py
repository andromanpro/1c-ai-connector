"""Translate Iikona's model messages without adding agent instructions or tools."""
import json
import re


class ProtocolError(Exception):
    def __init__(self, code, message, *, response_id="", outcome="not_sent"):
        super().__init__(message)
        self.code = code
        self.response_id = response_id
        self.outcome = outcome


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(
        isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        for part in content
    ):
        return "\n".join(part["text"] for part in content)
    raise ProtocolError("unsupported_content", "This connection currently accepts text content only")


def chat_to_responses(body):
    if not isinstance(body, dict) or not isinstance(body.get("model"), str) or not body["model"].strip():
        raise ProtocolError("invalid_request", "A model is required")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ProtocolError("invalid_request", "A nonempty messages array is required")
    if body.get("stream"):
        raise ProtocolError("unsupported_stream", "The 1C-facing response is buffered")
    result = {"model": body["model"], "input": [], "store": False, "stream": True,
              "include": ["reasoning.encrypted_content"]}
    calls = set()
    for message in messages:
        if not isinstance(message, dict):
            raise ProtocolError("invalid_message", "Each message must be an object")
        role = message.get("role")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in calls:
                raise ProtocolError("invalid_tool_result", "A tool result has no preceding call")
            calls.remove(call_id)
            result["input"].append({"type": "function_call_output", "call_id": call_id,
                                    "output": text_content(message.get("content"))})
            continue
        if role not in ("system", "developer", "user", "assistant"):
            raise ProtocolError("invalid_role", "Unsupported message role")
        content = message.get("content")
        if content is not None:
            text = text_content(content)
            if text:
                result["input"].append({"role": role, "content": text})
        tool_calls = message.get("tool_calls", [])
        if not isinstance(tool_calls, list) or (tool_calls and role != "assistant"):
            raise ProtocolError("invalid_tool_calls", "Only assistant messages may request tools")
        for call in tool_calls:
            if not isinstance(call, dict) or call.get("type") != "function":
                raise ProtocolError("invalid_tool_call", "Expected a function call")
            function = call.get("function", {})
            call_id = call.get("id")
            if not isinstance(call_id, str) or not call_id or call_id in calls:
                raise ProtocolError("invalid_tool_call", "Function call identifiers must be unique")
            name, arguments = function.get("name"), function.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, str):
                raise ProtocolError("invalid_tool_call", "Function name and arguments are required")
            try:
                parsed = json.loads(arguments)
            except ValueError as error:
                raise ProtocolError("invalid_tool_arguments", "Function arguments must be JSON") from error
            if not isinstance(parsed, dict):
                raise ProtocolError("invalid_tool_arguments", "Function arguments must be an object")
            calls.add(call_id)
            result["input"].append({"type": "function_call", "call_id": call_id,
                                    "name": name, "arguments": arguments})
    if calls:
        raise ProtocolError("missing_tool_result", "Every historical tool call needs its result")
    tools = body.get("tools", [])
    if not isinstance(tools, list):
        raise ProtocolError("invalid_tools", "Tools must be an array")
    names = set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ProtocolError("unsupported_tool", "Only tools supplied by 1C are accepted")
        function = tool.get("function", {})
        name = function.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or name in names:
            raise ProtocolError("invalid_tool", "Tool names must be valid and unique")
        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            raise ProtocolError("invalid_tool", "A tool input schema is required")
        names.add(name)
        result.setdefault("tools", []).append({"type": "function", "name": name,
            "description": function.get("description", ""), "parameters": parameters})
    if tools:
        choice = body.get("tool_choice", "auto")
        if choice not in ("auto", "none", "required"):
            raise ProtocolError("unsupported_tool_choice", "Unsupported tool selection mode")
        result["tool_choice"] = choice
        result["parallel_tool_calls"] = False
    maximum = body.get("max_tokens")
    if maximum is not None:
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
            raise ProtocolError("invalid_token_limit", "Token limit must be a positive integer")
        result["max_output_tokens"] = maximum
    if "reasoning_effort" in body:
        result["reasoning"] = {"effort": body["reasoning_effort"]}
    return result


def read_sse(lines, on_event=None):
    """Require a terminal event; never promote a partial answer to success."""
    data = []
    response_id = ""
    terminal = None
    completed_items = {}
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
            continue
        if line != "" or not data:
            continue
        raw = "\n".join(data)
        data = []
        if raw == "[DONE]":
            break
        try:
            event = json.loads(raw)
        except ValueError as error:
            raise ProtocolError("invalid_stream", "Provider returned an invalid event",
                                response_id=response_id, outcome="unknown") from error
        response = event.get("response", {})
        response_id = response.get("id", response_id)
        if on_event:
            on_event(event)
        kind = event.get("type")
        if kind == "response.output_item.done":
            index = event.get("output_index")
            item = event.get("item")
            if not isinstance(index, int) or index < 0 or not isinstance(item, dict):
                raise ProtocolError("invalid_output_item", "Provider returned an invalid completed item",
                                    response_id=response_id, outcome="received")
            if index in completed_items and completed_items[index] != item:
                raise ProtocolError("conflicting_output_item", "Provider returned conflicting completed items",
                                    response_id=response_id, outcome="received")
            completed_items[index] = item
        if kind in ("response.failed", "response.incomplete", "error"):
            error = response.get("error") or event.get("error") or event
            code = error.get("code") or kind
            raise ProtocolError(code, "Provider did not complete the response",
                                response_id=response_id, outcome="received")
        if kind == "response.completed":
            if response.get("status") != "completed":
                raise ProtocolError("invalid_completion", "Completed event has an invalid status",
                                    response_id=response_id, outcome="unknown")
            terminal = response
            # SIWC can leave response.output empty and deliver the authoritative
            # items in output_item.done. Deltas alone are never sufficient.
            if not terminal.get("output") and completed_items:
                if sorted(completed_items) != list(range(len(completed_items))):
                    raise ProtocolError("missing_output_item", "A completed output item is missing",
                                        response_id=response_id, outcome="received")
                terminal = {**terminal, "output": [completed_items[index] for index in sorted(completed_items)]}
            break
    if terminal is None:
        raise ProtocolError("incomplete_stream", "Stream ended before response.completed",
                            response_id=response_id, outcome="unknown")
    return terminal


def responses_to_chat(response, allowed_tools):
    content, calls = [], []
    for item in response.get("output", []):
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    content.append(part["text"])
                elif part.get("type") == "refusal":
                    raise ProtocolError("refusal", "Model declined the request", outcome="received")
        elif kind == "function_call":
            if item.get("name") not in allowed_tools:
                raise ProtocolError("unexpected_tool", "Model requested a tool not supplied by 1C",
                                    response_id=response.get("id", ""), outcome="received")
            calls.append({"id": item["call_id"], "type": "function",
                          "function": {"name": item["name"], "arguments": item["arguments"]}})
        elif kind != "reasoning":
            raise ProtocolError("unexpected_output", "Provider returned an unsupported output item",
                                response_id=response.get("id", ""), outcome="received")
    if not content and not calls:
        raise ProtocolError("empty_completion", "Provider returned no answer or tool calls",
                            response_id=response.get("id", ""), outcome="received")
    message = {"role": "assistant", "content": "\n".join(content) or None}
    if calls:
        message["tool_calls"] = calls
    result = {"id": response.get("id", ""), "object": "chat.completion",
        "model": response.get("model", ""), "created": response.get("created_at", 0),
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}],
        "iikona": {"billing": "subscription", "cost": None, "outcome": "received"}}
    usage = response.get("usage")
    if isinstance(usage, dict):
        result["usage"] = {"prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0), "total_tokens": usage.get("total_tokens", 0),
            "prompt_tokens_details": usage.get("input_tokens_details", {})}
    return result
