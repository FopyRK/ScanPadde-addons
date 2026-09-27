import asyncio
import json
import logging
import threading
import time
from contextlib import asynccontextmanager, closing
from html import escape
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from . import VERSION, jobs
from .db import connection, initialize
from .ingestion import observe, execute
from .paths import Paths
from .storage import cleanup_temps, process_lock
from .config import load_ocr_config
from .remote_ocr import RemoteOcrClient, RemoteOcrError
from .segmentation import reprocess_source, group_detail, override

ASSETS = Path(__file__).parent

class Runtime:
    def __init__(self, paths):
        self.paths = paths
        self.db_path = paths.data / "scanpadde.db"
        self.stop = threading.Event()
        self.worker = None
        self.state = "stopped"
        self.last_tick = None
        self.lock = None
        self.ocr_config = load_ocr_config(paths.data)
        self.remote_status = {"configured": False, "backend": "disabled", "online": None,
                              "worker_version": None, "tesseract_version": None, "languages": None,
                              "last_successful_contact": None}

    def start(self, background=True):
        self.paths.initialize()
        self.ocr_config = load_ocr_config(self.paths.data)
        self.lock = process_lock(self.paths.data)
        self.lock.__enter__()
        try:
            initialize(self.db_path)
            with connection(self.db_path) as db:
                jobs.recover(db)
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
                        observe(db, self.paths)
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

def create_app(paths=None, background=True, allow_test_client=False):
    """Create the production app.

    ``allow_test_client`` is deliberately an in-process construction option,
    rather than an environment switch: the production entry point always keeps
    the Home-Assistant ingress peer check.  It exists solely for the isolated
    synthetic browser harness.
    """
    runtime = Runtime(paths or Paths())

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
                "remote_ocr": remote, "inbox": rows("SELECT state,count(*) AS count FROM inbox_entries GROUP BY state"),
                "jobs": rows("SELECT state,count(*) AS count FROM jobs GROUP BY state")}

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

    @app.get("/api/sources/{source_id}/groups")
    def source_groups(source_id: int):
        if not rows("SELECT id FROM source_files WHERE id=?", (source_id,)):
            raise HTTPException(404, "source_not_found")
        return rows("""SELECT dg.* FROM document_groups dg LEFT JOIN group_pages gp ON gp.group_id=dg.id
                    LEFT JOIN pages p ON p.id=gp.page_id
                    WHERE dg.source_file_id=? AND dg.status != 'superseded'
                    GROUP BY dg.id ORDER BY MIN(p.page_number), dg.id""", (source_id,))

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
    @app.patch("/api/sources/{source_id}/metadata")
    def metadata(source_id: int, payload: dict): return apply_group_action(source_id, "metadata", payload)
    @app.post("/api/sources/{source_id}/approve")
    def approve(source_id: int, payload: dict): return apply_group_action(source_id, "approve", payload)
    @app.post("/api/sources/{source_id}/needs-review")
    def needs_review(source_id: int, payload: dict): return apply_group_action(source_id, "needs_review", payload)

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

    @app.get("/static/style.css")
    def style():
        return FileResponse(ASSETS / "static/style.css", media_type="text/css")

    @app.get("/static/review.js")
    def review_js(): return FileResponse(ASSETS / "static/review.js", media_type="application/javascript")

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
        table = "".join("<tr>" + "".join("<td>" + escape(str(v)) + "</td>" for v in (
            r["id"], r["original_filename"], r["sha256"][:12], r["page_count"],
            r["status"], f"OCR {ocr_done.get(r['id'], 0)} / {r['page_count']}", __import__("datetime").datetime.fromtimestamp(r["first_seen_at"],
            __import__("datetime").timezone.utc).isoformat())) +
            f'<td><a href="review/{r["id"]}">Analyse &amp; Review</a></td></tr>' for r in sources())
        template = (ASSETS / "templates/index.html").read_text(encoding="utf-8")
        return template.replace("{{version}}", VERSION).replace("{{health}}", escape(
            f"System: {s['app']} | Datenbank: {s['database']} | Arbeitsverzeichnis: {s['workspace']} | Worker: {s['worker']} | Remote OCR: {s['remote_ocr']['backend']} / {s['remote_ocr']['online']}")).replace(
            "{{inbox}}", escape(f"Erkannt: {sum(counts.values())} | Wartet auf Stabilität: {counts.get('waiting',0)} | Verarbeitet: {counts.get('processed',0)} | Fehler: {counts.get('error',0)}")).replace(
            "{{jobs}}", escape(" | ".join(f"{k}: {queue.get(k,0)}" for k in ("pending","running","completed","failed")))).replace("{{sources}}", table)

    return app
