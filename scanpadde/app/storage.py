import hashlib
import os
import tempfile
from contextlib import contextmanager
from .formats import MAX_BYTES, InvalidFile, inspect
from .faults import checkpoint

class SourceChanged(OSError):
    pass

class IntegrityError(ValueError):
    pass

def signature(s):
    # Windows Python 3.12 stat/fstat disagree on ctime (creation vs change time).
    # Identity, exact size and nanosecond mtime are portable; Linux adds ctime.
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
            s.st_ctime_ns if os.name != "nt" else None)

def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

@contextmanager
def process_lock(data):
    """Kernel releases the lifetime lock on process death; no stale PID guessing."""
    path = data / "worker.lock"
    if path.is_symlink():
        raise IntegrityError("lock_link")
    with open(path, "a+b") as f:
        if os.name == "nt":
            import msvcrt
            if os.fstat(f.fileno()).st_size == 0:
                f.write(b"0")
                f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)

def archive(paths, relative, expected, heartbeat=lambda: None):
    source = paths.source(relative)
    before = source.stat()
    if (before.st_size, before.st_mtime_ns) != tuple(expected):
        raise SourceChanged("source_changed")
    if before.st_size > MAX_BYTES:
        raise InvalidFile("size_limit")
    root = paths.guard("originals")
    fd, tmp_name = tempfile.mkstemp(prefix=".archive-", dir=root)
    tmp = paths.guard(tmp_name)
    try:
        h = hashlib.sha256()
        total = 0
        with os.fdopen(fd, "wb") as out:
            source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            with os.fdopen(source_fd, "rb") as src:
                if signature(os.fstat(src.fileno())) != signature(before):
                    raise SourceChanged("source_changed")
                for block in iter(lambda: src.read(1024 * 1024), b""):
                    total += len(block)
                    if total > MAX_BYTES:
                        raise InvalidFile("size_limit")
                    h.update(block)
                    out.write(block)
                    checkpoint("archive_copy_started")
                    heartbeat()
                    if signature(os.fstat(src.fileno())) != signature(before):
                        raise SourceChanged("source_changed")
                out.flush()
                os.fsync(out.fileno())
                if signature(os.fstat(src.fileno())) != signature(before):
                    raise SourceChanged("source_changed")
        if signature(paths.source(relative).stat()) != signature(before):
            raise SourceChanged("source_changed")
        digest = h.hexdigest()
        if sha(tmp) != digest:
            raise IntegrityError("copy_hash_mismatch")
        mime, pages = inspect(tmp, source.suffix)
        target = paths.guard(root / digest)
        if target.exists():
            if sha(target) != digest:
                raise IntegrityError("archive_hash_mismatch")
        else:
            try:
                os.link(tmp, target)
            except FileExistsError:
                if sha(paths.guard(target)) != digest:
                    raise IntegrityError("archive_collision")
            sync_directory(root)
        checkpoint("archive_published")
        return target.relative_to(paths.root).as_posix(), digest, total, mime, pages
    finally:
        paths.guard(tmp).unlink(missing_ok=True)

def cleanup_temps(paths):
    # Only after lifetime lock acquisition. Never remove final hash-named files.
    for p in paths.guard("originals").glob(".archive-*"):
        paths.guard(p).unlink()
