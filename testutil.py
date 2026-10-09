"""Shared helpers for the test suites."""
import os
import shutil
import stat
import sys
import time


def remove_tree(path: str, wait: float = 5.0) -> bool:
    """Delete a test's scratch folder, and say so if it cannot.

    On Windows a file or folder stays locked for a moment after the process that held
    it exits (the scratch hub's SQLite files, a log, or a folder a spawned session ran
    in), so a single rmtree(ignore_errors=True) quietly leaves empty folders behind.
    Retry for up to `wait` seconds, clearing read-only flags as we go."""
    def unlock(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    end = time.time() + wait
    while True:
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=unlock)
            else:
                shutil.rmtree(path, onerror=unlock)
        except OSError:
            pass
        if not os.path.exists(path):
            return True
        if time.time() >= end:
            print(f"note: could not remove the scratch folder {path}; delete it by hand")
            return False
        time.sleep(0.25)
