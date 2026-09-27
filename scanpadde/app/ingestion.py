"""Persistent observation and idempotent intake; inbox is read-only."""
import hashlib
import json
import logging
import time
from pathlib import Path
from . import jobs
from .db import transaction
from .formats import inspect, InvalidFile
from .paths import UnsafePath
from .storage import archive, sha, SourceChanged, IntegrityError
from .faults import checkpoint, fail
from .ocr import ENGINE, PREPROCESSING_VERSION, embedded_text, engine_version, recognize, render
from .remote_ocr import RemoteOcrClient, RemoteOcrError, ROTATION_POLICY_VERSION

log = logging.getLogger("scanpadde")

def observe(db, paths, now=None):
    now = time.time() if now is None else now
    seen = set()
    for entry in paths.guard("inbox").iterdir():
        relative = "inbox/" + entry.name
        seen.add(relative)
        try:
            p = paths.source(relative)
            stat = p.stat()
        except (OSError, UnsafePath):
            # Never stat through an unsafe link; retain a persistent rejected entry.
            db.execute("""INSERT INTO inbox_entries(relative_path,size_bytes,mtime_ns,
                first_seen_at,stable_since,last_seen_at,state,last_error)
                VALUES (?,-1,-1,?,?,?,'error','unsafe_or_unavailable')
                ON CONFLICT(relative_path) DO UPDATE SET state='error',
                last_error='unsafe_or_unavailable',last_seen_at=?""",
                (relative, now, now, now, now))
            continue
        with transaction(db):
            old = db.execute("SELECT * FROM inbox_entries WHERE relative_path=?", (relative,)).fetchone()
            sig = stat.st_size, stat.st_mtime_ns
            if old is None:
                db.execute("""INSERT INTO inbox_entries(relative_path,size_bytes,mtime_ns,
                    first_seen_at,stable_since,last_seen_at,state) VALUES (?,?,?,?,?,?,'waiting')""",
                    (relative, *sig, now, now, now))
            elif (old["size_bytes"], old["mtime_ns"]) != sig or old["state"] in ("missing", "changed"):
                db.execute("""UPDATE inbox_entries SET size_bytes=?,mtime_ns=?,stable_since=?,
                    last_seen_at=?,state='waiting',source_file_id=NULL,last_error=NULL,
                    generation=generation+1 WHERE relative_path=?""", (*sig, now, now, relative))
            else:
                db.execute("UPDATE inbox_entries SET last_seen_at=? WHERE relative_path=?", (now, relative))
                if old["state"] == "waiting" and now - old["stable_since"] >= 15:
                    payload = {"relative": relative, "size": sig[0], "mtime": sig[1],
                               "generation": old["generation"]}
                    key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
                    jobs.enqueue(db, "ingest:" + key, "ingest_file", relative, payload, now)
                    db.execute("UPDATE inbox_entries SET state='queued' WHERE relative_path=?", (relative,))
    for row in db.execute("SELECT relative_path FROM inbox_entries").fetchall():
        if row[0] not in seen:
            db.execute("UPDATE inbox_entries SET state='missing' WHERE relative_path=?", (row[0],))

def execute(db, paths, job, ocr_config=None):
    payload = json.loads(job["payload_json"])
    last_beat = [0.0]
    def heartbeat():
        now = time.time()
        if now - last_beat[0] >= 2:
            db.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (now, job["id"]))
            last_beat[0] = now
    try:
        if job["job_type"] == "ingest_file":
            row = db.execute("SELECT * FROM inbox_entries WHERE relative_path=?", (payload["relative"],)).fetchone()
            if row is None or row["generation"] != payload["generation"]:
                with transaction(db):
                    jobs.complete(db, job["id"])
                return
            saved, digest, size, mime, pages = archive(
                paths, payload["relative"], (payload["size"], payload["mtime"]), heartbeat)
            now = time.time()
            with transaction(db):
                db.execute("""INSERT INTO source_files(sha256,original_filename,archived_path,
                    mime_type,size_bytes,page_count,status,first_seen_at,last_seen_at,archived_at)
                    VALUES (?,?,?,?,?,?,'archived',?,?,?)
                    ON CONFLICT(sha256) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                    (digest, Path(payload["relative"]).name, saved, mime, size, len(pages), now, now, now))
                source_id = db.execute("SELECT id FROM source_files WHERE sha256=?", (digest,)).fetchone()[0]
                db.execute("""UPDATE inbox_entries SET state='processed',source_file_id=?,last_error=NULL
                    WHERE relative_path=? AND generation=?""",
                    (source_id, payload["relative"], payload["generation"]))
                jobs.enqueue(db, "pages:" + digest, "enumerate_pages", source_id, {"source_id": source_id})
                jobs.complete(db, job["id"])
        elif job["job_type"] == "enumerate_pages":
            row = db.execute("SELECT * FROM source_files WHERE id=?", (payload["source_id"],)).fetchone()
            p = paths.guard(row["archived_path"])
            if sha(p) != row["sha256"]:
                raise IntegrityError("archive_hash_mismatch")
            _, pages = inspect(p, Path(row["original_filename"]).suffix)
            with transaction(db):
                for n, (width, height) in enumerate(pages, 1):
                    db.execute("""INSERT INTO pages(source_file_id,page_number,width,height,created_at)
                        VALUES (?,?,?,?,?) ON CONFLICT(source_file_id,page_number) DO NOTHING""",
                        (row["id"], n, width, height, time.time()))
                for page in db.execute("SELECT id,page_number FROM pages WHERE source_file_id=?", (row["id"],)):
                    jobs.enqueue(db, f"render:{row['sha256']}:{page['page_number']}", "render_page",
                                 page["id"], {"page_id": page["id"]})
                db.execute("""UPDATE source_files SET status='ready',page_count=?,
                    error_code=NULL,error_message=NULL WHERE id=?""", (len(pages), row["id"]))
                jobs.complete(db, job["id"])
        elif job["job_type"] == "render_page":
            page = db.execute("""SELECT p.*,s.archived_path,s.sha256,s.original_filename FROM pages p
                JOIN source_files s ON s.id=p.source_file_id WHERE p.id=?""", (payload["page_id"],)).fetchone()
            if page is None: raise IntegrityError("page_not_found")
            source = paths.guard(page["archived_path"])
            extension = Path(page["original_filename"]).suffix
            text = embedded_text(source, page["page_number"], extension)
            with transaction(db):
                if text is not None:
                    db.execute("""INSERT OR IGNORE INTO ocr_results(page_id,engine,engine_version,language,
                      preprocessing_version,rotation,source_type,text,words_json,confidence,image_sha256,status,created_at)
                      VALUES (?, 'embedded_pdf', 'pypdf', '', 'none-v1', 0, 'embedded_text', ?, NULL, NULL, '', 'completed', ?)""",
                      (page["id"], text, time.time()))
                    db.execute("UPDATE pages SET status='ocr_completed',rotation=0 WHERE id=?", (page["id"],))
                else:
                    db.execute("UPDATE pages SET status='pending_render' WHERE id=?", (page["id"],))
            if text is None:
                rendered_path, digest = render(paths, source, page["sha256"], page["page_number"], extension)
                with transaction(db):
                    db.execute("UPDATE pages SET status='pending_ocr',rendered_path=?,image_sha256=? WHERE id=?",
                               (rendered_path, digest, page["id"]))
                    # Disabled is a safe production default: rendering may finish, but no OCR is run locally.
                    if getattr(ocr_config, "backend", "local") != "disabled":
                        jobs.enqueue(db, f"ocr:{page['id']}:{digest}:{ENGINE}:{PREPROCESSING_VERSION}", "ocr_page",
                                     page["id"], {"page_id": page["id"]})
            with transaction(db): jobs.complete(db, job["id"])
        elif job["job_type"] == "ocr_page":
            page = db.execute("SELECT * FROM pages WHERE id=?", (payload["page_id"],)).fetchone()
            if page is None or not page["rendered_path"] or not page["image_sha256"]: raise IntegrityError("page_not_rendered")
            backend = getattr(ocr_config, "backend", "local")
            if backend == "disabled":
                # This is deliberately retryable but does no local OCR work.
                raise RemoteOcrError("ocr_backend_disabled")
            if backend == "local":
                version = engine_version()
                engine = ENGINE
            elif backend == "remote":
                client = RemoteOcrClient(ocr_config.worker_url, ocr_config.worker_token, ocr_config.worker_ca,
                                         ocr_config.connect_timeout, ocr_config.read_timeout)
                worker = client.version()
                version, engine = worker["engine_version"], worker["engine"]
            else:
                raise IntegrityError("invalid_ocr_backend")
            existing = db.execute("""SELECT id FROM ocr_results WHERE page_id=? AND image_sha256=? AND engine=?
                AND engine_version=? AND language=? AND preprocessing_version=? AND status='completed'""",
                (page["id"],page["image_sha256"],engine,version,"deu+eng",PREPROCESSING_VERSION)).fetchone()
            if existing:
                with transaction(db):
                    db.execute("UPDATE pages SET status='ocr_completed' WHERE id=?", (page["id"],))
                    jobs.complete(db, job["id"])
                return
            with transaction(db): db.execute("UPDATE pages SET status='ocr_running' WHERE id=?", (page["id"],))
            # Test-only checkpoints are inert unless SCANPADDE_TEST_FAULT names them.
            checkpoint("ocr_page_running")
            fail("ocr_page")
            if backend == "local":
                text, words, confidence, rotation = recognize(paths.guard(page["rendered_path"]))
            else:
                result = client.recognize(job["job_key"], paths.guard(page["rendered_path"]), page["image_sha256"],
                                          "deu+eng", PREPROCESSING_VERSION)
                text, words, confidence, rotation = result["text"], json.dumps(result["words"], ensure_ascii=False, separators=(",", ":")), result["confidence"], result["rotation"]
            with transaction(db):
                db.execute("""INSERT OR IGNORE INTO ocr_results(page_id,engine,engine_version,language,
                   preprocessing_version,rotation,source_type,text,words_json,confidence,image_sha256,status,created_at)
                   VALUES (?,?,?,?,?,?, 'ocr', ?,?,?,?,'completed',?)""",
                   (page["id"],engine,version,"deu+eng",PREPROCESSING_VERSION,rotation,text,words,confidence,
                    page["image_sha256"],time.time()))
                db.execute("UPDATE pages SET status='ocr_completed',rotation=? WHERE id=?", (rotation,page["id"]))
                jobs.complete(db, job["id"])
    except Exception as exc:
        # Never store arbitrary parser exception text or source contents.
        code = str(exc) if isinstance(exc, (InvalidFile, IntegrityError, UnsafePath, SourceChanged)) else type(exc).__name__
        retry = (isinstance(exc, OSError) and not isinstance(exc, SourceChanged)) or isinstance(exc, RemoteOcrError)
        with transaction(db):
            state = jobs.fail(db, job, code, retry=retry)
            if job["job_type"] == "ingest_file":
                entry_state = "changed" if isinstance(exc, SourceChanged) else "queued" if state == "pending" else "error"
                db.execute("""UPDATE inbox_entries SET state=?,last_error=?
                    WHERE relative_path=? AND generation=?""",
                    (entry_state, code, payload["relative"], payload["generation"]))
            elif job["job_type"] == "enumerate_pages":
                db.execute("UPDATE source_files SET status='error',error_code=?,error_message=? WHERE id=?",
                           (code, code, payload["source_id"]))
            elif job["job_type"] == "ocr_page":
                db.execute("UPDATE pages SET status=? WHERE id=?", ("pending_ocr" if state == "pending" else "ocr_failed", payload["page_id"]))
            elif job["job_type"] == "render_page":
                db.execute("UPDATE pages SET status='pending_render' WHERE id=?", (payload["page_id"],))
        log.warning(json.dumps({"event": "job_error", "job_id": job["id"], "error_class": type(exc).__name__}))
