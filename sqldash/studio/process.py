"""Bounded output from a headless coding-agent subprocess."""

import base64
import contextlib
import os
import select
import selectors
import signal
import subprocess
import sys
import threading

from sqldash.studio.control import ClaudeControl
from sqldash.studio.errors import StudioError

LIMIT = 2 * 1024 * 1024
STOP_TIMEOUT = 2.0


class _ProcessExit:
    def __init__(self, pid):
        self.pid = pid
        self.waitid = getattr(os, "waitid", None)
        self.queue = None
        self.exited = False
        if self.waitid is None:
            self.queue = select.kqueue()
            event = select.kevent(
                pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            try:
                self.queue.control([event], 0, 0)
            except ProcessLookupError:
                self.exited = True
            except OSError:
                self.queue.close()
                raise

    def wait(self, block=False):
        if self.waitid is not None:
            flags = os.WEXITED | os.WNOWAIT | (0 if block else os.WNOHANG)
            return self.waitid(os.P_PID, self.pid, flags) is not None
        if not self.exited:
            self.exited = bool(self.queue.control(None, 1, None if block else 0))
        return self.exited

    def close(self):
        if self.queue is not None:
            self.queue.close()


class AgentProcess:
    def __init__(self, argv, cwd, env, prompt=None, auto_approve=False):
        self.control = ClaudeControl(prompt) if prompt is not None else None
        if self.control:
            self.control.set_auto_approve(auto_approve)
        self.lock = threading.Lock()
        self.output = bytearray()
        self.offset = 0
        self.closed = False
        self.cancelled = False
        self.error = None
        self.process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if self.control else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if self.control:
            os.set_blocking(self.process.stdin.fileno(), False)
        try:
            self.exit_watch = _ProcessExit(self.process.pid)
        except OSError:
            with contextlib.suppress(ProcessLookupError):
                self._signal(signal.SIGKILL)
            self.process.wait()
            self.process.stdout.close()
            if self.process.stdin:
                self.process.stdin.close()
            raise
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                while True:
                    with self.lock:
                        if self.control and not self.cancelled and not self.error:
                            try:
                                self.control.flush(self.process.stdin)
                            except (OSError, ValueError, StudioError):
                                self.error = "Agent permission connection failed. Stop and retry."
                                self._try_signal(signal.SIGKILL)
                    ready = selector.select(timeout=0.2)
                    if not ready:
                        if self.exit_watch.wait():
                            break
                        continue
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    with self.lock:
                        self._append(chunk)
                    if self.exit_watch.wait():
                        break
        finally:
            self.exit_watch.wait(block=True)
            # Keep the unreaped leader's PID reserved through every group signal.
            with self.lock:
                self._try_signal(signal.SIGKILL)
                os.set_blocking(self.process.stdout.fileno(), False)
                remaining = LIMIT
                while remaining:
                    try:
                        chunk = os.read(self.process.stdout.fileno(), min(65536, remaining))
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    self._append(chunk)
                    remaining -= len(chunk)
                self.process.wait()
                self.closed = True
            self.process.stdout.close()
            if self.process.stdin:
                self.process.stdin.close()
            self.exit_watch.close()

    def _try_signal(self, sig):
        try:
            self._signal(sig)
        except ProcessLookupError:
            pass
        except (OSError, subprocess.SubprocessError):
            self.error = (
                "Could not stop all agent processes. Review and undo are disabled. "
                "Stop any remaining processes locally, then end this session."
            )

    def _signal(self, sig):
        try:
            os.killpg(self.process.pid, sig)
        except PermissionError:
            if sys.platform != "darwin":
                raise
            # Darwin returns EPERM for a group containing only unreaped zombies.
            group = subprocess.run(
                ["/bin/ps", "-o", "stat=", "-g", str(self.process.pid)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            states = group.stdout.split()
            if (
                group.returncode not in {0, 1}
                or not states
                or any(not s.startswith("Z") for s in states)
            ):
                raise

    def _append(self, chunk):
        if self.control and not self.cancelled and not self.error:
            try:
                self.control.feed(chunk)
            except (StudioError, ValueError, RecursionError):
                self.error = "Agent permission connection failed. Stop and retry."
                self._try_signal(signal.SIGKILL)
        self.output.extend(chunk)
        if len(self.output) > LIMIT:
            excess = len(self.output) - LIMIT
            del self.output[:excess]
            self.offset += excess

    def read(self, offset: int):
        with self.lock:
            start = max(0, offset - self.offset)
            return {
                "data": base64.b64encode(self.output[start:]).decode(),
                "offset": self.offset + len(self.output),
                "truncated": offset < self.offset,
                "running": not self.closed,
                "returncode": self.process.returncode,
                "cancelled": self.cancelled,
                "error": self.error,
                "conversation_id": self.control.conversation_id if self.control else None,
                "permissions": self.control.requests()
                if self.control and not (self.closed or self.cancelled or self.error)
                else [],
            }

    def set_auto_approve(self, enabled):
        with self.lock:
            if self.closed or self.cancelled or self.error or self.control is None:
                raise StudioError("Agent is not accepting permission changes")
            self.control.set_auto_approve(enabled)

    def permission(self, token, decision):
        with self.lock:
            if self.closed or self.cancelled or self.error or self.control is None:
                raise StudioError("Agent is not accepting permission responses")
            self.control.decide(token, decision)

    def stop(self) -> bool:
        with self.lock:
            if self.closed:
                return False
            self.cancelled = True
            self._try_signal(signal.SIGTERM)
        self.reader.join(timeout=STOP_TIMEOUT)
        with self.lock:
            if not self.closed:
                self._try_signal(signal.SIGKILL)
        self.reader.join(timeout=STOP_TIMEOUT)
        with self.lock:
            return not self.closed
