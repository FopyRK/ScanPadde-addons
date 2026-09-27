import sqlite3
from contextlib import contextmanager
from .migrations import migrate

def connect(path):
    db = sqlite3.connect(path, timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA synchronous=FULL")
    return db

def initialize(path):
    with connection(path) as db:
        mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode.lower() != "wal":
            raise RuntimeError("wal_unavailable")
        migrate(db)

@contextmanager
def connection(path):
    db = connect(path)
    try:
        yield db
    finally:
        db.close()

@contextmanager
def transaction(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
