import json
import time
from .db import transaction

def enqueue(db, key, kind, subject, payload, now=None):
    now = time.time() if now is None else now
    db.execute("""INSERT INTO jobs(job_key,job_type,subject_type,subject_id,payload_json,
      state,available_at,created_at,updated_at) VALUES (?,?,?,?,?,'pending',?,?,?)
      ON CONFLICT(job_key) DO NOTHING""",
      (key, kind, "inbox_entry" if kind == "ingest_file" else "page" if kind in ("render_page", "ocr_page") else "source_file",
       str(subject), json.dumps(payload), now, now, now))

def recover(db):
    # Caller MUST own process_lock for entire worker lifetime.
    now = time.time()
    with transaction(db):
        db.execute("""UPDATE jobs SET state='pending',started_at=NULL,heartbeat_at=NULL,
            available_at=?,updated_at=?,error_code='recovered_after_stop',
            error_message='Recovered under exclusive process lock'
            WHERE state='running'""", (now, now))
        db.execute("UPDATE pages SET status='pending_ocr' WHERE status='ocr_running'")
        # A process gap cannot establish continuous stability. Observe anew.
        db.execute("UPDATE inbox_entries SET stable_since=? WHERE state='waiting'", (now,))

def claim(db):
    now = time.time()
    with transaction(db):
        if db.execute("SELECT 1 FROM jobs WHERE state='running'").fetchone():
            return None
        row = db.execute("""SELECT id FROM jobs WHERE state='pending' AND available_at<=?
            ORDER BY priority DESC,id LIMIT 1""", (now,)).fetchone()
        if row is None:
            return None
        db.execute("""UPDATE jobs SET state='running',attempt=attempt+1,started_at=?,
            heartbeat_at=?,updated_at=? WHERE id=?""", (now, now, now, row["id"]))
        return dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

def complete(db, job_id):
    now = time.time()
    db.execute("""UPDATE jobs SET state='completed',finished_at=?,updated_at=?,
        error_code=NULL,error_message=NULL WHERE id=?""", (now, now, job_id))

def fail(db, job, code, retry=False):
    now = time.time()
    state = "pending" if retry and job["attempt"] < job["max_attempts"] else "failed"
    db.execute("""UPDATE jobs SET state=?,available_at=?,finished_at=?,updated_at=?,
        error_code=?,error_message=? WHERE id=?""",
        (state, now + 5 * job["attempt"], None if state == "pending" else now,
         now, code, code, job["id"]))
    return state
