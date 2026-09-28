"""Approved-review PDF materialisation.  It never writes source artefacts."""
import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from .db import transaction
from .storage import sha, sync_directory

EXPORT_VERSION = 1


class ExportError(ValueError):
    pass


def _value(metadata, field, fallback):
    item = metadata.get(field) or {}
    value = item.get("human_value") or item.get("effective_value") or item.get("auto_value") or {}
    return str(value.get("value") or fallback)


def safe_filename(metadata, group_id):
    """Stable cross-platform file name; metadata is a snapshot, never a path."""
    date = _value(metadata, "invoice_date", "UNDATIERT")
    supplier = _value(metadata, "supplier", "UNBEKANNT")
    number = _value(metadata, "invoice_number", "OHNE-NUMMER")
    kind = _value(metadata, "document_type", "Rechnung")
    # OCR dates are commonly DD.MM.YYYY.  Keep ISO dates when they are certain.
    match = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", date.strip())
    if match:
        date = f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
    def clean(value):
        value = unicodedata.normalize("NFKC", value)
        value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", value)
        value = re.sub(r"\s+", "_", value.strip())
        value = re.sub(r"_+", "_", value).strip("._ ")
        return value or "UNBEKANNT"
    stem = "_".join(clean(value) for value in (date, supplier, number, kind))
    stem = stem[:180].rstrip("._ ") or f"Dokument_{group_id}"
    return stem + ".pdf"


def _collision_name(directory, filename, group_id):
    target = directory / filename
    if not target.exists():
        return filename
    suffix = hashlib.sha256(str(group_id).encode()).hexdigest()[:6]
    stem = Path(filename).stem[:(180 - len(suffix) - 1)].rstrip("._ ")
    candidate = f"{stem}_{suffix}.pdf"
    # A completed identical export is handled before this function.  This loop
    # only protects unrelated groups from a pathological hash/name collision.
    counter = 2
    while (directory / candidate).exists():
        candidate = f"{stem}_{suffix}_{counter}.pdf"
        counter += 1
    return candidate


def _group(db, group_id):
    group = db.execute("SELECT * FROM document_groups WHERE id=?", (group_id,)).fetchone()
    if not group:
        raise ExportError("group_not_found")
    if group["status"] != "approved":
        raise ExportError("approved_group_required")
    pages = db.execute("""SELECT gp.page_id,gp.sequence,p.page_number,s.archived_path,s.mime_type,s.original_filename
        FROM group_pages gp JOIN pages p ON p.id=gp.page_id JOIN source_files s ON s.id=p.source_file_id
        WHERE gp.group_id=? ORDER BY gp.sequence""", (group_id,)).fetchall()
    if not pages:
        raise ExportError("group_has_no_pages")
    return group, pages


def _copy_pages(paths, pages, destination):
    writer = PdfWriter()
    readers = {}
    temporary_inputs = []
    try:
        for page in pages:
            source = paths.guard(page["archived_path"])
            if page["mime_type"] == "application/pdf":
                key = str(source)
                readers.setdefault(key, PdfReader(source, strict=True))
                writer.add_page(readers[key].pages[page["page_number"] - 1])
            else:
                # Ingestion already created an exact rendered page for image input.
                rendered = paths.guard(f"pages/{Path(page['archived_path']).name}/{page['page_number']:04d}.png")
                if not rendered.is_file():
                    raise ExportError("rendered_page_unavailable")
                image_pdf = Path(tempfile.mkstemp(prefix=".export-image-", suffix=".pdf", dir=destination.parent)[1])
                temporary_inputs.append(image_pdf)
                from PIL import Image
                with Image.open(rendered) as image:
                    image.convert("RGB").save(image_pdf, "PDF", resolution=150.0)
                writer.add_page(PdfReader(image_pdf, strict=True).pages[0])
        with open(destination, "wb") as output:
            writer.write(output)
            output.flush()
            os.fsync(output.fileno())
    finally:
        for path in temporary_inputs:
            path.unlink(missing_ok=True)


def materialize(db, paths, group_id):
    """Create or reuse exactly one export for an approved immutable revision."""
    group, pages = _group(db, group_id)
    existing = db.execute("""SELECT * FROM document_exports WHERE group_id=? AND group_revision=?
        AND export_version=?""", (group_id, group["revision"], EXPORT_VERSION)).fetchone()
    if existing and existing["status"] == "completed":
        output = paths.guard(existing["output_path"])
        if output.is_file() and sha(output) == existing["sha256"]:
            return dict(existing), True
    metadata = json.loads(group["metadata_json"] or "{}")
    page_ids = [row["page_id"] for row in pages]
    now = time.time()
    with transaction(db):
        if existing:
            db.execute("UPDATE document_exports SET status='building',error=NULL WHERE id=?", (existing["id"],))
            export_id = existing["id"]
        else:
            export_id = db.execute("""INSERT INTO document_exports(group_id,group_revision,export_version,status,created_at,
                source_page_ids_json,metadata_snapshot_json) VALUES(?,?,?,?,?,?,?)""",
                (group_id, group["revision"], EXPORT_VERSION, "building", now, json.dumps(page_ids), json.dumps(metadata))).lastrowid
    ready = paths.guard("export/ready")
    ready.mkdir(parents=True, exist_ok=True)
    filename = _collision_name(ready, safe_filename(metadata, group_id), group_id)
    fd, name = tempfile.mkstemp(prefix=".export-", suffix=".pdf", dir=ready)
    os.close(fd)
    temporary = paths.guard(name)
    try:
        _copy_pages(paths, pages, temporary)
        reader = PdfReader(temporary, strict=True)
        if len(reader.pages) != len(pages) or temporary.stat().st_size <= 0:
            raise ExportError("pdf_validation_failed")
        digest = sha(temporary)
        final = paths.guard(ready / filename)
        os.replace(temporary, final)
        sync_directory(ready)
        with transaction(db):
            db.execute("""UPDATE document_exports SET status='completed',completed_at=?,output_filename=?,output_path=?,
                sha256=?,page_count=?,error=NULL WHERE id=?""",
                (time.time(), filename, final.relative_to(paths.root).as_posix(), digest, len(pages), export_id))
        return dict(db.execute("SELECT * FROM document_exports WHERE id=?", (export_id,)).fetchone()), False
    except Exception as exc:
        code = str(exc) if isinstance(exc, ExportError) else "pdf_build_failed"
        with transaction(db):
            db.execute("UPDATE document_exports SET status='failed',error=? WHERE id=?", (code, export_id))
        raise ExportError(code) from None
    finally:
        temporary.unlink(missing_ok=True)


def supersede_group_exports(db, group_id):
    """Retain audit records but remove obsolete files from the ready area."""
    rows = db.execute("SELECT id,output_path FROM document_exports WHERE group_id=? AND status='completed'", (group_id,)).fetchall()
    if rows:
        db.execute("UPDATE document_exports SET status='superseded' WHERE group_id=? AND status='completed'", (group_id,))


def recover(db, paths):
    """A crash leaves no ready file: unfinished records are safely retryable."""
    with transaction(db):
        db.execute("""UPDATE document_exports SET status='failed',error='recovered_after_stop'
                    WHERE status IN ('pending','building')""")
    ready = paths.guard("export/ready")
    if ready.exists():
        for temporary in ready.glob(".export-*.pdf"):
            paths.guard(temporary).unlink(missing_ok=True)
