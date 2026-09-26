import json
import os
import sys
import time

import pytest

from sqldash.studio.control import PENDING_LIMIT, ClaudeControl
from sqldash.studio.entrypoints import StudioError
from sqldash.studio.process import AgentProcess


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_permission_round_trip_to_real_subprocess(tmp_path, decision):
    script = """
import json,sys
from pathlib import Path
init=json.loads(sys.stdin.readline())
print(json.dumps({'type':'control_response','response':{
 'subtype':'success','request_id':init['request_id'],'response':{}}}),flush=True)
message=json.loads(sys.stdin.readline())
assert message['message']['content']=='Test prompt'
print(json.dumps({'type':'control_request','request_id':'tool-1','request':{
 'subtype':'can_use_tool','tool_name':'Write','input':{'file_path':'result.txt','content':'approved'}}}),flush=True)
reply=json.loads(sys.stdin.readline())['response']
assert reply['request_id']=='tool-1'
answer=reply['response']
if answer['behavior']=='allow':
 assert answer['updatedInput']=={'file_path':'result.txt','content':'approved'}
 Path('result.txt').write_text(answer['updatedInput']['content'])
print(json.dumps({'type':'result','subtype':'success'}),flush=True)
assert sys.stdin.read()==''
"""
    process = AgentProcess(
        [sys.executable, "-c", script], tmp_path, os.environ.copy(), prompt="Test prompt"
    )
    try:
        deadline = time.monotonic() + 10
        while not (requests := process.read(0)["permissions"]):
            assert time.monotonic() < deadline, process.read(0)
            time.sleep(0.02)
        assert not (tmp_path / "result.txt").exists()
        token = requests[0]["id"]
        assert requests[0]["tool"] == "Write"
        process.permission(token, decision)
        with pytest.raises(StudioError):
            process.permission(token, "allow")
        while process.read(0)["running"]:
            assert time.monotonic() < deadline, process.read(0)
            time.sleep(0.02)
        assert process.read(0)["returncode"] == 0
        assert (tmp_path / "result.txt").exists() == (decision == "allow")
        assert process.read(0)["permissions"] == []
    finally:
        process.stop()


def test_control_cancellation_bounds_and_no_permission_escalation():
    control = ClaudeControl("prompt")
    event = {
        "type": "control_request",
        "request_id": "one",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": "Bash",
            "input": {"command": "echo hello"},
            "permission_suggestions": [{"type": "setMode", "mode": "bypassPermissions"}],
        },
    }
    for byte in json.dumps(event).encode() + b"\n":
        control.feed(bytes([byte]))
    request = control.requests()[0]
    control.decide(request["id"], "allow")
    response = json.loads(control.outgoing.splitlines()[-1])["response"]["response"]
    assert response == {"behavior": "allow", "updatedInput": {"command": "echo hello"}}
    with pytest.raises(StudioError):
        control.event(event)
    event["request_id"] = "two"
    control.event(event)
    token = control.requests()[0]["id"]
    control.event({"type": "control_cancel_request", "request_id": "two"})
    with pytest.raises(StudioError):
        control.decide(token, "allow")
    event["request_id"] = "three"
    event["request"]["input"] = {"content": "x" * 64001}
    control.event(event)
    assert control.requests() == []
    assert (
        json.loads(control.outgoing.splitlines()[-1])["response"]["response"]["behavior"] == "deny"
    )


def test_pending_permission_limit_denies_with_reason():
    control = ClaudeControl("prompt")
    for index in range(PENDING_LIMIT + 1):
        control.event(
            {
                "type": "control_request",
                "request_id": f"request-{index}",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Bash",
                    "input": {"command": f"echo {index}"},
                },
            }
        )
    assert len(control.requests()) == PENDING_LIMIT
    response = json.loads(control.outgoing.splitlines()[-1])["response"]["response"]
    assert response["behavior"] == "deny"
    assert f"{PENDING_LIMIT} permission requests are already waiting" in response["message"]
    assert "display" not in response["message"]


def test_stopping_pending_permission_cannot_approve(tmp_path):
    script = """
import json,time
print(json.dumps({'type':'control_request','request_id':'one','request':{
 'subtype':'can_use_tool','tool_name':'Write','input':{}}}),flush=True)
time.sleep(60)
"""
    process = AgentProcess([sys.executable, "-c", script], tmp_path, os.environ.copy(), prompt="x")
    try:
        deadline = time.monotonic() + 10
        while not (requests := process.read(0)["permissions"]):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        process.stop()
        assert process.read(0)["permissions"] == []
        with pytest.raises(StudioError):
            process.permission(requests[0]["id"], "allow")
    finally:
        process.stop()


def test_auto_approve_is_explicit_reversible_and_session_local():
    control = ClaudeControl("prompt")

    def request(identifier, tool):
        control.event(
            {
                "type": "control_request",
                "request_id": identifier,
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": tool,
                    "input": {"value": identifier},
                },
            }
        )

    request("first", "Read")
    assert len(control.requests()) == 1
    control.set_auto_approve(True)
    assert control.requests() == []
    request("second", "Bash")
    request("third", "Write")
    assert control.requests() == []
    responses = [
        json.loads(line)["response"]["response"] for line in control.outgoing.splitlines()[1:]
    ]
    assert [value["behavior"] for value in responses] == ["allow", "allow", "allow"]
    assert responses[-1]["updatedInput"] == {"value": "third"}
    control.set_auto_approve(False)
    request("fourth", "Write")
    assert len(control.requests()) == 1
    assert not ClaudeControl("new session").auto_approve
