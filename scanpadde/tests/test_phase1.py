import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch
import pytest
from PIL import Image
from pypdf import PdfWriter
from fastapi.testclient import TestClient
from app import VERSION, jobs
from app.db import connect, initialize, transaction
from app.formats import inspect, InvalidFile
from app.ingestion import observe, execute
from app.main import create_app, Runtime
from app.paths import Paths, UnsafePath
from app.storage import archive, sha, SourceChanged, IntegrityError, process_lock, cleanup_temps
from app.handoff import export_handoff, import_handoff

def fixture_file(paths, name="test.pdf", pages=1, color="white"):
    p = paths.guard("inbox/" + name)
    if p.suffix == ".pdf":
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(100, 200)
        writer.write(p)
    else:
        image = Image.new("RGB", (30, 40), color)
        if p.suffix in (".tiff", ".tif"):
            image.save(p, save_all=True, append_images=[image.copy() for _ in range(pages-1)])
        else:
            image.save(p)
    return p

def saved(paths, p, heartbeat=lambda: None):
    s = p.stat()
    return archive(paths, p.relative_to(paths.root).as_posix(), (s.st_size, s.st_mtime_ns), heartbeat)

def intake(paths, db, now=None):
    now = time.time() - 16 if now is None else now
    observe(db, paths, now)
    observe(db, paths, now + 15)
    while (job := jobs.claim(db)) is not None:
        execute(db, paths, job)

@pytest.mark.parametrize("name,n", [("one.pdf",1),("multi.pdf",4),("a.png",1),("b.jpg",1),
    ("c.jpeg",1),("d.tiff",3),("e.tif",2)])
def test_formats(env, name, n):
    paths, db = env
    p = fixture_file(paths, name, n)
    _, pages = inspect(p, p.suffix)
    assert len(pages) == n
    intake(paths, db)
    assert db.execute("SELECT count(*) FROM pages").fetchone()[0] == n
    assert db.execute("SELECT status FROM source_files").fetchone()[0] == "ready"

def test_hash_atomic_and_inbox_unchanged(env):
    paths, _ = env
    p = fixture_file(paths)
    original = p.read_bytes()
    def midway():
        assert list(paths.guard("originals").iterdir())
        assert all(x.name.startswith(".archive-") for x in paths.guard("originals").iterdir())
    target, digest, *_ = saved(paths, p, midway)
    assert digest == hashlib.sha256(original).hexdigest()
    assert paths.guard(target).read_bytes() == p.read_bytes() == original
    assert sha(paths.guard(target)) == digest
    assert not list(paths.guard("originals").glob(".archive-*"))

def test_duplicate_and_rename(env):
    paths, db = env
    fixture_file(paths)
    intake(paths, db)
    intake(paths, db)
    paths.guard("inbox/copy.pdf").write_bytes(next(paths.guard("processed").iterdir()).read_bytes())
    intake(paths, db)
    observe(db, paths, time.time())
    assert db.execute("SELECT count(*) FROM source_files").fetchone()[0] == 1
    assert len(list(paths.guard("originals").iterdir())) == 1
    assert db.execute("SELECT count(*) FROM inbox_entries WHERE state='archived'").fetchone()[0] == 2
    assert not list(paths.guard("inbox").iterdir())
    assert len(list(paths.guard("processed").iterdir())) == 2

def test_completed_input_moves_to_local_processed_archive(env):
    paths, db = env
    source = fixture_file(paths, "finished.pdf", pages=2)
    intake(paths, db)
    db.execute("UPDATE pages SET status='ocr_completed'")
    observe(db, paths, time.time())
    assert not source.exists()
    archived = list(paths.guard("processed").iterdir())
    assert len(archived) == 1 and sha(archived[0]) == sha(paths.guard("originals").iterdir().__next__())
    assert db.execute("SELECT state FROM inbox_entries").fetchone()[0] == "archived"

def test_local_handoff_preserves_database_and_options(tmp_path):
    source_paths = Paths(tmp_path / "share/scanpadde", tmp_path / "source-data")
    source_paths.initialize()
    initialize(source_paths.data / "scanpadde.db")
    with connect(source_paths.data / "scanpadde.db") as db:
        db.execute("INSERT INTO inbox_entries(relative_path,size_bytes,mtime_ns,first_seen_at,stable_since,last_seen_at,state) VALUES(?,?,?,?,?,?,?)",
                   ("inbox/example.pdf", 1, 1, 1, 1, 1, "processed"))
    source_config = tmp_path / "source-config"
    source_config.mkdir()
    (source_config / "worker-ca.crt").write_text("test-ca", encoding="utf-8")
    (source_paths.data / "options.json").write_text(json.dumps({
        "ocr_backend": "remote", "ocr_worker_url": "https://worker.invalid",
        "ocr_worker_ca": "worker-ca.crt", "ocr_worker_token": "secret-not-logged"}), encoding="utf-8")
    result = export_handoff(source_paths, source_paths.data, source_config)
    assert result == {"ok": True, "snapshot": True, "options": True, "ca": True}

    target_data = tmp_path / "target-data"
    target_config = tmp_path / "target-config"
    assert import_handoff(source_paths, target_data, target_config) is True
    with connect(target_data / "scanpadde.db") as db:
        assert db.execute("SELECT count(*) FROM inbox_entries").fetchone()[0] == 1
    assert (target_data / "options.json").is_file()
    assert (target_config / "scanpadde-ocr-ca.crt").read_text(encoding="utf-8") == "test-ca"
    assert import_handoff(source_paths, target_data, target_config) is False


def test_local_handoff_repairs_supervisor_default_options(tmp_path):
    source_paths = Paths(tmp_path / "share/scanpadde", tmp_path / "source-data")
    source_paths.initialize()
    initialize(source_paths.data / "scanpadde.db")
    source_config = tmp_path / "source-config"
    source_config.mkdir()
    (source_config / "worker-ca.crt").write_text("test-ca", encoding="utf-8")
    (source_paths.data / "options.json").write_text(json.dumps({
        "ocr_backend": "remote", "ocr_worker_url": "https://worker.invalid",
        "ocr_worker_ca": "worker-ca.crt", "ocr_worker_token": "secret-not-logged"}), encoding="utf-8")
    export_handoff(source_paths, source_paths.data, source_config)

    target_data = tmp_path / "target-data"
    target_data.mkdir()
    initialize(target_data / "scanpadde.db")
    (target_data / "options.json").write_text(json.dumps({
        "ocr_backend": "disabled", "ocr_worker_url": "", "ocr_worker_ca": "", "ocr_worker_token": ""}), encoding="utf-8")
    target_config = tmp_path / "target-config"
    assert import_handoff(source_paths, target_data, target_config) is True
    options = json.loads((target_data / "options.json").read_text(encoding="utf-8"))
    assert options["ocr_backend"] == "remote"
    assert (target_config / "scanpadde-ocr-ca.crt").read_text(encoding="utf-8") == "test-ca"


def test_local_handoff_keeps_new_explicit_non_ocr_options(tmp_path):
    source_paths = Paths(tmp_path / "share/scanpadde", tmp_path / "source-data")
    source_paths.initialize()
    initialize(source_paths.data / "scanpadde.db")
    source_config = tmp_path / "source-config"; source_config.mkdir()
    (source_config / "worker-ca.crt").write_text("test-ca", encoding="utf-8")
    (source_paths.data / "options.json").write_text(json.dumps({
        "ocr_backend": "remote", "ocr_worker_url": "https://worker.invalid",
        "ocr_worker_ca": "worker-ca.crt", "ocr_worker_token": "secret"}), encoding="utf-8")
    export_handoff(source_paths, source_paths.data, source_config)
    target_data = tmp_path / "target-data"; target_data.mkdir()
    (target_data / "options.json").write_text(json.dumps({
        "ocr_backend": "disabled", "ocr_worker_url": "", "ocr_worker_ca": "", "ocr_worker_token": "",
        "ollama_enabled": True, "ollama_url": "http://192.168.10.168:11434", "ollama_model": "qwen2.5:1.5b"}), encoding="utf-8")
    target_config = tmp_path / "target-config"
    assert import_handoff(source_paths, target_data, target_config) is True
    options = json.loads((target_data / "options.json").read_text(encoding="utf-8"))
    assert options["ocr_backend"] == "remote"
    assert options["ollama_enabled"] is True and options["ollama_model"] == "qwen2.5:1.5b"

def test_same_name_changed_content(env):
    paths, db = env
    fixture_file(paths)
    intake(paths, db)
    fixture_file(paths, pages=2)
    intake(paths, db)
    assert db.execute("SELECT count(*) FROM source_files").fetchone()[0] == 2

def test_archive_never_overwritten(env):
    paths, _ = env
    p = fixture_file(paths)
    target, *_ = saved(paths, p)
    paths.guard(target).write_bytes(b"damaged")
    with pytest.raises(IntegrityError):
        saved(paths, p)
    assert paths.guard(target).read_bytes() == b"damaged"

def test_source_changes_during_copy(env):
    paths, _ = env
    p = fixture_file(paths)
    changed = False
    def change():
        nonlocal changed
        if not changed:
            with p.open("ab") as out:
                out.write(b"changed")
            changed = True
    with pytest.raises(SourceChanged):
        saved(paths, p, change)
    assert list(paths.guard("originals").iterdir()) == []

@pytest.mark.parametrize("relative", ["../paperless", "inbox/../../outside", "/share/paperless/consume"])
def test_path_guard(env, relative):
    paths, _ = env
    with pytest.raises(UnsafePath):
        paths.guard(relative)

def test_symlink(env):
    paths, _ = env
    target = fixture_file(paths)
    link = paths.root / "inbox/link.pdf"
    try:
        link.symlink_to(target)
    except OSError:
        # Windows without symlink privilege: exercise Windows junction below separately.
        pytest.skip("OS does not grant symlink creation; Linux container test required")
    with pytest.raises(UnsafePath):
        paths.source("inbox/link.pdf")

def test_mock_symlink_rejection(env):
    paths, _ = env
    with patch.object(Path, "is_symlink", return_value=True):
        with pytest.raises(UnsafePath):
            paths.guard("inbox/a.pdf")

def test_hardlinked_input_rejected(env):
    paths, _ = env
    p = fixture_file(paths)
    os.link(p, paths.guard("inbox/link.pdf"))
    with pytest.raises(UnsafePath):
        paths.source("inbox/link.pdf")

@pytest.mark.parametrize("kind", ["invalid_pdf", "unsupported", "mismatch", "oversize", "page_limit", "encrypted"])
def test_invalid_files(env, kind):
    paths, _ = env
    p = fixture_file(paths)
    ext = ".pdf"
    kwargs = {}
    if kind == "invalid_pdf":
        p.write_bytes(b"not pdf")
    elif kind == "unsupported":
        ext = ".gif"
    elif kind == "mismatch":
        p = fixture_file(paths, "a.png")
    elif kind == "oversize":
        kwargs["max_bytes"] = 10
    elif kind == "page_limit":
        p = fixture_file(paths, pages=2)
        kwargs["max_pages"] = 1
    elif kind == "encrypted":
        w = PdfWriter(); w.add_blank_page(100,200); w.encrypt("synthetic"); w.write(p)
    with pytest.raises(InvalidFile):
        inspect(p, ext, **kwargs)

def test_stability_15_seconds_and_reset(env):
    paths, db = env
    p = fixture_file(paths)
    now = time.time() - 100
    observe(db, paths, now)
    observe(db, paths, now+14.9)
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    observe(db, paths, now+15)
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    p.write_bytes(p.read_bytes()+b"\n")
    observe(db, paths, now+16)
    assert db.execute("SELECT state FROM inbox_entries").fetchone()[0] == "waiting"

def test_queue_atomic_single_running_and_keys(env):
    _, db = env
    for key in ("a","a","b"):
        jobs.enqueue(db, key, "ingest_file", "inbox/a.pdf", {})
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    job = jobs.claim(db)
    other = connect(db.execute("PRAGMA database_list").fetchone()[2])
    try:
        assert jobs.claim(other) is None
        with pytest.raises(sqlite3.IntegrityError):
            other.execute("UPDATE jobs SET state='running' WHERE id!=?", (job["id"],))
    finally:
        other.close()
    jobs.complete(db, job["id"])
    assert jobs.claim(db)["state"] == "running"

def test_retry_and_terminal_failure(env):
    _, db = env
    jobs.enqueue(db, "a", "ingest_file", "x", {})
    job = jobs.claim(db)
    assert jobs.fail(db, job, "io", retry=True) == "pending"
    assert jobs.claim(db) is None
    db.execute("UPDATE jobs SET available_at=0,attempt=2")
    job = jobs.claim(db)
    assert jobs.fail(db, job, "io", retry=True) == "failed"
    assert jobs.claim(db) is None

def test_restart_running_completed_and_stability(env):
    paths, db = env
    fixture_file(paths)
    observe(db, paths)
    jobs.enqueue(db, "a", "ingest_file", "x", {})
    job = jobs.claim(db)
    jobs.complete(db, job["id"])
    jobs.enqueue(db, "b", "ingest_file", "y", {})
    jobs.claim(db)
    with process_lock(paths.data):
        jobs.recover(db)
    assert [r[0] for r in db.execute("SELECT state FROM jobs ORDER BY id")] == ["completed","pending"]

def test_exclusive_process_lock(env):
    paths, _ = env
    with process_lock(paths.data):
        with pytest.raises(OSError):
            with process_lock(paths.data):
                pass

def test_db_migration_fk_unique_and_persistence(env):
    paths, db = env
    fixture_file(paths)
    intake(paths, db)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 7
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO pages(source_file_id,page_number,created_at) VALUES(999,1,0)")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO pages(source_file_id,page_number,created_at) VALUES(1,1,0)")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM source_files WHERE id=1")
    initialize(paths.data/"scanpadde.db")
    other = connect(paths.data/"scanpadde.db")
    try:
        assert other.execute("SELECT count(*) FROM source_files").fetchone()[0] == 1
    finally:
        other.close()

def test_future_schema_and_transaction_rollback(env):
    paths, db = env
    with pytest.raises(RuntimeError):
        with transaction(db):
            jobs.enqueue(db,"rollback","ingest_file","x",{})
            raise RuntimeError()
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    db.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError):
        initialize(paths.data/"scanpadde.db")

def test_failed_input_persists_without_repeating(env):
    paths, db = env
    paths.guard("inbox/bad.pdf").write_bytes(b"invalid")
    intake(paths, db)
    intake(paths, db)
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert db.execute("SELECT state FROM jobs").fetchone()[0] == "failed"
    assert list(paths.guard("originals").iterdir()) == []

def test_recovery_after_publication_before_database(env):
    paths, db = env
    p = fixture_file(paths, pages=2)
    saved(paths,p)
    assert db.execute("SELECT count(*) FROM source_files").fetchone()[0] == 0
    intake(paths,db)
    assert db.execute("SELECT count(*) FROM pages").fetchone()[0] == 2
    assert len(list(paths.guard("originals").iterdir())) == 1

def test_temporary_cleanup_does_not_remove_archive(env):
    paths, _ = env
    p=fixture_file(paths)
    saved(paths,p)
    paths.guard("originals/.archive-orphan").write_bytes(b"partial")
    with process_lock(paths.data):
        cleanup_temps(paths)
    assert len(list(paths.guard("originals").iterdir())) == 1

def test_health_status_sources_jobs_and_ingress(env):
    paths, db = env
    fixture_file(paths)
    intake(paths,db)
    app = create_app(paths,background=False)
    with TestClient(app, client=("172.30.32.2",1234)) as client:
        assert client.get("/api/health").json()["ok"]
        assert client.get("/api/status").json()["version"] == VERSION
        assert len(client.get("/api/sources").json()) == 1
        assert len(client.get("/api/sources/1").json()["pages"]) == 1
        assert 'review/1' in client.get("/").text
        assert client.get("/api/sources/999").status_code == 404
        # Phase 2A adds persistent render and OCR page checkpoints.
        assert len(client.get("/api/jobs").json()) == 4
        html = client.get("/").text
        assert 'href="static/style.css"' in html
        assert "https://" not in html
        assert client.get("/static/style.css").status_code == 200
        assert client.post("/api/sources").status_code == 405
        paths.guard("export").rmdir()
        assert client.get("/api/health").status_code == 503
        assert client.get("/api/health").json()["workspace"] == "unavailable"
        paths.guard("export").mkdir()
        with patch("sqlite3.connect", side_effect=sqlite3.OperationalError):
            assert client.get("/api/health").json()["database"] == "unavailable"
    with TestClient(create_app(paths,background=False),client=("127.0.0.1",1234)) as client:
        assert client.get("/api/health",headers={"X-Forwarded-For":"172.30.32.2"}).status_code == 403

def test_html_escapes_filename(env):
    paths, db=env
    fixture_file(paths)
    intake(paths,db)
    db.execute("UPDATE source_files SET original_filename='<script>alert(1)</script>.pdf'")
    with TestClient(create_app(paths,background=False),client=("172.30.32.2",1)) as client:
        assert "<script>" not in client.get("/").text
        assert "&lt;script&gt;" in client.get("/").text

def test_changed_source_gets_fresh_stability_window(env):
    paths,db=env
    p=fixture_file(paths)
    start=time.time()-16
    observe(db,paths,start); observe(db,paths,start+15)
    job=jobs.claim(db)
    p.write_bytes(p.read_bytes()+b"\n")
    execute(db,paths,job)
    assert db.execute("SELECT state FROM inbox_entries").fetchone()[0]=="changed"
    observe(db,paths)
    assert db.execute("SELECT state FROM inbox_entries").fetchone()[0]=="waiting"

def test_invalid_job_logging_has_no_document_text(env,caplog):
    paths,db=env
    paths.guard("inbox/private-name.pdf").write_bytes(b"SECRET-DOCUMENT-TEXT")
    intake(paths,db)
    assert "SECRET" not in caplog.text
    assert "private-name" not in caplog.text


def test_real_200_mib_limit_before_copy(env):
    paths,db=env
    p=paths.guard("inbox/large.pdf")
    with p.open("wb") as out:
        out.truncate(200*1024*1024+1)
    with pytest.raises(InvalidFile,match="size_limit"):
        saved(paths,p)
    assert list(paths.guard("originals").iterdir())==[]


def test_real_301_page_limit(env):
    paths,_=env
    p=fixture_file(paths,pages=301)
    with pytest.raises(InvalidFile,match="page_limit"):
        saved(paths,p)
    assert list(paths.guard("originals").iterdir())==[]


def test_parser_child_logs_suppressed(caplog):
    import logging
    logging.getLogger("pypdf._reader").warning("synthetic-private-parser-detail")
    assert "synthetic-private-parser-detail" not in caplog.text


def test_page_job_detects_archive_damage(env):
    paths,db=env
    fixture_file(paths)
    start=time.time()-16
    observe(db,paths,start);observe(db,paths,start+15)
    execute(db,paths,jobs.claim(db))
    row=db.execute("SELECT * FROM source_files").fetchone()
    paths.guard(row["archived_path"]).write_bytes(b"damaged")
    execute(db,paths,jobs.claim(db))
    assert db.execute("SELECT status FROM source_files").fetchone()[0]=="error"
    assert db.execute("SELECT count(*) FROM pages").fetchone()[0]==0
