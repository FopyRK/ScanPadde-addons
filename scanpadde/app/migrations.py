"""Versioned schema. All times are UTC Unix seconds."""
SCHEMA = """
CREATE TABLE source_files (
 id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE,
 original_filename TEXT NOT NULL, archived_path TEXT NOT NULL,
 mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 page_count INTEGER NOT NULL, status TEXT NOT NULL,
 first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL, archived_at REAL NOT NULL,
 error_code TEXT, error_message TEXT
);
CREATE TABLE pages (
 id INTEGER PRIMARY KEY, source_file_id INTEGER NOT NULL REFERENCES source_files(id),
 page_number INTEGER NOT NULL CHECK(page_number>0), width REAL, height REAL,
 status TEXT NOT NULL DEFAULT 'pending_render' CHECK(status IN ('pending_render','rendered','pending_ocr','ocr_running','ocr_completed','ocr_failed')),
 rendered_path TEXT, image_sha256 TEXT, rotation INTEGER,
 created_at REAL NOT NULL, UNIQUE(source_file_id,page_number)
);
CREATE TABLE ocr_results (
 id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL REFERENCES pages(id),
 engine TEXT NOT NULL, engine_version TEXT NOT NULL, language TEXT NOT NULL,
 preprocessing_version TEXT NOT NULL, rotation INTEGER NOT NULL DEFAULT 0,
 source_type TEXT NOT NULL CHECK(source_type IN ('ocr','embedded_text')),
 text TEXT NOT NULL, words_json TEXT, confidence REAL, image_sha256 TEXT,
 status TEXT NOT NULL CHECK(status IN ('completed','failed')), created_at REAL NOT NULL,
 UNIQUE(page_id,image_sha256,engine,engine_version,language,preprocessing_version,rotation)
);
CREATE INDEX ocr_results_page ON ocr_results(page_id, created_at DESC);
CREATE TABLE jobs (
 id INTEGER PRIMARY KEY, job_key TEXT NOT NULL UNIQUE,
 job_type TEXT NOT NULL CHECK(job_type IN ('ingest_file','enumerate_pages','render_page','ocr_page')),
 subject_type TEXT NOT NULL, subject_id TEXT, payload_json TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','running','completed','failed','paused','cancelled')),
 priority INTEGER NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL DEFAULT 3, available_at REAL NOT NULL,
 started_at REAL, heartbeat_at REAL, finished_at REAL,
 error_code TEXT, error_message TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX one_running_job ON jobs(state) WHERE state='running';
CREATE INDEX ready_jobs ON jobs(state,available_at,priority);
CREATE TABLE inbox_entries (
 relative_path TEXT PRIMARY KEY, size_bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
 first_seen_at REAL NOT NULL, stable_since REAL NOT NULL, last_seen_at REAL NOT NULL,
 state TEXT NOT NULL, source_file_id INTEGER REFERENCES source_files(id),
 last_error TEXT, generation INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE page_features (page_id INTEGER PRIMARY KEY REFERENCES pages(id), feature_version TEXT NOT NULL, data_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE document_groups (id INTEGER PRIMARY KEY, source_file_id INTEGER NOT NULL REFERENCES source_files(id), revision INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('proposed','review_required','approved','rejected','superseded')), algorithm_version TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', parent_group_id INTEGER REFERENCES document_groups(id), created_at REAL NOT NULL, UNIQUE(source_file_id, revision, id));
CREATE TABLE group_pages (group_id INTEGER NOT NULL REFERENCES document_groups(id), page_id INTEGER NOT NULL REFERENCES pages(id), sequence INTEGER NOT NULL CHECK(sequence > 0), PRIMARY KEY(group_id,page_id), UNIQUE(group_id,sequence));
CREATE TABLE boundary_evidence (source_file_id INTEGER NOT NULL REFERENCES source_files(id), after_page_id INTEGER NOT NULL REFERENCES pages(id), algorithm_version TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(source_file_id,after_page_id,algorithm_version));
CREATE TABLE group_overrides (id INTEGER PRIMARY KEY, source_file_id INTEGER NOT NULL REFERENCES source_files(id), action TEXT NOT NULL CHECK(action IN ('split','merge','move_page','reorder_page','exclude_page','metadata','approve','needs_review','apply_ollama_suggestion')), payload_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX group_pages_page ON group_pages(page_id);
CREATE INDEX groups_source ON document_groups(source_file_id,revision DESC);
CREATE TABLE ollama_grouping_suggestions (
 source_file_id INTEGER PRIMARY KEY REFERENCES source_files(id),
 model TEXT NOT NULL, input_digest TEXT NOT NULL, suggestion_json TEXT NOT NULL,
 created_at REAL NOT NULL
);
CREATE TABLE learned_metadata_rules (
 id INTEGER PRIMARY KEY,
 field TEXT NOT NULL CHECK(field IN ('supplier','invoice_label')),
 normalized TEXT NOT NULL,
 value TEXT NOT NULL,
 source_group_id INTEGER REFERENCES document_groups(id),
 created_at REAL NOT NULL,
 UNIQUE(field, normalized)
);
CREATE TABLE drive_entries (
 remote_path TEXT PRIMARY KEY,
 remote_id TEXT NOT NULL,
 local_relative_path TEXT NOT NULL UNIQUE,
 processed_remote_path TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL CHECK(state IN ('downloaded','moved','error')),
 source_file_id INTEGER REFERENCES source_files(id),
 last_error TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX drive_entries_source ON drive_entries(source_file_id,state);
CREATE TABLE learning_rule_events (
 id INTEGER PRIMARY KEY,
 action TEXT NOT NULL CHECK(action IN ('removed','reset')),
 rule_id INTEGER,
 field TEXT,
 created_at REAL NOT NULL
);
PRAGMA user_version=9;
"""

def migrate(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > 9:
        raise RuntimeError("future_schema_rejected")
    if version == 0:
        conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
    elif version == 1:
        conn.executescript("""BEGIN IMMEDIATE;
        ALTER TABLE pages ADD COLUMN status TEXT NOT NULL DEFAULT 'pending_render'
          CHECK(status IN ('pending_render','rendered','pending_ocr','ocr_running','ocr_completed','ocr_failed'));
        ALTER TABLE pages ADD COLUMN rendered_path TEXT;
        ALTER TABLE pages ADD COLUMN image_sha256 TEXT;
        ALTER TABLE pages ADD COLUMN rotation INTEGER;
        CREATE TABLE ocr_results (
          id INTEGER PRIMARY KEY,
          page_id INTEGER NOT NULL REFERENCES pages(id),
          engine TEXT NOT NULL, engine_version TEXT NOT NULL, language TEXT NOT NULL,
          preprocessing_version TEXT NOT NULL, rotation INTEGER NOT NULL DEFAULT 0,
          source_type TEXT NOT NULL CHECK(source_type IN ('ocr','embedded_text')),
          text TEXT NOT NULL, words_json TEXT, confidence REAL,
          image_sha256 TEXT, status TEXT NOT NULL CHECK(status IN ('completed','failed')),
          created_at REAL NOT NULL,
          UNIQUE(page_id,image_sha256,engine,engine_version,language,preprocessing_version,rotation)
        );
        CREATE INDEX ocr_results_page ON ocr_results(page_id, created_at DESC);
        ALTER TABLE jobs RENAME TO jobs_old;
        CREATE TABLE jobs (
         id INTEGER PRIMARY KEY, job_key TEXT NOT NULL UNIQUE,
         job_type TEXT NOT NULL CHECK(job_type IN ('ingest_file','enumerate_pages','render_page','ocr_page')),
         subject_type TEXT NOT NULL, subject_id TEXT, payload_json TEXT NOT NULL,
         state TEXT NOT NULL CHECK(state IN ('pending','running','completed','failed','paused','cancelled')),
         priority INTEGER NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 0,
         max_attempts INTEGER NOT NULL DEFAULT 3, available_at REAL NOT NULL,
         started_at REAL, heartbeat_at REAL, finished_at REAL,
         error_code TEXT, error_message TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        INSERT INTO jobs SELECT * FROM jobs_old;
        DROP TABLE jobs_old;
        CREATE UNIQUE INDEX one_running_job ON jobs(state) WHERE state='running';
        CREATE INDEX ready_jobs ON jobs(state,available_at,priority);
        PRAGMA user_version=2;
        COMMIT;""")
    elif version == 2:
        conn.executescript("""BEGIN IMMEDIATE;
        CREATE TABLE page_features (
          page_id INTEGER PRIMARY KEY REFERENCES pages(id), feature_version TEXT NOT NULL,
          data_json TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE document_groups (
          id INTEGER PRIMARY KEY, source_file_id INTEGER NOT NULL REFERENCES source_files(id),
          revision INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('proposed','review_required','approved','rejected','superseded')),
          algorithm_version TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
          parent_group_id INTEGER REFERENCES document_groups(id), created_at REAL NOT NULL,
          UNIQUE(source_file_id, revision, id)
        );
        CREATE TABLE group_pages (
          group_id INTEGER NOT NULL REFERENCES document_groups(id), page_id INTEGER NOT NULL REFERENCES pages(id),
          sequence INTEGER NOT NULL CHECK(sequence > 0), PRIMARY KEY(group_id, page_id), UNIQUE(group_id, sequence)
        );
        CREATE TABLE boundary_evidence (
          source_file_id INTEGER NOT NULL REFERENCES source_files(id), after_page_id INTEGER NOT NULL REFERENCES pages(id),
          algorithm_version TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at REAL NOT NULL,
          PRIMARY KEY(source_file_id, after_page_id, algorithm_version)
        );
        CREATE TABLE group_overrides (
          id INTEGER PRIMARY KEY, source_file_id INTEGER NOT NULL REFERENCES source_files(id),
          action TEXT NOT NULL CHECK(action IN ('split','merge','move_page','metadata','approve','needs_review','apply_ollama_suggestion')),
          payload_json TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE INDEX group_pages_page ON group_pages(page_id);
        CREATE INDEX groups_source ON document_groups(source_file_id, revision DESC);
        PRAGMA user_version=3;
        COMMIT;""")
    elif version == 3:
        conn.executescript("""BEGIN IMMEDIATE;
        CREATE TABLE ollama_grouping_suggestions (
          source_file_id INTEGER PRIMARY KEY REFERENCES source_files(id),
          model TEXT NOT NULL, input_digest TEXT NOT NULL, suggestion_json TEXT NOT NULL,
          created_at REAL NOT NULL
        );
        PRAGMA user_version=4;
        COMMIT;""")
    elif version == 4:
        # SQLite cannot extend a CHECK constraint in place.  Keep the complete
        # local audit log while allowing a deliberately confirmed KI proposal
        # to be recorded as a distinct review action.
        conn.executescript("""BEGIN IMMEDIATE;
        ALTER TABLE group_overrides RENAME TO group_overrides_old;
        CREATE TABLE group_overrides (
          id INTEGER PRIMARY KEY, source_file_id INTEGER NOT NULL REFERENCES source_files(id),
          action TEXT NOT NULL CHECK(action IN ('split','merge','move_page','metadata','approve','needs_review','apply_ollama_suggestion')),
          payload_json TEXT NOT NULL, created_at REAL NOT NULL
        );
        INSERT INTO group_overrides SELECT * FROM group_overrides_old;
        DROP TABLE group_overrides_old;
        PRAGMA user_version=5;
        COMMIT;""")
    elif version == 5:
        conn.executescript("""BEGIN IMMEDIATE;
        CREATE TABLE learned_metadata_rules (
          id INTEGER PRIMARY KEY,
          field TEXT NOT NULL CHECK(field IN ('supplier','invoice_label')),
          normalized TEXT NOT NULL,
          value TEXT NOT NULL,
          source_group_id INTEGER REFERENCES document_groups(id),
          created_at REAL NOT NULL,
          UNIQUE(field, normalized)
        );
        PRAGMA user_version=6;
        COMMIT;""")
    elif version == 6:
        conn.executescript("""BEGIN IMMEDIATE;
        ALTER TABLE group_overrides RENAME TO group_overrides_old;
        CREATE TABLE group_overrides (
          id INTEGER PRIMARY KEY, source_file_id INTEGER NOT NULL REFERENCES source_files(id),
          action TEXT NOT NULL CHECK(action IN ('split','merge','move_page','reorder_page','exclude_page','metadata','approve','needs_review','apply_ollama_suggestion')),
          payload_json TEXT NOT NULL, created_at REAL NOT NULL
        );
        INSERT INTO group_overrides SELECT * FROM group_overrides_old;
        DROP TABLE group_overrides_old;
        PRAGMA user_version=7;
        COMMIT;""")
    elif version == 7:
        conn.executescript("""BEGIN IMMEDIATE;
        CREATE TABLE drive_entries (
          remote_path TEXT PRIMARY KEY,
          remote_id TEXT NOT NULL,
          local_relative_path TEXT NOT NULL UNIQUE,
          processed_remote_path TEXT NOT NULL UNIQUE,
          state TEXT NOT NULL CHECK(state IN ('downloaded','moved','error')),
          source_file_id INTEGER REFERENCES source_files(id),
          last_error TEXT,
          created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE INDEX drive_entries_source ON drive_entries(source_file_id,state);
        PRAGMA user_version=8;
        COMMIT;""")
    elif version == 8:
        # The learning library keeps its own minimal audit trail.  It records
        # only rule identifiers and fields, never document or OCR text.
        conn.executescript("""BEGIN IMMEDIATE;
        CREATE TABLE learning_rule_events (
          id INTEGER PRIMARY KEY,
          action TEXT NOT NULL CHECK(action IN ('removed','reset')),
          rule_id INTEGER,
          field TEXT,
          created_at REAL NOT NULL
        );
        PRAGMA user_version=9;
        COMMIT;""")
