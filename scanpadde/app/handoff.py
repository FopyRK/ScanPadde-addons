"""One-time, local hand-off between two Home Assistant add-on identities.

Home Assistant gives an add-on from a custom repository its own ``/data``
directory.  The archive itself lives in ``/share/scanpadde`` and is already
shared, but the SQLite database, OCR cache and remote-OCR configuration need
an explicit local hand-off.  Nothing in this module sends data anywhere.
"""
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path


HANDOFF_DIR = "migration-handoff"
SNAPSHOT_NAME = "scanpadde.db"
OPTIONS_NAME = "options.json"
CA_NAME = "scanpadde-ocr-ca.crt"


def _handoff_dir(paths) -> Path:
    directory = paths.guard(HANDOFF_DIR)
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def _atomic_backup(source: Path, destination: Path) -> None:
    """Create a SQLite-consistent snapshot without copying WAL files."""
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".handoff-", delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        with sqlite3.connect(source) as source_db, sqlite3.connect(temporary) as target_db:
            source_db.backup(target_db)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def export_handoff(paths, data_dir: Path, config_dir: Path = Path("/config")) -> dict:
    """Write a local, permission-restricted transfer snapshot.

    The source remains online.  SQLite's backup API supplies a coherent copy
    even while the worker is idle between jobs.
    """
    source = data_dir / SNAPSHOT_NAME
    if not source.is_file():
        raise RuntimeError("handoff_source_database_missing")
    directory = _handoff_dir(paths)
    destination = directory / SNAPSHOT_NAME
    _atomic_backup(source, destination)

    options = data_dir / OPTIONS_NAME
    copied_options = False
    ca_copied = False
    if options.is_file():
        # Keep the option file local and never log or return its content.
        option_data = options.read_bytes()
        option_destination = directory / OPTIONS_NAME
        option_destination.write_bytes(option_data)
        os.chmod(option_destination, 0o600)
        copied_options = True
        try:
            values = json.loads(option_data)
        except (TypeError, ValueError):
            values = {}
        ca_value = values.get("ocr_worker_ca") if isinstance(values, dict) else None
        if isinstance(ca_value, str) and ca_value and not Path(ca_value).is_absolute() and ".." not in Path(ca_value).parts:
            ca_source = config_dir / ca_value
            if ca_source.is_file():
                ca_destination = directory / CA_NAME
                ca_destination.write_bytes(ca_source.read_bytes())
                os.chmod(ca_destination, 0o600)
                ca_copied = True

    marker = directory / "manifest.json"
    marker.write_text(json.dumps({"format": 1, "created_at": time.time(), "options": copied_options,
                                  "ca": ca_copied}, separators=(",", ":")), encoding="utf-8")
    os.chmod(marker, 0o600)
    return {"ok": True, "snapshot": True, "options": copied_options, "ca": ca_copied}


def import_handoff(paths, data_dir: Path, config_dir: Path = Path("/config")) -> bool:
    """Import a local hand-off without replacing configured release data.

    Home Assistant creates a default ``options.json`` for a newly installed
    add-on before its first process starts.  That file must not prevent the
    one-time migration of an already configured remote OCR worker.  A later
    start may therefore repair *only* still-default options and a missing CA;
    an explicit release configuration is never overwritten.
    """
    destination = data_dir / SNAPSHOT_NAME
    directory = paths.guard(HANDOFF_DIR)
    source = directory / SNAPSHOT_NAME
    marker = directory / "manifest.json"
    if not source.is_file() or not marker.is_file():
        return False
    try:
        manifest = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if manifest.get("format") != 1:
        return False
    data_dir.mkdir(parents=True, exist_ok=True)
    copied = False
    if not destination.exists():
        _atomic_backup(source, destination)
        copied = True
    options_source = directory / OPTIONS_NAME
    options_destination = data_dir / OPTIONS_NAME
    if options_source.is_file() and _has_default_ocr_options(options_destination):
        options_destination = data_dir / OPTIONS_NAME
        options_destination.write_bytes(options_source.read_bytes())
        os.chmod(options_destination, 0o600)
        copied = True
    ca_source = directory / CA_NAME
    ca_destination = config_dir / CA_NAME
    if ca_source.is_file() and not ca_destination.exists():
        config_dir.mkdir(parents=True, exist_ok=True)
        ca_destination.write_bytes(ca_source.read_bytes())
        os.chmod(ca_destination, 0o600)
        copied = True
    return copied


def _has_default_ocr_options(path: Path) -> bool:
    """Return true only for Supervisor-created, unconfigured OCR options."""
    if not path.exists():
        return True
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(values, dict):
        return False
    return (values.get("ocr_backend", "disabled") == "disabled"
            and not values.get("ocr_worker_url", "")
            and not values.get("ocr_worker_ca", "")
            and not values.get("ocr_worker_token", ""))
