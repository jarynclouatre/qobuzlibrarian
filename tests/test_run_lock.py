"""Run-lock acquisition: durability, mutual exclusion, and graceful degrade."""
import pytest


def test_acquire_fsyncs_pid_to_disk(tmp_path, monkeypatch):
    """The PID write must hit disk before acquire() returns."""
    import os

    lock_file = tmp_path / "run.lock"
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    fsynced_fds = []
    orig_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: fsynced_fds.append(fd) or orig_fsync(fd))

    from qobuz_librarian import run_lock

    fp = run_lock.acquire()
    try:
        assert fp is not None
        assert fp.fileno() in fsynced_fds
        assert lock_file.read_text().strip() == str(os.getpid())
    finally:
        if fp is not None:
            fp.close()


def test_second_acquire_while_held_raises_lockbusy_with_holder_pid(tmp_path, monkeypatch):
    """While one run holds the lock, the next acquire() is refused and names it."""
    import os

    lock_file = tmp_path / "run.lock"
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    from qobuz_librarian import run_lock

    held = run_lock.acquire()
    try:
        assert held is not None
        with pytest.raises(run_lock.LockBusy) as caught:
            run_lock.acquire()
        assert caught.value.pid == str(os.getpid())
    finally:
        held.close()

    # Releasing the handle frees the lock, so the next run can take it.
    again = run_lock.acquire()
    assert again is not None
    again.close()


def test_acquire_degrades_to_none_when_flock_unsupported(tmp_path, monkeypatch, caplog):
    """A mount that can't flock (ENOLCK/EOPNOTSUPP) drops the lock loudly, not crash."""
    import errno
    import fcntl
    import logging

    lock_file = tmp_path / "run.lock"
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    def no_flock(fd, op):
        raise OSError(errno.ENOLCK, "no locks available")
    monkeypatch.setattr(fcntl, "flock", no_flock)

    from qobuz_librarian import run_lock

    with caplog.at_level(logging.WARNING, logger="qobuz_librarian"):
        assert run_lock.acquire() is None
    assert any("single-instance lock" in r.getMessage() for r in caplog.records)
