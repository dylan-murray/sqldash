# sqldash Studio

Point at a tile or filter, describe the change, and send the notes to your local
coding agent. Studio runs a headless command, shows its output, and lets you review
or undo its dashboard edits while you keep chatting. It ships in the Python package; there is no
terminal emulator, Node.js runtime, or frontend build step.

## Start

```bash
sqldash serve
```

Open a dashboard and click **AI Studio**. Installed Claude Code and Codex commands
appear automatically when they are on the server's PATH. Click **Annotate dashboard**,
choose the exact element, and write your comment beside it. Use **Add request**
for changes that aren't tied to one element. Add any overall instructions, then click **Send to agent**.
**Included context** shows what accompanies the request and offers full-context
review before launch.

Studio currently supports macOS and Linux. The server must bind to a loopback
address. Studio is enabled by default on loopback hosts; use `sqldash serve --no-studio`
to disable it. Non-loopback serving leaves Studio off. No agent launches until you
click **Send to agent**.

## Style a dashboard with Studio

Start with the [themed example project](../examples/studio/README.md), or use your
own dashboard. Pin a card and describe a concrete change: “Make this revenue card
the focal point, with a violet background and a brighter number.” Add a general
request for the rest: “Give the canvas a subtle grid and keep the other cards dark.”

Ask the agent to put styling in the dashboard's `css: |` block and preserve queries,
metric definitions, and filters. Styles refresh in place as the file changes. Keep
chatting to refine them; **Undo last edit** restores tracked YAML/CSS files to
their state before the latest edit.

The [theme guide](themes.md) explains the canvas scope and selectors. Selectors in
theme CSS cannot reach the topbar or Studio; page tokens at the top of `css:` set the
colours they draw from, and external images and fonts are blocked.
**View changes** tracks the YAML edit; use `git diff` for the full authored CSS.

## Custom entrypoints

Open **Agent settings**, then **Custom entrypoints** to add a name, command/path, and agent type. For a
shell alias such as `claude-custom`, select the shell that defines it. You can
save several entrypoints for the same agent without leaving the dashboard.
Existing names are replaced, including names that match discovered agents. Re-saving
a name from the panel keeps any `pass_env` and `env` settings already in `studio.json`.

The configured bash or zsh runs as an interactive login shell and reads its startup
files. Direct executables don't need a shell. Use absolute paths if the server's
PATH differs from your terminal. For arbitrary headless commands, extra arguments,
or entrypoint-specific environment variables, the CLI remains available:

```bash
sqldash studio add "Custom agent" --shell /bin/zsh -- claude-custom -p '{prompt}'
sqldash studio list
sqldash studio check "Custom agent"
```

`studio list` and `studio check` see the same names as the panel: discovered
Claude Code and Codex plus everything saved in `studio.json`.

Any headless CLI or wrapper works: configure its arguments and include `{prompt}`
as a separate argument. Studio substitutes the request text, including sanitized
dashboard context. Requests over 100 kB are rejected before launch rather than
truncated. Studio captures stdout and stderr, and runs in the selected
project's working directory. An interactive CLI that needs terminal input is not
supported. Configure tool permissions through your agent's own settings; Studio
doesn't add permission-bypass flags. **Check entrypoint** verifies that the command
exists, not that authentication or editing permissions work.

Custom entrypoints live in your platform's user configuration directory under
`sqldash/studio.json`; the Studio panel shows the exact path. They stay outside
committed dashboard YAML. Saving an entrypoint is protected by the same local token and origin checks as
launching. Saving does not execute it; **Send to agent** starts the command.

### Agent environment

Studio passes a small set of environment variables for executable lookup, home and
config directories, temporary files, and locale. It does not automatically forward
warehouse credentials, cloud tokens, SSH agent sockets, proxy credentials, or API
keys from the server. Existing CLI logins remain available through the agent's home
and configuration directories.

If an agent needs additional variables, add their **names** to `pass_env` in its
entry in `studio.json`, for example `"pass_env": ["ANTHROPIC_API_KEY"]`. Studio reads
their current values from the server environment at launch and reports missing
variables. The existing `env` object supplies explicit values and takes precedence;
prefer `pass_env` for secrets so their values do not need to be saved in this file.
Saving the same name again from **Agent settings** keeps both `pass_env` and `env`;
only a save that sends them explicitly, such as an empty list, replaces them.
Do not add warehouse credentials unless you intend to give the agent access.

Aliases and shell functions still load your shell startup files, which can export
additional credentials. Environment filtering is not filesystem isolation: agents
may read credentials from files or use configured integrations within their own
permissions. Studio does not enforce filesystem or network restrictions.

## Keep iterating

The context includes the selected dashboard, tile identifiers, notes, active filter
values, and metric definitions with connection settings omitted. Open **Included context**, click **Capture screenshot**, choose the dashboard tab in your browser, and
preview the captured image before sending. Capture stops after one frame;
**Remove screenshot** excludes it. The browser requires a new capture permission
each time. You can also attach a PNG up to 1 MB. Query results are not automatically attached.
Your selected agent may send context to a remote model and has your local filesystem
permissions.

**Stop agent** terminates the process group. Review partial edits even if the
command fails or is stopped. Output is bounded to 2 MiB; some agents print only
when they finish. The dashboard updates in place as files change, keeping your
notes, conversation, and run state open. Invalid YAML leaves the last rendered
dashboard visible and shows a refresh error.

**View changes** validates configuration without querying your warehouse and
compares the direct YAML/YML/CSS files in the dashboard directory. The comparison
normalizes YAML and omits connection settings. CSS is included in undo, but its
text comparison is unavailable in Studio. Use `git diff` for CSS, exact text and
formatting, and `sqldash lint` for full validation details.

Edits stay on disk automatically, without committing or pushing. Keep chatting to
refine them: the composer stays visible while the agent works, so you can draft
your next message. Claude resumes the same conversation on each send. Other
headless entrypoints receive the latest dashboard context and new requests, but
do not yet resume their agent's conversation history; the conversation header
shows "New context each turn" for them.

**Undo last edit** restores the files changed by the latest turn to their state
before that turn, preserving earlier edits. Sending again starts a new checkpoint
and replaces the previous undo history. After undoing, you can keep chatting;
Claude is told to read the restored files. **View changes** is optional.

Undo covers direct YAML/YML/CSS files in the dashboard directory, excluding
configuration/credential profiles, nested directories, and other paths. Avoid
editing these files elsewhere during a turn or undo: Studio cannot distinguish
concurrent edits from the agent's. Changes detected after review require **View
changes** again before undo; filesystem checks cannot prevent another editor
writing during undo.

Notes and the current session reference survive reloads in the same browser tab.
Restarting the server ends its sessions; Studio recovers missing sessions to your
saved requests. Closing the tab that owned a session used to lock Studio on that
project until restart; sending again now ends the leftover session and starts a
new one. Closing AI Studio confirms stopping the session and keeping edits.
Ending a session releases its review and undo history.

If Studio cannot stop all agent processes, it displays a cleanup error and disables
review and undo. Stop any remaining processes locally before closing AI Studio. Ending the session does not prove that those processes stopped.

Press **Enter** in the message box to send, or **Shift+Enter** for a new line.

**Annotate dashboard** stays on between saved requests; choose **Browse** when
you're done pinning. **Clear all** removes every request and pin but keeps the
conversation, overall instructions, and a request you're still writing; **Undo**
in the cleared notice is available for up to ten seconds, until another request change,
and restores the unfinished edit
from clear time, unless you have started a newer draft.
Undo history lasts until the page reloads. The tour demonstrates on the dashboard using temporary visual
overlays. **Skip tour** dismisses it without changing your requests or draft.

### Live agent output

Discovered Claude Code and Claude entrypoints saved in the browser use streaming
output. Studio shows response text, tool activity, and permission denials while the
agent runs. Existing custom entrypoints keep their configured arguments. To enable
Claude streaming for one, save it again in Agent settings or add
`--output-format stream-json --verbose --include-partial-messages` to its command.
These flags do not grant tool permissions. Claude entrypoints saved in Studio also
use the permission bridge described below.
Other commands continue to show their plain output as it arrives.

The transcript follows new output while you are at the bottom. Scroll up to read
earlier messages; **Latest** returns to live output. Connector notices and
unrecognized structured events are available under **Agent diagnostics**.

Studio groups agent output into request and response messages, with expandable
tool activity. Claude permission requests appear as **Allow once** and **Deny** cards. A denial
already reported by the agent is distinct from a pending approval.

### Approving Claude tool requests

The discovered Claude Code entrypoint forwards permission requests to Studio.
Review the tool and its arguments, then choose **Allow once** or **Deny**. Allow
applies only to that request and does not save an allow rule. Refreshing the page
keeps pending requests; stopping or closing the session invalidates them.

Existing custom Claude entrypoints need to be re-saved in Agent settings to enable
the bridge, or configured with `"protocol": "claude"` in their entrypoint definition.
Keep the usual `-p --output-format stream-json --verbose --include-partial-messages`
arguments and `{prompt}` placeholder; Studio supplies the input/control flags.
Your agent's existing permission rules still apply. Already-allowed tools need not
prompt, and already-denied tools may fail without a prompt. Codex and other plain
commands continue to use their own permission settings; interactive forwarding for
them is not implemented.


To skip individual approvals, enable **Auto-approve all tools** below the
conversation. This approves pending and future forwarded Claude tool requests,
including shell commands and file writes, for this Studio session. It starts off,
can be turned off at any time, and resets when you close the session. Disabling it
doesn't stop tools already approved; use **Stop agent** to stop running work.
The toggle doesn't change your agent's saved permissions or override denied-tool
rules. It is available only for Claude entrypoints with the permission bridge.
This is blanket approval of forwarded requests, not Claude's native `auto` safety
classifier. The permission summary below the agent picker shows which behavior is
selected; it does not claim to inspect the agent's effective sandbox configuration.

Codex currently uses output-only `exec` integration, so Studio cannot relay its
interactive approvals. Recent Codex versions provide `--approve-for-me`, which
routes eligible requests through Codex's own automatic reviewer with a workspace-write
sandbox. This differs from blanket approval and is not enabled by Studio. A custom
entrypoint can opt into it if the installed CLI supports it. Studio never adds
`--dangerously-bypass-approvals-and-sandbox`.

### Codex outside Git repositories

Studio's discovered Codex command and newly saved Codex entrypoints include
`--skip-git-repo-check`, so demo folders and ordinary dashboard directories work
without `git init`. This does not change Codex sandbox or tool permissions. If an
older custom entrypoint reports “Not inside a trusted directory”, re-save it as
Codex in Agent settings or add that flag after `exec` in its command.
The “Reading additional input from stdin” line is informational; Studio supplies
the prompt as an argument and closes stdin for plain headless commands.


Discovered Codex and newly saved Codex commands also use `--json`. Studio shows
agent messages as replies, groups command/file activity, and keeps raw command
output in **Agent diagnostics**. Re-save an older Codex entrypoint to enable this
format. Existing plain-text transcripts retain the format used when launched.
