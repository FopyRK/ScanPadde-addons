import json
import time
from unittest.mock import patch

from PIL import Image

from app import jobs
from app.ingestion import execute
from app.ocr import recognize


def page_with_image(paths, db):
    original = paths.guard("originals/source")
    Image.new("RGB", (100, 50), "white").save(original, "PNG")
    now = time.time()
    db.execute("""INSERT INTO source_files(sha256,original_filename,archived_path,mime_type,size_bytes,page_count,status,first_seen_at,last_seen_at,archived_at)
        VALUES ('sourcehash','scan.png','originals/source','image/png',1,1,'ready',?,?,?)""", (now, now, now))
    page_id = db.execute("INSERT INTO pages(source_file_id,page_number,width,height,status,created_at) VALUES(1,1,100,50,'pending_ocr',?)", (now,)).lastrowid
    image = paths.guard("pages/sourcehash/0001.png")
    image.parent.mkdir(exist_ok=True)
    Image.new("RGB", (100, 50), "white").save(image, "PNG")
    from app.storage import sha
    db.execute("UPDATE pages SET rendered_path=?,image_sha256=? WHERE id=?", ("pages/sourcehash/0001.png", sha(image), page_id))
    return page_id


def test_ocr_cache_prevents_second_engine_run(env):
    paths, db = env
    page_id = page_with_image(paths, db)
    jobs.enqueue(db, "ocr-one", "ocr_page", page_id, {"page_id": page_id})
    with patch("app.ingestion.engine_version", return_value="tesseract 1"), patch("app.ingestion.recognize", return_value=("Hallo", '[{"text":"Hallo"}]', 91.0, 0)) as ocr:
        execute(db, paths, jobs.claim(db))
        jobs.enqueue(db, "ocr-two", "ocr_page", page_id, {"page_id": page_id})
        execute(db, paths, jobs.claim(db))
    assert ocr.call_count == 1
    assert db.execute("SELECT count(*) FROM ocr_results").fetchone()[0] == 1
    assert db.execute("SELECT status FROM pages WHERE id=?", (page_id,)).fetchone()[0] == "ocr_completed"


def test_embedded_text_completes_without_ocr_or_confidence(env):
    paths, db = env
    page_id = page_with_image(paths, db)
    db.execute("UPDATE source_files SET original_filename='text.pdf'")
    db.execute("UPDATE pages SET status='pending_render' WHERE id=?", (page_id,))
    jobs.enqueue(db, "render-embedded", "render_page", page_id, {"page_id": page_id})
    with patch("app.ingestion.embedded_text", return_value="Already embedded") as embedded, patch("app.ingestion.render") as renderer, patch("app.ingestion.recognize") as ocr:
        execute(db, paths, jobs.claim(db))
    assert embedded.called and not renderer.called and not ocr.called
    result = db.execute("SELECT source_type,text,confidence,words_json FROM ocr_results").fetchone()
    assert tuple(result) == ("embedded_text", "Already embedded", None, None)


def test_restart_requeues_only_interrupted_ocr(env):
    _, db = env
    now = time.time()
    db.execute("""INSERT INTO source_files(sha256,original_filename,archived_path,mime_type,size_bytes,page_count,status,first_seen_at,last_seen_at,archived_at)
        VALUES ('s','x.png','originals/s','image/png',1,2,'ready',?,?,?)""", (now, now, now))
    db.execute("INSERT INTO pages(source_file_id,page_number,status,created_at) VALUES(1,1,'ocr_completed',?)", (now,))
    second = db.execute("INSERT INTO pages(source_file_id,page_number,status,created_at) VALUES(1,2,'ocr_running',?)", (now,)).lastrowid
    jobs.enqueue(db, "interrupted", "ocr_page", second, {"page_id": second})
    jobs.claim(db)
    jobs.recover(db)
    assert [r[0] for r in db.execute("SELECT status FROM pages ORDER BY page_number")] == ["ocr_completed", "pending_ocr"]
    assert db.execute("SELECT state FROM jobs WHERE job_key='interrupted'").fetchone()[0] == "pending"


def test_real_tsv_has_boxes_and_noninvented_confidence(tmp_path):
    from PIL import ImageDraw, ImageFont
    image = tmp_path / "words.png"
    picture = Image.new("RGB", (900, 180), "white")
    ImageDraw.Draw(picture).text((30, 30), "Hello ScanPadde", fill="black", font=ImageFont.load_default(size=72))
    picture.save(image)
    text, words, confidence, rotation = recognize(image, "eng")
    assert text
    assert json.loads(words)[0]["bbox"]["width"] > 0
    assert confidence is not None and 0 <= confidence <= 100
    assert rotation in (0, 90, 180, 270)
