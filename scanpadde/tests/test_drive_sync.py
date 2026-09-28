import json
from pathlib import Path
from types import SimpleNamespace

from app.config import DriveSyncConfig
from app.drive_sync import DriveSync


def test_drive_pull_copies_only_and_never_moves_remote_before_processing(env):
    paths, db = env
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        if "lsjson" in args:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Path": "scan.pdf", "ID": "drive-file-1"}]))
        if "copyto" in args:
            Path(args[-1]).write_bytes(b"%PDF-1.4\n")
            return SimpleNamespace(returncode=0, stdout="")
        raise AssertionError(args)

    sync = DriveSync(DriveSyncConfig(True, "gdrive", "Paddenwirt/ScanPadde V2/Eingang",
                                     "Paddenwirt/ScanPadde V2/Verarbeitet", "/config/rclone.conf"), runner)
    assert sync.pull(db, paths) == 1
    entry = db.execute("SELECT remote_path,local_relative_path,state FROM drive_entries").fetchone()
    assert entry[0] == "scan.pdf" and entry[2] == "downloaded"
    assert paths.guard(entry[1]).is_file()
    assert not any("moveto" in call for call in calls)


def test_drive_archive_moves_only_after_archived_complete_ocr(env):
    paths, db = env
    now = 1.0
    db.execute("""INSERT INTO source_files(sha256,original_filename,archived_path,mime_type,size_bytes,page_count,status,
        first_seen_at,last_seen_at,archived_at) VALUES (?,?,?,?,?,?,?, ?,?,?)""",
               ("a" * 64, "scan.pdf", "originals/" + "a" * 64, "application/pdf", 1, 1, "ready", now, now, now))
    source_id = db.execute("SELECT id FROM source_files").fetchone()[0]
    db.execute("INSERT INTO pages(source_file_id,page_number,status,created_at) VALUES (?,?,?,?)",
               (source_id, 1, "ocr_completed", now))
    db.execute("""INSERT INTO inbox_entries(relative_path,size_bytes,mtime_ns,first_seen_at,stable_since,last_seen_at,state,source_file_id)
        VALUES (?,?,?,?,?,?,?,?)""", ("inbox/drive-abc-scan.pdf", 1, 1, now, now, now, "archived", source_id))
    db.execute("""INSERT INTO drive_entries(remote_path,remote_id,local_relative_path,processed_remote_path,state,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?)""", ("scan.pdf", "drive-file-1", "inbox/drive-abc-scan.pdf",
        "Paddenwirt/ScanPadde V2/Verarbeitet/drive-abc-scan.pdf", "downloaded", now, now))
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        if "moveto" in args:
            return SimpleNamespace(returncode=0, stdout="")
        if "lsjson" in args:
            return SimpleNamespace(returncode=0, stdout="[]")
        raise AssertionError(args)

    sync = DriveSync(DriveSyncConfig(True, "gdrive", "Paddenwirt/ScanPadde V2/Eingang",
                                     "Paddenwirt/ScanPadde V2/Verarbeitet", "/config/rclone.conf"), runner)
    assert sync.archive(db) == 1
    assert db.execute("SELECT state FROM drive_entries").fetchone()[0] == "moved"
    move = next(call for call in calls if "moveto" in call)
    assert "--ignore-existing" in move
