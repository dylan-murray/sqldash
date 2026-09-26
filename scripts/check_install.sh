#!/usr/bin/env bash
# Answers one question the test suite cannot: does `pip install sqldash` work?
#
# The test job installs from uv.lock and imports from the source tree. This
# builds the wheel, installs it into a clean venv with FRESH dependency
# resolution (no lockfile), and drives the real CLI — so it catches both a
# dependency's breaking major release and anything missing from the wheel.
set -euo pipefail

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "==> building wheel"
rm -rf dist
uv build --wheel >/dev/null

echo "==> sdist ships the package, not the dev tree"
uv build --sdist >/dev/null
python3 - dist/*.tar.gz <<'PYEOF'
import sys, tarfile

path = sys.argv[1]
with tarfile.open(path) as sdist:
    members = [m for m in sdist.getmembers() if m.isfile()]
top = {m.name.split("/", 2)[1] for m in members if m.name.count("/") >= 1}
size = sum(m.size for m in members)
dev_only = {".github", "assets", "scripts", "tests"} & top
assert {"sqldash", "examples"} <= top, f"sdist is missing the package or examples: {sorted(top)}"
assert not dev_only, f"sdist ships dev-only paths: {sorted(dev_only)}"
assert size < 10_000_000, f"sdist unpacks to {size:,} bytes; check the include list"
print(f"   {len(members)} files, {size:,} bytes unpacked: {sorted(top)}")
PYEOF

echo "==> installing into a clean venv (resolving dependencies fresh)"
uv venv -q "$work/venv"
VIRTUAL_ENV="$work/venv" uv pip install -q dist/*.whl
sqldash="$work/venv/bin/sqldash"
VIRTUAL_ENV="$work/venv" uv pip list 2>/dev/null | grep -E '^(mcp|fastapi|pydantic|duckdb|typer) ' || true

echo "==> version"
"$sqldash" --version

echo "==> init + lint"
mkdir -p "$work/project"
(cd "$work/project" && "$sqldash" init --demo . >/dev/null && "$sqldash" lint .)

echo "==> dashboard list --json"
(cd "$work/project" && "$sqldash" dashboard list . --json | python3 -c "
import json, sys
payload = json.load(sys.stdin)
assert payload['dashboards'], 'no dashboards listed'
print('   listed:', [d['name'] for d in payload['dashboards']])
")

echo "==> metric query (real rows through the semantic layer)"
(cd "$work/project" && "$sqldash" metric query revenue --format json | python3 -c "
import json, sys
rows = json.load(sys.stdin)
assert rows and rows[0], f'no rows: {rows}'
print('   revenue =', list(rows[0].values())[0])
")

echo "==> mcp stdio handshake (catches breaking changes in the mcp package)"
# Driven from python rather than `printf | sqldash mcp | python`. That pipeline
# closed stdin the instant the three messages were written, so the server could
# reach EOF and shut down before dispatching tools/list — an intermittent "no
# tools" failure that says nothing about the build. It also sent stderr to
# /dev/null, so when it did fail there was nothing to look at.
(cd "$work/project" && python3 - "$sqldash" <<'PYEOF'
import json, subprocess, sys, threading

REPLY_DEADLINE_SECONDS = 60

server = subprocess.Popen(
    [sys.argv[1], "mcp", "."],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)

# Drained on its own thread, from the start. Reading it only on the failure path
# would be an unbounded blocking read in exactly the situation this check exists
# for — and a child of the child can hold the pipe open after terminate().
# Nothing ever joins this thread, so it cannot hang the script.
stderr_lines: list[str] = []
threading.Thread(
    target=lambda: stderr_lines.extend(server.stderr), daemon=True
).start()

requests = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "1"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
]
server.stdin.write("".join(json.dumps(r) + "\n" for r in requests))
server.stdin.flush()          # stdin stays OPEN so the server keeps serving

outcome: dict[str, object] = {}
answered = threading.Event()


def read_tools_list_reply():
    try:
        for line in server.stdout:
            line = line.strip()
            if not line.startswith("{") or json.loads(line).get("id") != 2:
                continue
            message = json.loads(line)
            if "error" in message:
                outcome["problem"] = f"tools/list returned an error: {message['error']}"
            else:
                try:
                    outcome["tools"] = sorted(t["name"] for t in message["result"]["tools"])
                except (KeyError, TypeError) as exc:
                    outcome["problem"] = f"unexpected tools/list shape ({exc}): {line[:400]}"
            return
        outcome["problem"] = "the server closed its output without answering tools/list"
    except Exception as exc:  # noqa: BLE001 - a diagnostic, never a control path
        outcome["problem"] = f"could not read the server's output: {exc!r}"
    finally:
        answered.set()


threading.Thread(target=read_tools_list_reply, daemon=True).start()
# Waiting on the event, not joining the reader: this returns the moment a reply
# is handled, so a protocol error reports at once instead of after the deadline.
answered.wait(REPLY_DEADLINE_SECONDS)

server.stdin.close()
server.terminate()

tools = outcome.get("tools")
if not tools:
    problem = outcome.get(
        "problem", f"no tools/list reply within {REPLY_DEADLINE_SECONDS}s"
    )
    sys.stderr.write(f"mcp stdio handshake failed: {problem}\n")
    sys.stderr.write("server stderr:\n")
    sys.stderr.write("".join(stderr_lines) or "  (empty)\n")
    raise SystemExit(1)
assert "query_metric" in tools, f"missing query_metric: {tools}"
print("   tools:", " ".join(tools))
PYEOF
)

echo "==> serve + fetch"
cd "$work/project"
nohup "$sqldash" serve . --port 8799 --no-browser >"$work/serve.log" 2>&1 &
server_pid=$!
disown "$server_pid" 2>/dev/null || true
trap 'kill "$server_pid" 2>/dev/null || true; rm -rf "$work"' EXIT
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null http://127.0.0.1:8799/ 2>/dev/null; then break; fi
  sleep 1
done
for path in / /d/demo /static/js/runner.js /static/vendor/echarts.min.js; do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8799$path")"
  echo "   $code $path"
  [ "$code" = "200" ] || { echo "FAILED: $path returned $code"; cat "$work/serve.log"; exit 1; }
done

echo "==> OK: the published artifact works from a clean install"
