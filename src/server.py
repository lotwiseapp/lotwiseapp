#!/usr/bin/env python3
"""Serve the LotWise browser dashboard and manage its background monitor."""
import argparse
import csv
import json
import logging
import sqlite3
import subprocess
import sys
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from demo_recall import DEMO_FILE, DEMO_URL, read_simulation
from email_drafts import EmailDrafts
from match_inventory import INSPECT, MATCH, NO_MATCH, check_row, parse_inventory, read_db
from monitor import POLL_SECONDS, make_model, make_scraper
from providers import digest
from storage import BASE, DEFAULT_DB, connect, encode

MAX_ROWS = 1000
LOG = logging.getLogger("lotwise.web")

@contextmanager
def background_monitor(db_path, disabled, interval):
    if disabled:
        yield lambda: {"state": "snapshot", "detail": "Background monitoring paused"}
        return
    try:
        make_model()
        make_scraper()
    except ValueError:
        yield lambda: {"state": "snapshot", "detail": "Add API keys to enable live polling"}
        return

    process = subprocess.Popen(
        [sys.executable, "-u", str(BASE / "monitor.py"), "--db", str(db_path), "--interval", str(interval)],
        cwd=BASE,
    )

    def status():
        exit_code = process.poll()
        if exit_code is not None:
            return {"state": "stopped", "detail": f"Monitor exited ({exit_code}). See server logs."}
        return {"state": "running", "detail": f"Monitor running · {interval:g}s interval"}

    try:
        yield status
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def snapshot(path, demo_file):
    with closing(connect(path)) as db:
        _, notices, issues = read_db(db)
        overviews = [dict(row) for row in db.execute("SELECT url, last_checked FROM overviews ORDER BY url")]
    simulation = read_simulation(demo_file)
    if simulation:
        notices.append(simulation)
    revision = digest(encode([
        [(n["id"], n["url"], n["english"], n["refresh_error"]) for n in notices],
        issues,
    ]).encode())
    return notices, issues, overviews, revision


def create_app(db_path=DEFAULT_DB, demo_file=DEMO_FILE, health=None):
    app = Flask(__name__, static_folder="web", static_url_path="/assets")
    app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
    health = health or (lambda: {"state": "snapshot", "detail": "Saved notice snapshot"})
    try:
        model = make_model()
    except ValueError:
        model = None

    drafts = EmailDrafts(model)
    app.extensions["email_drafts"] = drafts

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(413)
    def large_upload(_):
        return jsonify(error="Request is too large."), 413

    @app.errorhandler(sqlite3.Error)
    def database_error(exc):
        LOG.error("SQLite read failed: %s", exc)
        return jsonify(error="The recall database is temporarily unavailable."), 503

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/examples/<name>")
    def sample(name):
        return send_from_directory(BASE / "examples", name, as_attachment=True)

    @app.post("/api/upload")
    def upload():
        incoming = request.files.get("inventory")
        if incoming is None:
            return jsonify(error="Choose an inventory CSV."), 400
        try:
            content = incoming.read(512 * 1024 + 1)
            if len(content) > 512 * 1024:
                return jsonify(error="CSV upload is limited to 512 KB."), 413
            columns, rows = parse_inventory(content.decode("utf-8-sig"))
            if not rows:
                raise ValueError("The CSV contains no inventory rows.")
            if len(rows) > MAX_ROWS:
                raise ValueError(f"Use up to {MAX_ROWS:,} inventory rows per upload.")
        except (ValueError, csv.Error) as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(columns=columns, rows=rows)

    @app.post("/api/check")
    def check():
        body = request.get_json(silent=True)
        rows = body.get("rows") if isinstance(body, dict) else None
        if not isinstance(rows, list) or len(rows) > MAX_ROWS or any(
            not isinstance(row, dict) or any(not isinstance(v, str) for v in row.values()) for row in rows
        ):
            return jsonify(error="Invalid inventory rows."), 400
        notices, issues, overviews, revision = snapshot(db_path, demo_file)
        state = {
            "revision": revision, "notice_count": len(notices) - int(any(n["url"] == DEMO_URL for n in notices)),
            "simulation_active": any(n["url"] == DEMO_URL for n in notices),
            "issues": issues, "sources": overviews, "monitor": health(),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        if body.get("revision") == revision:
            return jsonify(state)
        results = []
        counts = Counter()
        for row in rows:
            result = check_row(row, notices)
            counts[result["status"]] += 1
            for evidence in result["evidence"]:
                evidence["simulated"] = evidence["notice_url"] == DEMO_URL
            reported = {key: result[key] for key in ("status", "reason", "evidence")}
            if result["status"] in {MATCH, INSPECT}:
                reported["draft"] = drafts.envelope(row, result)
            results.append(reported)
        return jsonify({**state, "results": results, "counts": {key: counts[key] for key in (MATCH, INSPECT, NO_MATCH)}})

    @app.post("/api/drafts")
    def draft_status():
        body = request.get_json(silent=True)
        keys = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(keys, list) or len(keys) > 1000 or any(
            not isinstance(key, str) or len(key) != 64 for key in keys
        ):
            return jsonify(error="Invalid draft references."), 400
        return jsonify(drafts=drafts.status(dict.fromkeys(keys)))

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--interval", type=float, default=POLL_SECONDS, help="Monitor poll interval in seconds")
    parser.add_argument("--no-monitor", action="store_true", help="Serve stored records without live polling")
    parser.add_argument("--demo-state", type=Path, default=DEMO_FILE)
    args = parser.parse_args()
    if not 0 < args.interval < float("inf"):
        parser.error("--interval must be a finite positive number")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    db_path = args.db.resolve()
    if not db_path.is_file():
        parser.error(f"Recall database not found at {db_path}")
    with background_monitor(db_path, args.no_monitor, args.interval) as status:
        app = create_app(db_path, args.demo_state.resolve(), status)
        try:
            app.run(host="127.0.0.1", port=args.port, use_reloader=False)
        finally:
            app.extensions["email_drafts"].close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
