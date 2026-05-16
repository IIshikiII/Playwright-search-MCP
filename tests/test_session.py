"""Unit tests for plsearch.session: SessionRegistry + process helpers."""

import json
import os
import subprocess
import sys
import time

import pytest

from plsearch.session import (
    SessionRegistry,
    _descendants_unix,
    _kill_process_tree,
    _process_alive,
    _wait_for_exit,
)


class TestProcessAlive:
    def test_returns_true_for_self(self) -> None:
        assert _process_alive(os.getpid()) is True

    def test_returns_false_for_zero(self) -> None:
        assert _process_alive(0) is False

    def test_returns_false_for_negative(self) -> None:
        assert _process_alive(-1) is False

    def test_returns_false_for_definitely_dead_pid(self) -> None:
        # Spawn a no-op process so we know a previously-real PID that is now dead.
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        # Give the OS a beat to reap the entry.
        for _ in range(50):
            if not _process_alive(proc.pid):
                break
            time.sleep(0.02)
        assert _process_alive(proc.pid) is False


class TestKillProcessTree:
    def test_kills_long_running_subprocess(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            assert _process_alive(proc.pid)
            _kill_process_tree(proc.pid)
            assert _wait_for_exit(proc.pid, timeout=10.0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_noop_on_dead_pid(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        # Should not raise even though pid is already dead.
        _kill_process_tree(proc.pid)


class TestWaitForExit:
    def test_returns_true_for_already_dead_pid(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        assert _wait_for_exit(proc.pid, timeout=2.0)

    def test_returns_false_when_pid_outlives_timeout(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        try:
            assert _wait_for_exit(proc.pid, timeout=0.2) is False
        finally:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="pgrep is Unix-only")
class TestDescendantsUnix:
    def test_returns_empty_for_leaf_process(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        try:
            time.sleep(0.2)
            assert _descendants_unix(proc.pid) == []
        finally:
            proc.kill()
            proc.wait(timeout=5)


class TestSessionRegistryClaim:
    def test_writes_pid_file_on_first_claim(self, tmp_path) -> None:
        registry = SessionRegistry(str(tmp_path))
        registry.claim()

        data = json.loads((tmp_path / SessionRegistry.FILENAME).read_text())
        assert data["pid"] == os.getpid()
        assert "started_at" in data

    def test_overwrites_stale_pid_file_with_dead_prior(self, tmp_path) -> None:
        # Write a session file pointing at a long-dead PID.
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        dead_pid = proc.pid
        path = tmp_path / SessionRegistry.FILENAME
        path.write_text(json.dumps({"pid": dead_pid, "started_at": 0.0}))

        SessionRegistry(str(tmp_path)).claim()

        data = json.loads(path.read_text())
        assert data["pid"] == os.getpid()

    def test_kills_living_prior_after_grace_expires(self, tmp_path) -> None:
        """Genuinely stuck prior is killed after grace period."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            path = tmp_path / SessionRegistry.FILENAME
            path.write_text(json.dumps({"pid": proc.pid, "started_at": 0.0}))

            # grace_timeout=0 skips the wait, going straight to kill.
            SessionRegistry(str(tmp_path), grace_timeout=0.0).claim()

            assert _wait_for_exit(proc.pid, timeout=10.0)
            data = json.loads(path.read_text())
            assert data["pid"] == os.getpid()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_grace_period_lets_prior_exit_without_taskkill(self, tmp_path) -> None:
        """Cleanly-shutting-down prior is allowed to exit on its own.

        Mirrors the /mcp reconnect scenario: parent (Claude Code) closes the
        old subprocess's stdin while spawning the new one. The new one's
        claim() should *wait* for the old to exit, not force-kill it — a
        forced kill becomes an abnormal exit code that the parent reports
        as a server failure.
        """
        # Prior process exits on its own after 0.5s (simulates parent-initiated
        # clean shutdown). claim() with a 5s grace must observe the natural exit.
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.5)"]
        )
        try:
            path = tmp_path / SessionRegistry.FILENAME
            path.write_text(json.dumps({"pid": proc.pid, "started_at": 0.0}))

            start = time.monotonic()
            SessionRegistry(str(tmp_path), grace_timeout=5.0).claim()
            elapsed = time.monotonic() - start

            # claim() must wait for the natural exit (~0.5s), not return
            # immediately, and certainly not take the full 5s grace.
            assert 0.3 < elapsed < 4.0, f"unexpected claim duration: {elapsed:.2f}s"

            # Crucially: process must have exited with code 0 (natural exit)
            # rather than being killed. Popen.returncode captures this.
            assert proc.poll() == 0, (
                f"prior exited with code {proc.returncode}; expected 0 "
                "(would have been non-zero if we taskkill'd it)"
            )

            data = json.loads(path.read_text())
            assert data["pid"] == os.getpid()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_idempotent_when_we_already_own(self, tmp_path) -> None:
        """Re-claiming with our own PID should be safe (no self-kill)."""
        registry = SessionRegistry(str(tmp_path))
        registry.claim()
        registry.claim()  # Must not kill ourselves.

        data = json.loads((tmp_path / SessionRegistry.FILENAME).read_text())
        assert data["pid"] == os.getpid()

    def test_tolerates_malformed_session_file(self, tmp_path) -> None:
        (tmp_path / SessionRegistry.FILENAME).write_text("not json at all{{{")

        SessionRegistry(str(tmp_path)).claim()  # Must not raise.

        data = json.loads((tmp_path / SessionRegistry.FILENAME).read_text())
        assert data["pid"] == os.getpid()

    def test_creates_profile_dir_when_missing(self, tmp_path) -> None:
        missing = tmp_path / "fresh-profile"
        SessionRegistry(str(missing)).claim()

        assert (missing / SessionRegistry.FILENAME).exists()


class TestSessionRegistryRelease:
    def test_removes_file_when_we_own_it(self, tmp_path) -> None:
        registry = SessionRegistry(str(tmp_path))
        registry.claim()
        assert (tmp_path / SessionRegistry.FILENAME).exists()

        registry.release()
        assert not (tmp_path / SessionRegistry.FILENAME).exists()

    def test_leaves_file_alone_when_owned_by_other_pid(self, tmp_path) -> None:
        path = tmp_path / SessionRegistry.FILENAME
        other_pid = os.getpid() + 99999  # Vanishingly unlikely to be us.
        path.write_text(json.dumps({"pid": other_pid, "started_at": 0.0}))

        SessionRegistry(str(tmp_path)).release()

        assert path.exists()
        data = json.loads(path.read_text())
        assert data["pid"] == other_pid

    def test_silent_when_no_file_exists(self, tmp_path) -> None:
        SessionRegistry(str(tmp_path)).release()  # Must not raise.
