"""Run-lock acquisition durability."""


def test_acquire_fsyncs_pid_to_disk(tmp_path, monkeypatch):
    """The PID write must hit disk before acquire() returns."""
    import os

    lock_file = tmp_path / "run.lock"
    monkeypatch.setattr("qobuz_fetch.config.LOCK_FILE", lock_file)

    fsynced_fds = []
    orig_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: fsynced_fds.append(fd) or orig_fsync(fd))

    from qobuz_fetch import run_lock

    fp = run_lock.acquire()
    try:
        assert fp is not None
        assert fp.fileno() in fsynced_fds
        assert lock_file.read_text().strip() == str(os.getpid())
    finally:
        if fp is not None:
            fp.close()
