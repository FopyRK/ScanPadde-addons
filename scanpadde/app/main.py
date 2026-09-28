import asyncio
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager, closing
from html import escape
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from . import VERSION, jobs
from .db import connection, initialize, transaction
from .ingestion import observe, execute
from .paths import Paths
from .storage import cleanup_temps, process_lock, sha
from .config import load_ocr_config, load_ollama_config, load_drive_sync_config, _drive_remote
from .drive_sync import DriveSync
from .features import extract
from .ollama import OllamaClient, OllamaError, _input_digest
from .remote_ocr import RemoteOcrClient, RemoteOcrError
from .segmentation import reprocess_source, group_detail, override, apply_ollama_suggestion, _known_entities, group_display_name, duplicate_candidates
from .handoff import export_handoff, import_handoff
from .pdf_export import ExportError, materialize as materialize_pdf, recover as recover_exports

ASSETS = Path(__file__).parent

class Runtime:
    def __init__(self, paths, config_dir=Path("/config")):
        self.paths = paths
        self.config_dir = Path(config_dir)
        self.db_path = paths.data / "scanpadde.db"
        self.stop = threading.Event()
        self.worker = None
        self.state = "stopped"
        self.last_tick = None
        self.lock = None
        self.ocr_config = load_ocr_config(paths.data, self.config_dir)
        self.ollama_config = load_ollama_config(paths.data)
        self.drive_sync = DriveSync(load_drive_sync_config(paths.data, self.config_dir))
        self.remote_status = {"configured": False, "backend": "disabled", "online": None,
                              "worker_version": None, "tesseract_version": None, "languages": None,
                              "last_successful_contact": None}

    def start(self, background=True):
        self.paths.initialize()
        import_handoff(self.paths, self.paths.data)
        self.ocr_config = load_ocr_config(self.paths.data, self.config_dir)
        self.ollama_config = load_ollama_config(self.paths.data)
        self.drive_sync = DriveSync(load_drive_sync_config(self.paths.data, self.config_dir))
        self.lock = process_lock(self.paths.data)
        self.lock.__enter__()
        try:
            initialize(self.db_path)
            with connection(self.db_path) as db:
                jobs.recover(db)
                recover_exports(db, self.paths)
            cleanup_temps(self.paths)
            self.stop.clear()
            self.state = "idle"
            if background:
                self.worker = threading.Thread(target=self.loop, name="scanpadde-worker", daemon=False)
                self.worker.start()
        except BaseException:
            self.lock.__exit__(None, None, None)
            self.lock = None
            raise

    def loop(self):
        try:
            with connection(self.db_path) as db:
                while not self.stop.is_set():
                    try:
                        self.drive_sync.pull(db, self.paths)
                        observe(db, self.paths)
                        self.drive_sync.archive(db)
                        self.last_tick = time.time()
                        if self.stop.is_set():
                            break
                        job = jobs.claim(db)
                        if job:
                            self.state = "running"
                            execute(db, self.paths, job, self.ocr_config)
                        self.state = "idle"
                    except Exception as exc:
                        self.state = "error"
                        logging.getLogger("scanpadde").error(json.dumps(
                            {"event": "worker_error", "error_class": type(exc).__name__}))
                    self.stop.wait(2)
        finally:
            self.state = "stopped"

    def close(self):
        self.stop.set()
        if self.worker:
            # Keep process lock until worker is really gone. Supervisor may SIGKILL;
            # running job and hidden temp copy then recover on next start.
            self.worker.join()
        self.state = "stopped"
        if self.lock:
            self.lock.__exit__(None, None, None)
            self.lock = None

    def remote_ocr_status(self):
        result = {"configured": self.ocr_config.backend == "remote", "backend": self.ocr_config.backend,
                  "online": None, "worker_version": None, "tesseract_version": None, "languages": None,
                  "last_successful_contact": self.remote_status["last_successful_contact"]}
        if self.ocr_config.backend != "remote":
            self.remote_status = result
            return result
        try:
            worker = RemoteOcrClient(self.ocr_config.worker_url, self.ocr_config.worker_token, self.ocr_config.worker_ca,
                                     self.ocr_config.connect_timeout, self.ocr_config.read_timeout).version()
            result.update({"online": True, "worker_version": worker["schema_version"],
                           "tesseract_version": worker["engine_version"], "languages": worker.get("languages"),
                           "last_successful_contact": time.time()})
        except RemoteOcrError:
            result["online"] = False
        self.remote_status = result
        return result

def create_app(paths=None, background=True, allow_test_client=False, config_dir=Path("/config")):
    """Create the production app.

    ``allow_test_client`` is deliberately an in-process construction option,
    rather than an environment switch: the production entry point always keeps
    the Home-Assistant ingress peer check.  It exists solely for the isolated
    synthetic browser harness.
    """
    runtime = Runtime(paths or Paths(), config_dir)

    @asynccontextmanager
    async def lifespan(app):
        runtime.start(background)
        try:
            yield
        finally:
            await asyncio.to_thread(runtime.close)

    app = FastAPI(title="ScanPadde", version=VERSION, docs_url=None,
                  redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.runtime = runtime

    @app.middleware("http")
    async def ingress_only(request: Request, call_next):
        # Compare real TCP peer; uvicorn MUST use --no-proxy-headers.
        if not allow_test_client and (request.client is None or request.client.host != "172.30.32.2"):
            return JSONResponse({"error": "ingress_only"}, status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'self'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    def health():
        result = {"app": "running", "database": "ok", "workspace": "ok"}
        try:
            # mode=rw prevents a health probe from silently creating a missing DB.
            import sqlite3
            with closing(sqlite3.connect(runtime.db_path.as_uri() + "?mode=rw", uri=True)) as db:
                db.execute("SELECT count(*) FROM jobs").fetchone()
        except Exception:
            result["database"] = "unavailable"
        try:
            runtime.paths.check()
        except Exception:
            result["workspace"] = "unavailable"
        result["ok"] = result["database"] == result["workspace"] == "ok"
        return result

    def rows(query, params=()):
        with connection(runtime.db_path) as db:
            return [dict(r) for r in db.execute(query, params).fetchall()]

    @app.get("/api/health")
    def api_health():
        h = health()
        return JSONResponse(h, status_code=200 if h["ok"] else 503)

    @app.get("/api/status")
    def status():
        remote = runtime.remote_ocr_status()
        return {"version": VERSION, **health(), "worker": runtime.state,
                "worker_alive": bool(runtime.worker and runtime.worker.is_alive()),
                "last_tick": runtime.last_tick,
                "ollama": {"enabled": runtime.ollama_config.enabled,
                           "model": runtime.ollama_config.model},
                "drive_sync": {"enabled": runtime.drive_sync.configured,
                               "last_error": runtime.drive_sync.last_error,
                               "last_successful_sync": runtime.drive_sync.last_successful_sync},
                "remote_ocr": remote, "inbox": rows("SELECT state,count(*) AS count FROM inbox_entries GROUP BY state"),
                "jobs": rows("SELECT state,count(*) AS count FROM jobs GROUP BY state")}

    @app.post("/api/drive/config")
    async def upload_drive_config(request: Request):
        """Store only a validated rclone profile in this add-on's private config mount."""
        body = await request.body()
        if not body or len(body) > 256 * 1024 or b"\0" in body:
            raise HTTPException(422, "invalid_drive_config")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(422, "invalid_drive_config") from exc
        # The configured remote must be a Drive profile.  This is deliberately
        # narrow: the endpoint must not turn ScanPadde into a generic uploader.
        remote = None
        try:
            options = json.loads((runtime.paths.data / "options.json").read_text(encoding="utf-8"))
            remote = _drive_remote(options.get("drive_remote"))
        except (OSError, ValueError, AttributeError):
            pass
        if not remote or f"[{remote}]" not in text or "type = drive" not in text:
            raise HTTPException(422, "invalid_drive_config")
        runtime.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = runtime.config_dir / "rclone.conf"
        temporary = runtime.config_dir / ".rclone.conf.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
        finally:
            temporary.unlink(missing_ok=True)
        runtime.drive_sync = DriveSync(load_drive_sync_config(runtime.paths.data, runtime.config_dir))
        if not runtime.drive_sync.configured:
            raise HTTPException(409, "drive_not_enabled")
        return {"ok": True, "enabled": True}

    @app.get("/api/sources")
    def sources(limit: int = 100, offset: int = 0):
        return rows("SELECT * FROM source_files ORDER BY id DESC LIMIT ? OFFSET ?",
                    (max(1, min(limit, 500)), max(0, offset)))

    @app.get("/api/sources/{source_id}")
    def source(source_id: int):
        found = rows("SELECT * FROM source_files WHERE id=?", (source_id,))
        if not found:
            raise HTTPException(404, "source_not_found")
        return {**found[0], "pages": rows("SELECT * FROM pages WHERE source_file_id=? ORDER BY page_number", (source_id,))}

    @app.get("/api/sources/{source_id}/pages")
    def source_pages(source_id: int):
        if not rows("SELECT id FROM source_files WHERE id=?", (source_id,)):
            raise HTTPException(404, "source_not_found")
        return rows("SELECT * FROM pages WHERE source_file_id=? ORDER BY page_number", (source_id,))

    @app.post("/api/sources/{source_id}/analyze")
    def analyze_source(source_id: int):
        if not rows("SELECT id FROM source_files WHERE id=?", (source_id,)):
            raise HTTPException(404, "source_not_found")
        with connection(runtime.db_path) as db:
            return {"group_ids": reprocess_source(db, source_id)}

    def ollama_pages(db, source_id):
        page_rows = db.execute("""SELECT p.id,p.page_number,p.width,p.height,o.text,o.words_json
                             FROM pages p JOIN ocr_results o ON o.id=(SELECT id FROM ocr_results
                             WHERE page_id=p.id AND status='completed' ORDER BY created_at DESC LIMIT 1)
                             WHERE p.source_file_id=? ORDER BY p.page_number""", (source_id,)).fetchall()
        expected = db.execute("SELECT page_count FROM source_files WHERE id=?", (source_id,)).fetchone()
        if not expected:
            raise HTTPException(404, "source_not_found")
        if len(page_rows) != expected["page_count"]:
            raise HTTPException(409, "ocr_incomplete")
        known_entities = _known_entities(db)
        return [{"page_id": row["id"], "page_number": row["page_number"], "text": row["text"],
                 "features": extract(row["text"], row["words_json"], row["width"], row["height"], known_entities)} for row in page_rows]

    @app.get("/api/sources/{source_id}/ollama-suggestion")
    def ollama_suggestion(source_id: int):
        if not rows("SELECT id FROM source_files WHERE id=?", (source_id,)):
            raise HTTPException(404, "source_not_found")
        found = rows("SELECT model,input_digest,suggestion_json,created_at FROM ollama_grouping_suggestions WHERE source_file_id=?", (source_id,))
        if not found:
            return {"available": False}
        result = found[0]
        return {"available": True, "model": result["model"], "created_at": result["created_at"],
                "groups": json.loads(result["suggestion_json"])}

    @app.post("/api/sources/{source_id}/ollama-analyze")
    def ollama_analyze(source_id: int):
        if not runtime.ollama_config.enabled:
            raise HTTPException(409, "ollama_not_configured")
        with connection(runtime.db_path) as db:
            pages = ollama_pages(db, source_id)
            try:
                suggestion = OllamaClient(runtime.ollama_config).suggest_groups(pages)
            except OllamaError as exc:
                raise HTTPException(503 if str(exc) == "ollama_unavailable" else 422, str(exc))
            with transaction(db):
                db.execute("""INSERT INTO ollama_grouping_suggestions(source_file_id,model,input_digest,suggestion_json,created_at)
                              VALUES(?,?,?,?,?) ON CONFLICT(source_file_id) DO UPDATE SET model=excluded.model,
                              input_digest=excluded.input_digest,suggestion_json=excluded.suggestion_json,created_at=excluded.created_at""",
                           (source_id, runtime.ollama_config.model, suggestion.input_digest,
                            json.dumps(suggestion.groups), time.time()))
            return {"groups": suggestion.groups, "model": runtime.ollama_config.model,
                    "review_required": True}

    @app.post("/api/sources/{source_id}/apply-ollama-suggestion")
    def apply_saved_ollama_suggestion(source_id: int, payload: dict):
        if payload.get("confirm") is not True:
            raise HTTPException(422, "confirmation_required")
        with connection(runtime.db_path) as db:
            suggestion = db.execute("SELECT model,input_digest,suggestion_json FROM ollama_grouping_suggestions WHERE source_file_id=?", (source_id,)).fetchone()
            if not suggestion:
                raise HTTPException(404, "ollama_suggestion_not_found")
            pages = ollama_pages(db, source_id)
            if suggestion["input_digest"] != _input_digest(pages, suggestion["model"]):
                raise HTTPException(409, "ollama_suggestion_stale")
            try:
                group_ids = apply_ollama_suggestion(db, source_id, json.loads(suggestion["suggestion_json"]), pages,
                                                    suggestion["model"], suggestion["input_digest"])
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                raise HTTPException(422, str(exc))
        return {"group_ids": group_ids, "review_required": True}

    @app.post("/api/sources/{source_id}/retry-ocr")
    def retry_source_ocr(source_id: int):
        """Queue only pages missing a completed OCR cache entry.

        This is for sources that entered the archive before remote OCR was
        configured.  It deliberately refuses to use a local fallback and
        keeps existing OCR cache entries untouched.
        """
        if runtime.ocr_config.backend != "remote":
            raise HTTPException(409, "remote_ocr_required")
        with connection(runtime.db_path) as db:
            source = db.execute("SELECT id FROM source_files WHERE id=?", (source_id,)).fetchone()
            if not source:
                raise HTTPException(404, "source_not_found")
            pages = db.execute("SELECT id,rendered_path,image_sha256 FROM pages WHERE source_file_id=?", (source_id,)).fetchall()
            queued = 0
            for page in pages:
                cached = page["image_sha256"] and db.execute(
                    "SELECT 1 FROM ocr_results WHERE page_id=? AND image_sha256=? AND status='completed' LIMIT 1",
                    (page["id"], page["image_sha256"])).fetchone()
                if cached:
                    db.execute("UPDATE pages SET status='ocr_completed' WHERE id=?", (page["id"],))
                    continue
                if page["rendered_path"] and page["image_sha256"]:
                    jobs.enqueue(db, f"manual-ocr:{page['id']}:{page['image_sha256']}", "ocr_page", page["id"], {"page_id": page["id"]})
                else:
                    jobs.enqueue(db, f"manual-render:{page['id']}", "render_page", page["id"], {"page_id": page["id"]})
                queued += 1
            return {"queued_pages": queued, "backend": "remote"}

    @app.post("/api/sources/{source_id}/delete")
    def delete_source(source_id: int, payload: dict):
        """Permanently remove a source only when it has no approved documents.

        This deliberately cannot remove reviewed business documents. It is for
        discarded test scans and mistaken, unreviewed intake only. Paths come
        exclusively from database records and are guarded before unlinking.
        """
        if payload.get("confirm") is not True:
            raise HTTPException(422, "confirmation_required")
        files = []
        with connection(runtime.db_path) as db, transaction(db):
            source = db.execute("SELECT id,sha256,original_filename,archived_path FROM source_files WHERE id=?", (source_id,)).fetchone()
            if not source:
                raise HTTPException(404, "source_not_found")
            if db.execute("SELECT 1 FROM document_groups WHERE source_file_id=? AND status='approved' LIMIT 1", (source_id,)).fetchone():
                raise HTTPException(409, "approved_documents_protected")
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('pending','running') AND (subject_id=? OR subject_id IN (SELECT CAST(id AS TEXT) FROM pages WHERE source_file_id=?)) LIMIT 1", (str(source_id), source_id)).fetchone():
                raise HTTPException(409, "source_has_active_jobs")
            files.append(runtime.paths.guard(source["archived_path"]))
            page_ids = [row["id"] for row in db.execute("SELECT id FROM pages WHERE source_file_id=?", (source_id,)).fetchall()]
            for row in db.execute("SELECT rendered_path FROM pages WHERE source_file_id=? AND rendered_path IS NOT NULL", (source_id,)).fetchall():
                files.append(runtime.paths.guard(row["rendered_path"]))
            processed = runtime.paths.guard("processed")
            prefix = source["sha256"][:12] + "-"
            suffix = "-" + source["original_filename"]
            files.extend(path for path in processed.iterdir() if path.is_file() and path.name.startswith(prefix) and path.name.endswith(suffix))
            group_ids = [row["id"] for row in db.execute("SELECT id FROM document_groups WHERE source_file_id=?", (source_id,)).fetchall()]
            if group_ids:
                marks = ",".join("?" for _ in group_ids)
                db.execute(f"UPDATE learned_metadata_rules SET source_group_id=NULL WHERE source_group_id IN ({marks})", group_ids)
                db.execute(f"DELETE FROM group_pages WHERE group_id IN ({marks})", group_ids)
                db.execute(f"DELETE FROM document_groups WHERE id IN ({marks})", group_ids)
            if page_ids:
                marks = ",".join("?" for _ in page_ids)
                db.execute(f"DELETE FROM jobs WHERE subject_id IN ({marks})", [str(page_id) for page_id in page_ids])
                db.execute(f"DELETE FROM ocr_results WHERE page_id IN ({marks})", page_ids)
                db.execute(f"DELETE FROM page_features WHERE page_id IN ({marks})", page_ids)
            db.execute("DELETE FROM boundary_evidence WHERE source_file_id=?", (source_id,))
            db.execute("DELETE FROM group_overrides WHERE source_file_id=?", (source_id,))
            db.execute("DELETE FROM ollama_grouping_suggestions WHERE source_file_id=?", (source_id,))
            db.execute("UPDATE drive_entries SET source_file_id=NULL WHERE source_file_id=?", (source_id,))
            db.execute("DELETE FROM inbox_entries WHERE source_file_id=?", (source_id,))
            db.execute("DELETE FROM pages WHERE source_file_id=?", (source_id,))
            db.execute("DELETE FROM jobs WHERE subject_id=?", (str(source_id),))
            db.execute("DELETE FROM source_files WHERE id=?", (source_id,))
        removed = 0
        for path in files:
            if path.exists():
                path.unlink()
                removed += 1
        return {"ok": True, "removed_files": removed}

    @app.get("/api/sources/{source_id}/groups")
    def source_groups(source_id: int, include_hidden: bool = False):
        if not rows("SELECT id FROM source_files WHERE id=?", (source_id,)):
            raise HTTPException(404, "source_not_found")
        statuses = "('superseded')" if include_hidden else "('superseded','rejected')"
        return rows("""SELECT dg.* FROM document_groups dg LEFT JOIN group_pages gp ON gp.group_id=dg.id
                    LEFT JOIN pages p ON p.id=gp.page_id
                    WHERE dg.source_file_id=? AND dg.status NOT IN """ + statuses + """
                    GROUP BY dg.id
                    ORDER BY CASE WHEN dg.status='approved' THEN 1 ELSE 0 END, MIN(p.page_number), dg.id""", (source_id,))

    @app.get("/api/sources/{source_id}/duplicate-candidates")
    def source_duplicate_candidates(source_id: int):
        with connection(runtime.db_path) as db:
            if not db.execute("SELECT id FROM source_files WHERE id=?", (source_id,)).fetchone():
                raise HTTPException(404, "source_not_found")
            return duplicate_candidates(db, source_id)

    @app.get("/api/groups/{group_id}")
    def api_group(group_id: int):
        with connection(runtime.db_path) as db: found = group_detail(db, group_id)
        if not found: raise HTTPException(404, "group_not_found")
        return found

    @app.get("/api/groups/{group_id}/evidence")
    def group_evidence(group_id: int):
        with connection(runtime.db_path) as db:
            group = db.execute("SELECT source_file_id FROM document_groups WHERE id=?", (group_id,)).fetchone()
            if not group: raise HTTPException(404, "group_not_found")
            return rows("SELECT * FROM boundary_evidence WHERE source_file_id=? ORDER BY after_page_id", (group["source_file_id"],))

    def apply_group_action(source_id, action, payload):
        if not rows("SELECT id FROM source_files WHERE id=?", (source_id,)): raise HTTPException(404, "source_not_found")
        try:
            with connection(runtime.db_path) as db: override(db, source_id, action, payload)
        except (ValueError, KeyError) as exc: raise HTTPException(422, str(exc))
        return {"ok": True}

    @app.post("/api/sources/{source_id}/split")
    def split(source_id: int, payload: dict): return apply_group_action(source_id, "split", payload)
    @app.post("/api/sources/{source_id}/merge")
    def merge(source_id: int, payload: dict): return apply_group_action(source_id, "merge", payload)
    @app.post("/api/sources/{source_id}/move-page")
    def move_page(source_id: int, payload: dict): return apply_group_action(source_id, "move_page", payload)
    @app.post("/api/sources/{source_id}/reorder-page")
    def reorder_page(source_id: int, payload: dict): return apply_group_action(source_id, "reorder_page", payload)
    @app.post("/api/sources/{source_id}/exclude-page")
    def exclude_page(source_id: int, payload: dict): return apply_group_action(source_id, "exclude_page", payload)
    @app.post("/api/sources/{source_id}/hide-duplicate")
    def hide_duplicate(source_id: int, payload: dict): return apply_group_action(source_id, "hide_duplicate", payload)
    @app.post("/api/sources/{source_id}/restore-duplicate")
    def restore_duplicate(source_id: int, payload: dict): return apply_group_action(source_id, "restore_duplicate", payload)
    @app.patch("/api/sources/{source_id}/metadata")
    def metadata(source_id: int, payload: dict): return apply_group_action(source_id, "metadata", payload)
    @app.post("/api/sources/{source_id}/approve")
    def approve(source_id: int, payload: dict): return apply_group_action(source_id, "approve", payload)
    @app.post("/api/sources/{source_id}/needs-review")
    def needs_review(source_id: int, payload: dict): return apply_group_action(source_id, "needs_review", payload)

    @app.get("/api/groups/{group_id}/export")
    def group_export(group_id: int):
        with connection(runtime.db_path) as db:
            group = db.execute("SELECT revision,status FROM document_groups WHERE id=?", (group_id,)).fetchone()
            if not group:
                raise HTTPException(404, "group_not_found")
            export = db.execute("""SELECT * FROM document_exports WHERE group_id=? AND group_revision=?
                ORDER BY export_version DESC LIMIT 1""", (group_id, group["revision"])).fetchone()
            return {"group_status": group["status"], "export": dict(export) if export else None}

    @app.post("/api/groups/{group_id}/export")
    def create_group_export(group_id: int):
        try:
            with connection(runtime.db_path) as db:
                export, reused = materialize_pdf(db, runtime.paths, group_id)
            return {"export": export, "reused": reused}
        except ExportError as exc:
            code = str(exc)
            raise HTTPException(409 if code == "approved_group_required" else 422, code)

    @app.get("/api/exports/{export_id}/download")
    def download_export(export_id: int):
        with connection(runtime.db_path) as db:
            export = db.execute("SELECT * FROM document_exports WHERE id=?", (export_id,)).fetchone()
        if not export or export["status"] != "completed" or not export["output_path"]:
            raise HTTPException(404, "export_not_available")
        output = runtime.paths.guard(export["output_path"])
        if not output.is_file() or sha(output) != export["sha256"]:
            raise HTTPException(409, "export_integrity_failed")
        return FileResponse(output, media_type="application/pdf", filename=export["output_filename"])

    @app.get("/api/pages/{page_id}")
    def page(page_id: int):
        found = rows("SELECT * FROM pages WHERE id=?", (page_id,))
        if not found:
            raise HTTPException(404, "page_not_found")
        return found[0]

    @app.get("/api/pages/{page_id}/ocr")
    def page_ocr(page_id: int):
        if not rows("SELECT id FROM pages WHERE id=?", (page_id,)):
            raise HTTPException(404, "page_not_found")
        return rows("SELECT * FROM ocr_results WHERE page_id=? ORDER BY created_at DESC", (page_id,))

    @app.get("/api/pages/{page_id}/thumbnail")
    def page_thumbnail(page_id: int):
        found = rows("SELECT page_number,rendered_path FROM pages WHERE id=?", (page_id,))
        if not found:
            raise HTTPException(404, "page_not_found")
        page_row = found[0]
        if page_row["rendered_path"]:
            image = runtime.paths.guard(page_row["rendered_path"])
            if image.is_file():
                return FileResponse(image)
        # Fixture-only pages have no raster original.  A small labelled SVG is
        # still a real image and makes the review card usable in that case.
        svg = ("<svg xmlns='http://www.w3.org/2000/svg' width='160' height='220' "
               "viewBox='0 0 160 220'><rect width='160' height='220' fill='#fff' "
               "stroke='#9db5ac'/><text x='80' y='112' text-anchor='middle' "
               "font-family='sans-serif' font-size='18' fill='#18352e'>Seite "
               f"{page_row['page_number']}</text></svg>")
        return Response(svg, media_type="image/svg+xml")

    @app.get("/api/jobs")
    def job_list(limit: int = 100, offset: int = 0):
        return rows("SELECT * FROM jobs ORDER BY id DESC LIMIT ? OFFSET ?",
                    (max(1, min(limit, 500)), max(0, offset)))

    @app.get("/api/learning-library")
    def learning_library():
        """List only local learning metadata; no OCR or document text is returned."""
        return rows("""SELECT r.id,r.field,r.value,r.created_at,r.source_group_id,
                       CASE WHEN g.status='approved' THEN 1 ELSE 0 END AS source_is_approved
                       FROM learned_metadata_rules r
                       LEFT JOIN document_groups g ON g.id=r.source_group_id
                       ORDER BY r.field,r.value COLLATE NOCASE,r.id""")

    @app.delete("/api/learning-library/{rule_id}")
    def remove_learning_rule(rule_id: int):
        with connection(runtime.db_path) as db, transaction(db):
            rule = db.execute("SELECT id,field FROM learned_metadata_rules WHERE id=?", (rule_id,)).fetchone()
            if not rule:
                raise HTTPException(404, "learning_rule_not_found")
            db.execute("DELETE FROM learned_metadata_rules WHERE id=?", (rule_id,))
            db.execute("INSERT INTO learning_rule_events(action,rule_id,field,created_at) VALUES(?,?,?,?)",
                       ("removed", rule["id"], rule["field"], time.time()))
        return {"ok": True}

    @app.post("/api/learning-library/reset")
    def reset_learning_library(payload: dict):
        if payload.get("confirm") is not True:
            raise HTTPException(422, "confirmation_required")
        with connection(runtime.db_path) as db, transaction(db):
            count = db.execute("SELECT count(*) FROM learned_metadata_rules").fetchone()[0]
            db.execute("DELETE FROM learned_metadata_rules")
            db.execute("INSERT INTO learning_rule_events(action,rule_id,field,created_at) VALUES(?,?,?,?)",
                       ("reset", None, None, time.time()))
        return {"ok": True, "removed": count}

    @app.post("/api/migration/export")
    def migration_export():
        try:
            return export_handoff(runtime.paths, runtime.paths.data)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))

    @app.get("/static/style.css")
    def style():
        return FileResponse(ASSETS / "static/style.css", media_type="text/css")

    @app.get("/static/review.js")
    def review_js(): return FileResponse(ASSETS / "static/review.js", media_type="application/javascript")

    @app.get("/static/ollama.js")
    def ollama_js(): return FileResponse(ASSETS / "static/ollama.js", media_type="application/javascript")

    @app.get("/static/learning.js")
    def learning_js(): return FileResponse(ASSETS / "static/learning.js", media_type="application/javascript")

    @app.get("/static/source-delete.js")
    def source_delete_js(): return FileResponse(ASSETS / "static/source-delete.js", media_type="application/javascript")

    @app.get("/static/drive-setup.js")
    def drive_setup_js(): return FileResponse(ASSETS / "static/drive-setup.js", media_type="application/javascript")

    @app.get("/review/{source_id}", response_class=HTMLResponse)
    def review(source_id: int):
        if not rows("SELECT id FROM source_files WHERE id=?", (source_id,)): raise HTTPException(404, "source_not_found")
        template=(ASSETS / "templates/review.html").read_text(encoding="utf-8")
        return template.replace("{{source_id}}", str(source_id))

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        s = status()
        counts = {r["state"]: r["count"] for r in s["inbox"]}
        queue = {r["state"]: r["count"] for r in s["jobs"]}
        ocr_done = {r["source_file_id"]: r["count"] for r in rows(
            "SELECT source_file_id,count(*) AS count FROM pages WHERE status='ocr_completed' GROUP BY source_file_id")}
        group_rows = rows("""SELECT source_file_id,status,metadata_json FROM document_groups
                           WHERE status NOT IN ('superseded','rejected') ORDER BY source_file_id,id""")
        review_summary = {}
        for group in group_rows:
            metadata = json.loads(group["metadata_json"] or "{}")
            review_summary.setdefault(group["source_file_id"], []).append({
                "status": group["status"], "name": group_display_name(metadata)})
        def review_cell(source_id):
            groups = review_summary.get(source_id, [])
            if not groups:
                return "<span>noch nicht analysiert</span>"
            approved = sum(group["status"] == "approved" for group in groups)
            open_groups = len(groups) - approved
            status = f"{approved} von {len(groups)} freigegeben" if not open_groups else f"{approved} freigegeben · {open_groups} offen"
            names = "".join(f"<li>{escape(group['name'])} · {escape('freigegeben' if group['status'] == 'approved' else 'offen')}</li>" for group in groups)
            return f"<strong>{escape(status)}</strong><details><summary>{len(groups)} Dokumente anzeigen</summary><ul>{names}</ul></details>"
        def source_row(source):
            captured = __import__("datetime").datetime.fromtimestamp(source["first_seen_at"], __import__("datetime").timezone.utc).isoformat()
            retry = (f'<form action="api/sources/{source["id"]}/retry-ocr" method="post"><button type="submit">OCR nachholen</button></form> '
                     if ocr_done.get(source["id"], 0) < source["page_count"] else "")
            has_approved = any(group["status"] == "approved" for group in review_summary.get(source["id"], []))
            delete = ("" if has_approved else f'<button class="source-delete" type="button" data-source-id="{source["id"]}" data-source-name="{escape(source["original_filename"], quote=True)}">Testquelle löschen</button> ')
            return (f"<tr><td>{source['id']}</td><td>{escape(source['original_filename'])}</td>"
                    f"<td>{review_cell(source['id'])}</td><td>{escape(source['sha256'][:12])}</td>"
                    f"<td>{source['page_count']}</td><td>{escape(source['status'])}</td>"
                    f"<td>OCR {ocr_done.get(source['id'], 0)} / {source['page_count']}</td><td>{escape(captured)}</td>"
                    f"<td>{retry}{delete}<a href=\"review/{source['id']}\">Analyse &amp; Review</a></td></tr>")
        table = "".join(source_row(source) for source in sources())
        template = (ASSETS / "templates/index.html").read_text(encoding="utf-8")
        return template.replace("{{version}}", VERSION).replace("{{health}}", escape(
            f"System: {s['app']} | Datenbank: {s['database']} | Arbeitsverzeichnis: {s['workspace']} | Worker: {s['worker']} | Remote OCR: {s['remote_ocr']['backend']} / {s['remote_ocr']['online']}")).replace(
            "{{inbox}}", escape(f"Erkannt: {sum(counts.values())} | Wartet auf Stabilität: {counts.get('waiting',0)} | Verarbeitet: {counts.get('processed',0)} | Lokal archiviert: {counts.get('archived',0)} | Fehler: {counts.get('error',0)}")).replace(
            "{{jobs}}", escape(" | ".join(f"{k}: {queue.get(k,0)}" for k in ("pending","running","completed","failed")))).replace("{{sources}}", table)

    return app
