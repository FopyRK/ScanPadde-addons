"""Synthetic subprocess fault injection. Not copied into production image."""
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import Runtime
from app.paths import Paths
from app.db import connection
from app import jobs, ingestion
from app.storage import archive


def seed_ocr_pages(db, paths, count=3):
    """Create only synthetic raster checkpoints for process-level OCR tests."""
    from PIL import Image, ImageDraw, ImageFont
    from app.storage import sha
    now = time.time()
    paths.guard("originals/source").write_bytes(b"synthetic")
    db.execute("""INSERT INTO source_files(sha256,original_filename,archived_path,mime_type,size_bytes,page_count,status,first_seen_at,last_seen_at,archived_at)
        VALUES ('sourcehash','synthetic.pdf','originals/source','application/pdf',9,?,'ready',?,?,?)""", (count, now, now, now))
    font = ImageFont.load_default(size=64)
    for number in range(1, count + 1):
        target = paths.guard(f"pages/sourcehash/{number:04d}.png")
        target.parent.mkdir(exist_ok=True)
        image = Image.new("RGB", (1200, 180), "white")
        ImageDraw.Draw(image).text((30, 50), f"Synthetic OCR page {number}", font=font, fill="black")
        image.save(target)
        page_id = db.execute("INSERT INTO pages(source_file_id,page_number,status,rendered_path,image_sha256,created_at) VALUES(1,?,'pending_ocr',?,?,?)",
                             (number, f"pages/sourcehash/{number:04d}.png", sha(target), now)).lastrowid
        jobs.enqueue(db, f"ocr:{page_id}", "ocr_page", page_id, {"page_id": page_id})

root, action = Path(sys.argv[1]), sys.argv[2]
runtime = Runtime(Paths(root/"share/scanpadde",root/"data"))
runtime.start(background=False)
try:
    with connection(runtime.db_path) as db:
        if action in ("crash_copy", "crash_published"):
            os.environ["SCANPADDE_TEST_FAULT"] = (
                "crash:archive_copy_started" if action == "crash_copy"
                else "crash:archive_published"
            )
            ingestion.execute(db,runtime.paths,jobs.claim(db))
        elif action == "recover":
            while (job := jobs.claim(db)) is not None:
                ingestion.execute(db,runtime.paths,job)
        elif action == "wait":
            print("READY",flush=True)
            time.sleep(30)
        elif action == "ocr_kill":
            seed_ocr_pages(db, runtime.paths)
            for _ in range(2):
                ingestion.execute(db, runtime.paths, jobs.claim(db))
            os.environ["SCANPADDE_TEST_FAULT"] = "crash:ocr_page_running"
            ingestion.execute(db, runtime.paths, jobs.claim(db))
        elif action == "ocr_error":
            seed_ocr_pages(db, runtime.paths)
            first = jobs.claim(db)
            os.environ["SCANPADDE_TEST_FAULT"] = "error:ocr_page"
            ingestion.execute(db, runtime.paths, first)
            os.environ.pop("SCANPADDE_TEST_FAULT", None)
            while (job := jobs.claim(db)) is not None:
                ingestion.execute(db, runtime.paths, job)
        elif action == "ocr_retry":
            db.execute("UPDATE jobs SET state='pending',available_at=?,error_code=NULL,error_message=NULL WHERE state='failed' AND job_type='ocr_page'", (time.time(),))
            db.execute("UPDATE pages SET status='pending_ocr' WHERE status='ocr_failed'")
            while (job := jobs.claim(db)) is not None:
                ingestion.execute(db, runtime.paths, job)
finally:
    runtime.close()
