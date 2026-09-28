"""Safe, optional Google Drive intake via rclone.

Drive is treated as an external input queue: files are copied to the local
inbox first, then moved on Drive only once local hash archival and every-page
OCR have completed.  All rclone calls use argument lists (never a shell).
"""
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path, PurePosixPath

from .config import DriveSyncConfig
from .storage import sync_directory

log = logging.getLogger("scanpadde")
_ALLOWED_SUFFIXES = {".pdf", ".tif", ".tiff", ".png", ".jpg", ".jpeg"}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


class DriveSyncError(RuntimeError):
    pass


def _safe_name(remote_path: str, remote_id: str) -> str | None:
    name = PurePosixPath(remote_path).name
    suffix = Path(name).suffix.lower()
    if not name or name in {".", ".."} or suffix not in _ALLOWED_SUFFIXES:
        return None
    stem = _SAFE_NAME.sub("_", Path(name).stem).strip(" ._")[:120] or "scan"
    marker = hashlib.sha256((remote_id + "\0" + remote_path).encode("utf-8")).hexdigest()[:12]
    return f"drive-{marker}-{stem}{suffix}"


class DriveSync:
    def __init__(self, config: DriveSyncConfig, runner=subprocess.run):
        self.config = config
        self.runner = runner
        self.last_error = None
        self.last_successful_sync = None
        self.next_poll_at = 0.0

    @property
    def configured(self):
        return bool(self.config.enabled and self.config.config_path)

    def _run(self, *args):
        if not self.config.config_path:
            raise DriveSyncError("not_configured")
        try:
            result = self.runner(["rclone", "--config", self.config.config_path, *args],
                                 check=False, capture_output=True, text=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DriveSyncError("rclone_unavailable") from exc
        if result.returncode:
            raise DriveSyncError("rclone_failed")
        return result.stdout

    def _remote(self, path: str) -> str:
        return f"{self.config.remote}:{path}"

    def pull(self, db, paths):
        """Copy new direct-child Drive files to the local inbox, never move them."""
        if not self.configured:
            return 0
        if time.monotonic() < self.next_poll_at:
            return 0
        self.next_poll_at = time.monotonic() + 30.0
        try:
            raw = self._run("lsjson", "--files-only", "--max-depth", "1", self._remote(self.config.inbox_path))
            entries = json.loads(raw)
            if not isinstance(entries, list):
                raise DriveSyncError("invalid_listing")
            copied = 0
            for item in entries[:20]:
                if not isinstance(item, dict):
                    continue
                remote_path = item.get("Path")
                remote_id = item.get("ID")
                if not isinstance(remote_path, str) or "/" in remote_path or not isinstance(remote_id, str):
                    continue
                filename = _safe_name(remote_path, remote_id)
                if filename is None:
                    continue
                rel = "inbox/" + filename
                known = db.execute("SELECT state FROM drive_entries WHERE remote_path=?", (remote_path,)).fetchone()
                if known is not None:
                    continue
                target = paths.guard(rel)
                # Never overwrite a locally supplied scan or a stale partial
                # transfer.  The remote entry remains available for retry.
                if target.exists():
                    raise DriveSyncError("local_name_collision")
                tmp = paths.guard("inbox/.drive-" + filename + ".part")
                if tmp.exists():
                    tmp.unlink()
                self._run("copyto", self._remote(self.config.inbox_path + "/" + remote_path), str(tmp))
                if not tmp.is_file() or tmp.is_symlink() or tmp.stat().st_size == 0:
                    tmp.unlink(missing_ok=True)
                    raise DriveSyncError("download_invalid")
                os.replace(tmp, target)
                sync_directory(paths.guard("inbox"))
                destination_name = _safe_name(remote_path, remote_id)
                processed_remote = self.config.processed_path + "/" + destination_name
                now = time.time()
                db.execute("""INSERT INTO drive_entries(remote_path,remote_id,local_relative_path,processed_remote_path,
                    state,created_at,updated_at) VALUES (?,?,?,?, 'downloaded',?,?)""",
                           (remote_path, remote_id, rel, processed_remote, now, now))
                copied += 1
            self.last_error = None
            self.last_successful_sync = time.time()
            return copied
        except (DriveSyncError, ValueError, json.JSONDecodeError) as exc:
            self.last_error = str(exc)
            logging.getLogger("scanpadde").error(json.dumps({"event": "drive_pull_error", "error_class": type(exc).__name__}))
            return 0

    def archive(self, db):
        """Move the remote copy only after local archive and OCR are complete."""
        if not self.configured:
            return 0
        rows = db.execute("""SELECT d.remote_path,d.processed_remote_path,d.local_relative_path,d.source_file_id
            FROM drive_entries d JOIN inbox_entries i ON i.relative_path=d.local_relative_path
            JOIN source_files s ON s.id=i.source_file_id
            WHERE d.state='downloaded' AND i.state='archived' AND s.status='ready'
            AND (SELECT count(*) FROM pages p WHERE p.source_file_id=s.id AND p.status='ocr_completed')=s.page_count""").fetchall()
        moved = 0
        for row in rows:
            try:
                self._run("moveto", "--ignore-existing", self._remote(self.config.inbox_path + "/" + row["remote_path"]),
                          self._remote(row["processed_remote_path"]))
                # A success is confirmed by a missing source, preventing a skipped
                # collision from ever being recorded as a completed Drive move.
                remaining = self._run("lsjson", "--files-only", "--max-depth", "1",
                                      self._remote(self.config.inbox_path + "/" + row["remote_path"]))
                if json.loads(remaining) != []:
                    raise DriveSyncError("remote_move_not_confirmed")
                db.execute("UPDATE drive_entries SET state='moved',last_error=NULL,updated_at=? WHERE remote_path=?",
                           (time.time(), row["remote_path"]))
                moved += 1
            except (DriveSyncError, ValueError, json.JSONDecodeError):
                db.execute("UPDATE drive_entries SET last_error='remote_move_retry',updated_at=? WHERE remote_path=?",
                           (time.time(), row["remote_path"]))
        return moved
