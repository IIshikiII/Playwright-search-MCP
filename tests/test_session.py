"""Unit tests for plsearch.session: SessionRegistry + process helpers."""

import json
import os
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import psutil
import pytest

from plsearch.session import (
    SessionRegistry,
    _descendants_unix,
    _kill_process_tree,
    _process_alive,
    _reap_orphan_chromes,
    _wait_for_exit,
)


def _spawn_with_user_data_dir(path) -> subprocess.Popen:
    """Spawn a long-living sentinel process that carries ``--user-data-dir=path``
    in its real argv — so psutil.cmdline() will see it on every platform."""
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            f"--user-data-dir={path}",
        ]
    )


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    """Poll until ``pid`` is gone or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.02)
    return not _process_alive(pid)


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


class TestReapOrphanChromes:
    """Cross-platform reaper for Chrome processes holding our user-data-dir.

    Uses real subprocesses (not mocks) for happy-path coverage so we exercise
    the actual psutil-on-this-platform path. Each test cleans up its own
    sentinels in a finally block — never leak processes across tests.
    """

    def test_returns_zero_when_nothing_matches(self, tmp_path) -> None:
        # No sentinel spawned at all — scanner must walk every process and
        # find none matching our profile path.
        assert _reap_orphan_chromes(str(tmp_path / "unused")) == 0

    def test_kills_process_holding_our_user_data_dir(self, tmp_path) -> None:
        profile = tmp_path / "ours"
        profile.mkdir()
        proc = _spawn_with_user_data_dir(profile)
        try:
            # Give the kernel a beat to publish cmdline.
            time.sleep(0.2)
            assert _process_alive(proc.pid)

            # skip_pids=set() overrides the default self-tree skip; the sentinel
            # is our child only because pytest IS the calling process, which is
            # an unavoidable test-scaffolding artifact, not the production case.
            killed = _reap_orphan_chromes(str(profile), skip_pids=set())

            assert killed >= 1, "expected to kill at least our sentinel"
            assert _wait_dead(proc.pid), "sentinel did not die after kill()"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_leaves_other_user_data_dirs_alone(self, tmp_path) -> None:
        """Different `--user-data-dir` value → not our problem, don't kill."""
        ours = tmp_path / "ours"
        theirs = tmp_path / "theirs"
        ours.mkdir()
        theirs.mkdir()
        proc = _spawn_with_user_data_dir(theirs)
        try:
            time.sleep(0.2)
            assert _process_alive(proc.pid)

            killed = _reap_orphan_chromes(str(ours), skip_pids=set())

            assert killed == 0
            assert _process_alive(proc.pid), \
                "process holding an UNRELATED user-data-dir was killed"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_normalizes_paths_before_comparing(self, tmp_path) -> None:
        """`/foo/profile` and `/foo/./profile` refer to the same dir — match."""
        profile = tmp_path / "profile"
        profile.mkdir()
        # Sentinel carries the cosmetically weird form.
        weird = str(tmp_path / "." / "profile")
        proc = _spawn_with_user_data_dir(weird)
        try:
            time.sleep(0.2)

            killed = _reap_orphan_chromes(str(profile), skip_pids=set())

            assert killed >= 1
            assert _wait_dead(proc.pid)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_skips_direct_children_of_current_process(self, tmp_path) -> None:
        """A stray re-claim() must NOT kill our own freshly-launched Chrome.

        The sentinel we spawn IS a direct child of the test process; we use
        that to assert the defensive skip-children-of-self rule.
        """
        profile = tmp_path / "mine"
        profile.mkdir()
        proc = _spawn_with_user_data_dir(profile)
        try:
            time.sleep(0.2)
            assert _process_alive(proc.pid)

            killed = _reap_orphan_chromes(str(profile))

            assert killed == 0, \
                "reaper killed a direct child of the calling process"
            assert _process_alive(proc.pid)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_tolerates_process_disappearing_mid_scan(self, tmp_path) -> None:
        """psutil raises NoSuchProcess if a process exits between enumeration
        and cmdline read. Reaper must swallow it and keep going."""
        # Stub one process that vanishes, plus one good match — the good one
        # must still be killed.
        good = MagicMock()
        good.pid = 99998
        good.info = {
            "cmdline": ["/path/to/chrome", f"--user-data-dir={tmp_path}"],
            "ppid": 1,
        }
        good.kill = MagicMock()

        gone = MagicMock()
        gone.pid = 99997
        # Simulate cmdline disappearing.
        gone.info = {"cmdline": None, "ppid": 1}

        denied = MagicMock()
        denied.pid = 99996
        denied.info = {"cmdline": None, "ppid": 1}

        with patch(
            "plsearch.session.psutil.process_iter",
            return_value=[gone, denied, good],
        ):
            killed = _reap_orphan_chromes(str(tmp_path))

        assert killed == 1
        good.kill.assert_called_once()

    def test_logs_but_does_not_raise_when_kill_fails(self, tmp_path, caplog) -> None:
        """If proc.kill() raises (e.g., AccessDenied on a root-owned process),
        reaper must log a warning and keep its return-count honest."""
        unkillable = MagicMock()
        unkillable.pid = 99995
        unkillable.info = {
            "cmdline": ["chrome", f"--user-data-dir={tmp_path}"],
            "ppid": 1,
        }
        unkillable.kill.side_effect = psutil.AccessDenied(pid=99995)

        with patch(
            "plsearch.session.psutil.process_iter",
            return_value=[unkillable],
        ):
            with caplog.at_level("WARNING"):
                killed = _reap_orphan_chromes(str(tmp_path))

        assert killed == 0, "count should reflect successful kills only"
        assert any(
            "99995" in rec.message for rec in caplog.records
            if rec.levelname == "WARNING"
        ), "expected a warning naming the un-killable pid"


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
