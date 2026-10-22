"""A cross-process lock: a file created with O_EXCL. Works the same on Windows.

Used where separate mlib runs must take turns - the catalog (the background job
runs `mlib add` while you work) and sovmusic.ru (which bans parallel clients).
"""
import os
import time
from contextlib import contextmanager
from pathlib import Path


class LockTimeout(RuntimeError):
    pass


@contextmanager
def file_lock(path, timeout=60, stale=120, poll=0.1):
    """Hold `path` for the duration. A lock older than `stale` seconds is taken
    to belong to a crashed process and broken; holders doing long work should
    call touch() to keep it fresh."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > stale:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.time() > deadline:
                raise LockTimeout("{} is held by another mlib (pid {})".format(path, _holder(path)))
            time.sleep(poll)
    try:
        yield path
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def touch(path):
    try:
        os.utime(str(path))
    except OSError:
        pass


def _holder(path):
    try:
        return Path(path).read_text().strip() or "?"
    except OSError:
        return "?"
