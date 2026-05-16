"""Cross-process session control for the plsearch MCP server.

Guarantees at most one server holds a given profile dir at any time. If a
prior server was killed by an uncatchable signal (Windows TerminateProcess
from MCP Inspector restart, Task Manager kill, OOM, Node-proxy crash), its
Python process and the Chrome it spawned are still alive — holding the
profile via an OS-level file lock. A fresh server would then hang forever
trying to launch Chrome with the same ``user_data_dir``.

``SessionRegistry`` solves this by writing the current server's PID into the
profile dir at startup. On the next start, the new server kills the prior
PID's entire process tree (Python + Chrome + everything Chrome forked)
before claiming the profile.

Platform-neutral by design: process existence checks and tree termination
work on Windows, Linux, and macOS without third-party deps.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _process_alive(pid: int) -> bool:
    """Return True if ``pid`` refers to a running process."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user — alive enough for us.
        return True


def _kill_process_tree(pid: int) -> None:
    """Force-kill ``pid`` and all its descendants. Best-effort and quiet.

    Windows: ``taskkill /F /T`` walks the job-object tree.
    Unix: ``pgrep -P`` recursively to enumerate descendants, then SIGKILL.
    """
    if not _process_alive(pid):
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                check=False,
                capture_output=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("taskkill failed for pid=%d: %s", pid, exc)
        return

    pids = [pid, *_descendants_unix(pid)]
    # Kill children before parents so reparenting to init can't outrun us.
    for p in reversed(pids):
        try:
            os.kill(p, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _descendants_unix(pid: int) -> list[int]:
    """Walk ``pgrep -P`` recursively to collect all descendant PIDs."""
    found: list[int] = []
    frontier = [pid]
    while frontier:
        nxt: list[int] = []
        for parent in frontier:
            try:
                out = subprocess.run(
                    ["pgrep", "-P", str(parent)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                continue
            for line in out.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    child = int(line)
                    if child not in found:
                        found.append(child)
                        nxt.append(child)
        frontier = nxt
    return found


def _wait_for_exit(pid: int, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Block until ``pid`` is gone or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(interval)
    return not _process_alive(pid)


DEFAULT_PRIOR_GRACE_TIMEOUT = 8.0


class SessionRegistry:
    """File-backed mutex over a Chrome user-data-dir.

    Stored at ``<profile_dir>/.plsearch_session.json``. The file is the
    authoritative record of "who currently owns this profile"; absence means
    no current owner. The format is intentionally human-readable so a stuck
    profile can be unstuck with a text editor in a pinch.
    """

    FILENAME = ".plsearch_session.json"

    def __init__(
        self,
        profile_dir: str,
        *,
        grace_timeout: float = DEFAULT_PRIOR_GRACE_TIMEOUT,
    ) -> None:
        self._profile_dir = Path(profile_dir)
        self._path = self._profile_dir / self.FILENAME
        self._grace_timeout = grace_timeout

    @property
    def path(self) -> Path:
        return self._path

    def claim(self) -> None:
        """Take ownership of the profile, killing any prior owner first.

        Healthy reconnect (Claude Code /mcp, Inspector restart) spawns the
        new subprocess in parallel with telling the old one to shut down.
        Killing the prior immediately turns its clean exit into an abnormal
        one, which parents report as "MCP server failed". So we wait
        ``grace_timeout`` seconds for the prior to exit on its own first;
        only if it's genuinely stuck (Inspector-crash scenario) do we
        terminate it.
        """
        prior = self._read()
        if prior is not None:
            prior_pid = prior.get("pid")
            if (
                isinstance(prior_pid, int)
                and prior_pid != os.getpid()
                and _process_alive(prior_pid)
            ):
                if _wait_for_exit(prior_pid, timeout=self._grace_timeout):
                    logger.info(
                        "Prior server pid=%d exited cleanly within grace",
                        prior_pid,
                    )
                else:
                    logger.warning(
                        "Prior server pid=%d still alive after %.1fs grace — "
                        "terminating its process tree",
                        prior_pid,
                        self._grace_timeout,
                    )
                    _kill_process_tree(prior_pid)
                    if not _wait_for_exit(prior_pid, timeout=5.0):
                        logger.warning(
                            "Prior server pid=%d did not exit within timeout",
                            prior_pid,
                        )
        self._write({"pid": os.getpid(), "started_at": time.time()})

    def release(self) -> None:
        """Drop ownership if (and only if) we still hold it."""
        prior = self._read()
        if prior is None:
            return
        if prior.get("pid") != os.getpid():
            return
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug("Could not remove session file %s: %s", self._path, exc)

    def _read(self) -> dict | None:
        try:
            return json.loads(self._path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Session file %s unreadable: %s", self._path, exc)
            return None

    def _write(self, payload: dict) -> None:
        try:
            self._profile_dir.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(payload))
        except OSError as exc:
            logger.warning("Could not write session file %s: %s", self._path, exc)
