import json
import os
import subprocess
import sys

import pytest
from pydantic import ValidationError

from sqldash.studio.entrypoints import AgentEntrypoint, StudioError


def test_agent_child_does_not_inherit_server_credentials(monkeypatch, tmp_path):
    excluded = {
        "SNOWFLAKE_PASSWORD",
        "AWS_SECRET_ACCESS_KEY",
        "DATABASE_URL",
        "INNOCENT_NAME",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "SSH_AUTH_SOCK",
        "HTTPS_PROXY",
        "PYTHONPATH",
        "NODE_OPTIONS",
        "BASH_ENV",
        "ENV",
        "LD_PRELOAD",
    }
    for name in excluded:
        monkeypatch.setenv(name, "server-only-secret")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    agent = AgentEntrypoint(name="Test", command=[sys.executable, "{prompt}"])
    result = subprocess.run(
        [sys.executable, "-c", "import os,json; print(json.dumps(dict(os.environ)))"],
        env=agent.environment(),
        capture_output=True,
        text=True,
        check=True,
    )
    child = json.loads(result.stdout)
    assert excluded.isdisjoint(child)
    assert "server-only-secret" not in result.stdout
    assert child["CODEX_HOME"] == str(tmp_path / "codex")
    assert child["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude")
    assert child["HOME"] == os.environ["HOME"]
    assert child["PATH"] == os.environ["PATH"]


def test_environment_opt_in_uses_current_values_without_persisting_them(monkeypatch):
    monkeypatch.setenv("AGENT_AUTH", "first-secret")
    agent = AgentEntrypoint(
        name="Test",
        command=[sys.executable, "{prompt}"],
        pass_env=["AGENT_AUTH"],
        env={"AGENT_MODE": "custom", "NO_COLOR": "0"},
    )
    assert agent.environment()["AGENT_AUTH"] == "first-secret"
    monkeypatch.setenv("AGENT_AUTH", "rotated-secret")
    assert agent.environment()["AGENT_AUTH"] == "rotated-secret"
    assert "rotated-secret" not in agent.model_dump_json()
    assert agent.environment()["AGENT_MODE"] == "custom"
    assert agent.environment()["NO_COLOR"] == "1"
    monkeypatch.delenv("AGENT_AUTH")
    with pytest.raises(StudioError, match="Missing entrypoint environment variables: AGENT_AUTH"):
        agent.environment()
    agent.env["AGENT_AUTH"] = "explicit-secret"
    assert agent.environment()["AGENT_AUTH"] == "explicit-secret"


def test_environment_has_default_search_path_when_server_path_absent(monkeypatch):
    monkeypatch.delenv("PATH")
    agent = AgentEntrypoint(name="Test", command=[sys.executable, "{prompt}"])
    assert agent.environment()["PATH"] == os.defpath
    agent.check()


@pytest.mark.parametrize(
    "settings",
    [
        {"pass_env": ["AWS_*"]},
        {"pass_env": ["BAD=NAME"]},
        {"pass_env": ["BAD\x00NAME"]},
        {"env": {"OK": "bad\x00value"}},
    ],
)
def test_invalid_environment_configuration_is_rejected(settings):
    with pytest.raises(ValidationError):
        AgentEntrypoint(name="Test", command=[sys.executable, "{prompt}"], **settings)
