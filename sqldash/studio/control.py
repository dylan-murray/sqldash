"""Claude's JSONL permission protocol; callers serialize access with the process lock."""

import contextlib
import json
import os
import secrets
import time
import uuid

from sqldash.studio.errors import StudioError

PENDING_LIMIT = 8


class ClaudeControl:
    def __init__(self, prompt):
        self.prompt = prompt
        self.buffer = bytearray()
        self.outgoing = bytearray()
        self.pending = {}
        self.auto_approve = False
        self.conversation_id = None
        self.seen = set()
        self.tasks = set()
        self.result = False
        self.initialized = False
        self.ended = False
        self.started = time.monotonic()
        self.initial_id = secrets.token_hex(16)
        self.queue(
            {
                "type": "control_request",
                "request_id": self.initial_id,
                "request": {"subtype": "initialize", "hooks": None},
            }
        )

    def queue(self, message):
        encoded = (json.dumps(message, ensure_ascii=True) + "\n").encode()
        if len(self.outgoing) + len(encoded) > 1_000_000:
            raise StudioError("Agent permission channel exceeded its buffer limit")
        self.outgoing.extend(encoded)

    def flush(self, pipe):
        if self.ended:
            return
        if not self.initialized and time.monotonic() - self.started > 60:
            raise StudioError("Agent permission connection timed out")
        if self.outgoing:
            try:
                written = os.write(pipe.fileno(), self.outgoing[:65536])
            except BlockingIOError:
                return
            del self.outgoing[:written]
        if self.result and not self.tasks and not self.pending and not self.outgoing:
            pipe.close()
            self.ended = True

    def feed(self, chunk):
        self.buffer.extend(chunk)
        while b"\n" in self.buffer:
            raw, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            if len(raw) > 2_000_000:
                raise StudioError("Agent protocol message is too large")
            try:
                event = json.loads(raw)
            except (ValueError, RecursionError):
                continue
            if isinstance(event, dict):
                self.event(event)
        if len(self.buffer) > 2_000_000:
            raise StudioError("Agent protocol message is too large")

    def response(self, request_id, data):
        self.queue(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": request_id, "response": data},
            }
        )

    def event(self, event):
        kind = event.get("type")
        if kind == "control_response":
            response = event.get("response", {})
            if isinstance(response, dict) and response.get("request_id") == self.initial_id:
                if self.initialized:
                    return
                if response.get("subtype") != "success":
                    raise StudioError("Agent rejected the permission connection")
                self.initialized = True
                self.queue(
                    {
                        "type": "user",
                        "session_id": "",
                        "message": {"role": "user", "content": self.prompt},
                        "parent_tool_use_id": None,
                    }
                )
                self.prompt = ""
        elif kind == "control_request":
            request_id, request = event.get("request_id"), event.get("request")
            if not isinstance(request_id, str) or not 0 < len(request_id) <= 200:
                raise StudioError("Agent sent an invalid permission request")
            if request_id in self.seen or len(self.seen) >= 1000:
                raise StudioError("Agent repeated or exceeded permission requests")
            self.seen.add(request_id)
            if not isinstance(request, dict) or request.get("subtype") != "can_use_tool":
                self.queue(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "error",
                            "request_id": request_id,
                            "error": "Studio does not support this control request",
                        },
                    }
                )
                return
            tool, inputs = request.get("tool_name"), request.get("input")
            if (
                not isinstance(tool, str)
                or not 0 < len(tool) <= 200
                or not isinstance(inputs, dict)
                or len(json.dumps(inputs)) > 64000
            ):
                self.response(
                    request_id,
                    {
                        "behavior": "deny",
                        "message": "Studio cannot display this permission request safely",
                    },
                )
                return
            if self.auto_approve:
                self.response(request_id, {"behavior": "allow", "updatedInput": inputs})
                return
            if len(self.pending) >= PENDING_LIMIT:
                self.response(
                    request_id,
                    {
                        "behavior": "deny",
                        "message": (
                            f"Studio denied this request because {PENDING_LIMIT} permission"
                            " requests are already waiting for a decision"
                        ),
                    },
                )
                return
            token = secrets.token_hex(24)
            self.pending[token] = {
                "id": token,
                "tool": tool,
                "input": inputs,
                "request_id": request_id,
            }
        elif kind == "control_cancel_request":
            request_id = event.get("request_id")
            self.pending = {
                key: value
                for key, value in self.pending.items()
                if value["request_id"] != request_id
            }
        elif kind == "system":
            if event.get("subtype") == "init" and isinstance(event.get("session_id"), str):
                with contextlib.suppress(ValueError):
                    self.conversation_id = str(uuid.UUID(event["session_id"]))
            task = event.get("task_id")
            if isinstance(task, str) and len(task) <= 200:
                if event.get("subtype") == "task_started":
                    if len(self.tasks) >= 1000:
                        raise StudioError("Agent exceeded the background task limit")
                    self.tasks.add(task)
                elif event.get("subtype") == "task_notification":
                    self.tasks.discard(task)
        elif kind == "result":
            self.result = True

    def decide(self, token, decision):
        request = self.pending.get(token)
        if request is None or self.ended:
            raise StudioError("This permission request is no longer pending")
        response = (
            {"behavior": "allow", "updatedInput": request["input"]}
            if decision == "allow"
            else {"behavior": "deny", "message": "The user denied this tool request"}
        )
        self.response(request["request_id"], response)
        del self.pending[token]

    def set_auto_approve(self, enabled):
        if enabled:
            for token in list(self.pending):
                self.decide(token, "allow")
        self.auto_approve = enabled

    def requests(self):
        return [
            {key: value for key, value in request.items() if key != "request_id"}
            for request in self.pending.values()
        ]
