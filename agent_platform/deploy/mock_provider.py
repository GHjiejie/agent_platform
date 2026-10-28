"""Local Responses fixture for smoke_test.py; never used by the application."""

import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COUNTS = {"calls": 0, "retry_test": 0}


def input_items(body: dict) -> list[dict]:
    value = body.get("input", [])
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    return [item for item in value if isinstance(item, dict)]


def contains_text(body: dict, text: str) -> bool:
    for item in input_items(body):
        content = item.get("content")
        if content == text or (
            isinstance(content, list)
            and any(
                isinstance(part, dict) and part.get("text") == text for part in content
            )
        ):
            return True
    return False


def response_events(body: dict) -> list[dict]:
    """Return a complete native Responses lifecycle, including tool fragments."""
    response_id = "resp_smoke_" + uuid.uuid4().hex
    call_tool = bool(body.get("tools")) and not any(
        item.get("type") == "function_call_output" for item in input_items(body)
    )
    response = {
        "id": response_id,
        "object": "response",
        "created_at": 1,
        "model": body.get("model", "gpt-4o-mini"),
        "status": "in_progress",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "output": [],
        "parallel_tool_calls": True,
        "store": False,
        "tool_choice": "auto",
        "tools": body.get("tools", []),
    }
    events = [{"type": "response.created", "response": dict(response)}]
    if call_tool:
        item = {
            "id": "fc_" + response_id,
            "type": "function_call",
            "call_id": "smoke-tool",
            "name": "calculator",
            "arguments": "",
            "status": "in_progress",
        }
        events.append(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": dict(item),
            }
        )
        for delta in ('{"expression":', '"2+2"}'):
            events.append(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": item["id"],
                    "output_index": 0,
                    "delta": delta,
                }
            )
        item = {**item, "arguments": '{"expression":"2+2"}', "status": "completed"}
        events.extend(
            [
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": item["id"],
                    "output_index": 0,
                    "arguments": item["arguments"],
                    "name": item["name"],
                },
                {"type": "response.output_item.done", "output_index": 0, "item": item},
            ]
        )
    else:
        part = {
            "type": "output_text",
            "text": "Answer: 4",
            "annotations": [],
            "logprobs": [],
        }
        item = {
            "id": "msg_" + response_id,
            "type": "message",
            "role": "assistant",
            "status": "in_progress",
            "content": [],
        }
        events.extend(
            [
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": dict(item),
                },
                {
                    "type": "response.content_part.added",
                    "item_id": item["id"],
                    "output_index": 0,
                    "content_index": 0,
                    "part": {**part, "text": ""},
                },
                {
                    "type": "response.output_text.delta",
                    "item_id": item["id"],
                    "output_index": 0,
                    "content_index": 0,
                    "delta": part["text"],
                },
                {
                    "type": "response.output_text.done",
                    "item_id": item["id"],
                    "output_index": 0,
                    "content_index": 0,
                    "text": part["text"],
                },
                {
                    "type": "response.content_part.done",
                    "item_id": item["id"],
                    "output_index": 0,
                    "content_index": 0,
                    "part": part,
                },
            ]
        )
        item = {**item, "status": "completed", "content": [part]}
        events.append(
            {"type": "response.output_item.done", "output_index": 0, "item": item}
        )
    response = {**response, "status": "completed", "output": [item]}
    if not contains_text(body, "missing-usage-test"):
        response["usage"] = {
            "input_tokens": 12,
            "output_tokens": 4,
            "total_tokens": 16,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
    events.append({"type": "response.completed", "response": response})
    return [dict(event, sequence_number=index) for index, event in enumerate(events)]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(200 if self.path == "/stats" else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(COUNTS if self.path == "/stats" else {}).encode())

    def do_POST(self) -> None:
        if self.path != "/v1/responses":
            self.send_response(404)
            self.end_headers()
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        COUNTS["calls"] += 1
        if contains_text(body, "retry-test"):
            COUNTS["retry_test"] += 1
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"error":{"message":"simulated failure","type":"internal_error"}}'
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for event in response_events(body):
            self.wfile.write(
                (
                    "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                ).encode()
            )
            self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
