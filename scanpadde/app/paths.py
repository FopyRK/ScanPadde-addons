"""Fixed production boundary; test roots require explicit Python injection."""
import os
import stat
from pathlib import Path

class UnsafePath(ValueError):
    pass

class Paths:
    def __init__(self, root=Path("/share/scanpadde"), data=Path("/data")):
        self.root = Path(os.path.abspath(root))
        self.data = Path(os.path.abspath(data))
        self._no_links(self.root)
        self._no_links(self.data)

    @staticmethod
    def _no_links(path):
        for part in (*reversed(path.parents), path):
            if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                raise UnsafePath("link_rejected")

    def guard(self, path):
        path = Path(path)
        if ".." in path.parts:
            raise UnsafePath("traversal_rejected")
        if not path.is_absolute():
            path = self.root / path
        self._no_links(path)
        self._no_links(self.root)
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise UnsafePath("outside_work_root")
        return resolved

    def initialize(self):
        self._no_links(self.data)
        self.data.mkdir(parents=True, exist_ok=True)
        for name in ("scanpadde.db", "scanpadde.db-wal", "scanpadde.db-shm", "worker.lock"):
            self._no_links(self.data / name)
        for name in ("inbox", "processed", "originals", "pages", "failed", "export"):
            self.guard(name).mkdir(parents=True, exist_ok=True)
        for name in ("export/ready", "export/superseded", "export/failed"):
            self.guard(name).mkdir(parents=True, exist_ok=True)

    def check(self):
        for name in ("inbox", "processed", "originals", "pages", "failed", "export"):
            p = self.guard(name)
            if not p.is_dir() or not os.access(p, os.R_OK | os.W_OK):
                raise OSError("work_directory_unavailable")
        for name in ("export/ready", "export/superseded", "export/failed"):
            p = self.guard(name)
            if not p.is_dir() or not os.access(p, os.R_OK | os.W_OK):
                raise OSError("work_directory_unavailable")

    def source(self, relative):
        p = self.guard(relative)
        if p.parent != self.guard("inbox"):
            raise UnsafePath("not_direct_inbox_file")
        s = p.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
            raise UnsafePath("not_single_regular_file")
        return p
