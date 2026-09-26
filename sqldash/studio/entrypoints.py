import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from platformdirs import user_config_dir
from pydantic import Field, model_validator

from sqldash.api.helpers import StrictBody
from sqldash.studio.errors import StudioError as StudioError
from sqldash.studio.errors import StudioNotFound as StudioNotFound
from sqldash.studio.process import AgentProcess

INHERITED_ENVIRONMENT = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "TZ",
        "TERM",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "ZDOTDIR",
    }
)
MAX_ENTRYPOINTS_BYTES = 1024 * 1024
CHECK_TIMEOUT = 10.0
NO_ENTRYPOINTS = "No entrypoints: claude and codex were not found on PATH and none are saved"


class AgentEntrypoint(StrictBody):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[\w .-]+$")
    command: list[str] = Field(min_length=1, max_length=32)
    protocol: Literal["text", "claude"] = "text"
    shell: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    pass_env: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def valid_command(self):
        if any(not arg or "\0" in arg for arg in self.command):
            raise ValueError("command arguments must be nonempty and contain no NUL")
        if "{prompt}" not in self.command:
            raise ValueError("command must contain a separate {prompt} argument")
        if any("{" in arg or "}" in arg for arg in self.command if arg != "{prompt}"):
            raise ValueError("only a standalone {prompt} placeholder is supported")
        if self.shell:
            if (
                "\0" in self.shell
                or Path(self.shell).name not in {"bash", "zsh"}
                or not Path(self.shell).is_absolute()
            ):
                raise ValueError("shell must be an absolute path to bash or zsh")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", self.command[0]):
                raise ValueError("shell entrypoint must be a simple alias/function/command name")
        if any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in [*self.env, *self.pass_env]
        ):
            raise ValueError("invalid environment variable name")
        if any("\0" in value for value in self.env.values()):
            raise ValueError("environment values must contain no NUL")
        return self

    def argv(self, prompt: str, resume_id: str | None = None) -> list[str]:
        if self.protocol == "claude":
            args = [arg for arg in self.command if arg != "{prompt}"]
            args.extend(["--input-format", "stream-json", "--permission-prompt-tool", "stdio"])
        else:
            args = [prompt if arg == "{prompt}" else arg for arg in self.command]
        if resume_id:
            args.append(f"--resume={resume_id}")
        if self.shell:
            # Only the locally configured entrypoint becomes shell syntax; all user text is argv.
            return [self.shell, "-lic", f'{args[0]} "$@"', "sqldash-studio", *args[1:]]
        return args

    def environment(self) -> dict[str, str]:
        missing = set(self.pass_env) - os.environ.keys() - self.env.keys()
        if missing:
            raise StudioError(
                "Missing entrypoint environment variables: " + ", ".join(sorted(missing))
            )
        inherited = {
            key: os.environ[key]
            for key in INHERITED_ENVIRONMENT | set(self.pass_env)
            if key in os.environ
        }
        return {"PATH": os.defpath, **inherited, **self.env, "NO_COLOR": "1"}

    def check(self) -> None:
        if self.shell:
            process = None
            try:
                process = AgentProcess(
                    [
                        self.shell,
                        "-lic",
                        'command -v -- "$1" >/dev/null',
                        "sqldash-studio",
                        self.command[0],
                    ],
                    cwd=None,
                    env=self.environment(),
                )
                process.reader.join(timeout=CHECK_TIMEOUT)
                state = process.read(0)
                if state["running"]:
                    raise StudioError("Could not check this shell entrypoint within 10 seconds")
                if state["error"]:
                    raise StudioError(state["error"])
            except OSError as exc:
                raise StudioError(
                    "Could not check this shell entrypoint within 10 seconds"
                ) from exc
            finally:
                if process is not None and (process.stop() or process.read(0)["error"]):
                    raise StudioError(
                        "Could not stop the entrypoint check. Stop remaining shell "
                        "processes locally."
                    )
            if state["returncode"]:
                raise StudioError("Entrypoint not found in the configured interactive login shell")
        elif not shutil.which(self.command[0], path=self.environment().get("PATH")):
            raise StudioError("Executable not found; use an absolute path or configure PATH")


def entrypoints_path() -> Path:
    return Path(user_config_dir("sqldash")) / "studio.json"


def load_entrypoints() -> dict[str, AgentEntrypoint]:
    path = entrypoints_path()
    if not path.exists():
        return {}
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("configuration must be a regular file")
            data = stream.read(MAX_ENTRYPOINTS_BYTES + 1)
        if len(data) > MAX_ENTRYPOINTS_BYTES:
            raise ValueError("configuration is too large")
        entrypoints = [AgentEntrypoint.model_validate(item) for item in json.loads(data)]
        if len({p.name for p in entrypoints}) != len(entrypoints):
            raise ValueError("duplicate name")
        return {p.name: p for p in entrypoints}
    except (ValueError, TypeError, OSError, RecursionError) as exc:
        raise StudioError(f"Invalid Studio entrypoints file: {path}") from exc


def save_entrypoint(entrypoint: AgentEntrypoint, keep: Iterable[str] = ()) -> None:
    entrypoints = load_entrypoints()
    existing = entrypoints.get(entrypoint.name)
    if existing:
        entrypoint = entrypoint.model_copy(
            update={field: getattr(existing, field) for field in keep}
        )
    entrypoints[entrypoint.name] = entrypoint
    data = json.dumps([p.model_dump() for p in entrypoints.values()], indent=2).encode()
    if len(data) > MAX_ENTRYPOINTS_BYTES:
        raise StudioError("Studio entrypoints configuration exceeds 1 MiB")
    path = entrypoints_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".studio-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def available_entrypoints() -> dict[str, AgentEntrypoint]:
    found = {}
    for name, command in [
        (
            "Claude Code",
            [
                "claude",
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
                "{prompt}",
            ],
        ),
        ("Codex", ["codex", "exec", "--skip-git-repo-check", "--json", "{prompt}"]),
    ]:
        executable = shutil.which(command[0])
        if executable:
            found[name] = AgentEntrypoint(
                name=name,
                command=[executable, *command[1:]],
                protocol="claude" if name == "Claude Code" else "text",
            )
    found.update(load_entrypoints())
    return found


def resolve_entrypoint(name: str) -> AgentEntrypoint:
    available = available_entrypoints()
    entrypoint = available.get(name)
    if entrypoint is not None:
        return entrypoint
    known = ", ".join(repr(other) for other in available)
    raise StudioNotFound(
        f"Unknown entrypoint {name!r}. " + (f"Available: {known}" if known else NO_ENTRYPOINTS)
    )
