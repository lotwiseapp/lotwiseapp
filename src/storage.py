"""SQLite storage shared by the monitor and inventory matcher."""

import json
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent
TRANSLATED_FIELDS = ("scope_raw", "review_reason", "reason", "action")


def default_database(demo=False):
    suffix = "_demo.sqlite3" if demo else ".sqlite3"
    current = BASE / ("lotwise" + suffix)
    legacy = BASE / ("shelfsignal" + suffix)
    return legacy if not current.exists() and legacy.is_file() else current


DEFAULT_DB = default_database()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS overviews (
    url TEXT PRIMARY KEY, html_hash TEXT, last_checked TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS articles (
    url TEXT PRIMARY KEY, last_checked TEXT, observed_hash TEXT,
    active_version_id INTEGER REFERENCES notice_versions(id), last_error TEXT
);
CREATE TABLE IF NOT EXISTS overview_articles (
    overview_url TEXT NOT NULL REFERENCES overviews(url),
    article_url TEXT NOT NULL REFERENCES articles(url),
    PRIMARY KEY (overview_url, article_url)
);
CREATE TABLE IF NOT EXISTS notice_versions (
    id INTEGER PRIMARY KEY, article_url TEXT NOT NULL REFERENCES articles(url),
    html_hash TEXT NOT NULL, fetched_at TEXT NOT NULL,
    request_id TEXT, data_json TEXT NOT NULL, markdown TEXT, metadata_json TEXT NOT NULL,
    translation_json TEXT, translation_error TEXT
);
CREATE TABLE IF NOT EXISTS model_cache (
    task TEXT NOT NULL, input_key TEXT NOT NULL, result_json TEXT NOT NULL,
    PRIMARY KEY (task, input_key)
);
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def connect(path, mode=None):
    """Open an existing read-only DB, or initialize a live/demo database explicitly."""
    path = path.resolve()
    db = sqlite3.connect(
        path if mode else path.as_uri() + "?mode=ro",
        uri=mode is None,
        timeout=15,
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        if mode:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEMA)
            with db:
                db.execute("INSERT OR IGNORE INTO settings VALUES ('mode', ?)", (mode,))
        actual = db.execute("SELECT value FROM settings WHERE name = 'mode'").fetchone()
        if not actual or (mode and actual["value"] != mode):
            raise ValueError(f"Database mode mismatch for {path}. Use separate live and demo databases.")
        return db
    except Exception:
        db.close()
        raise
