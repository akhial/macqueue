import contextlib
import json
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

from .common import json_bytes, require

TERMINAL = {"succeeded", "failed", "cancelled", "expired", "lost"}
LEASE_SECONDS = 60


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.directory / "queue.sqlite3"
        with contextlib.closing(self.connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL,
                    started REAL, finished REAL, worker TEXT, lease TEXT, lease_until REAL,
                    result TEXT, artifact TEXT, artifact_sha256 TEXT,
                    request_key TEXT UNIQUE NOT NULL, log_bytes INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created);
                CREATE TABLE IF NOT EXISTS logs (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL, text TEXT NOT NULL,
                    PRIMARY KEY(job_id, seq)
                );
            """)

    def connect(self):
        db = sqlite3.connect(self.db, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextlib.contextmanager
    def transaction(self):
        with contextlib.closing(self.connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                self.reap(db)
                yield db

    @staticmethod
    def reap(db):
        now = time.time()
        db.execute("UPDATE jobs SET status='expired',finished=? WHERE status='queued' AND expires<=?", (now, now))
        db.execute("""UPDATE jobs SET status=CASE WHEN status='cancel_requested' THEN 'cancelled' ELSE 'lost' END,
                   finished=?,result=? WHERE status IN ('running','cancel_requested') AND lease_until<=?""",
                   (now, json.dumps({"error": "worker lease expired; never automatically replayed"}), now))

    @staticmethod
    def public(row):
        if row is None:
            raise KeyError("job not found")
        result = dict(row)
        for field in ("lease", "request_key", "artifact"):
            result.pop(field, None)
        result["spec"] = json.loads(result["spec"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        result["has_artifact"] = bool(row["artifact"])
        return result

    def submit(self, spec, key):
        encoded = json_bytes(spec).decode()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE request_key=?", (key,)).fetchone()
            if row:
                require(json.loads(row["spec"]) == spec, "idempotency key already used for another job")
                return self.public(row)
            require(db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0] < 1000,
                    "queue is full")
            job_id, now = uuid.uuid4().hex, time.time()
            db.execute("INSERT INTO jobs(id,spec,status,created,expires,request_key) VALUES(?,?,'queued',?,?,?)",
                       (job_id, encoded, now, now + spec.get("expires_in_seconds", 86400), key))
            return self.public(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def get(self, job_id):
        with self.transaction() as db:
            return self.public(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list(self, limit=100):
        with self.transaction() as db:
            result = []
            for row in db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,)):
                item = self.public(row)
                spec = item.pop("spec")
                item.pop("result")
                item.update(project=spec["project"], label=spec.get("label"), steps=len(spec["steps"]))
                result.append(item)
            return result

    def claim(self, worker, projects):
        with self.transaction() as db:
            # Global serialization also fences a restarted worker until its old lease expires.
            if db.execute("SELECT 1 FROM jobs WHERE status IN ('running','cancel_requested')").fetchone():
                return None
            for row in db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created"):
                if json.loads(row["spec"])["project"] in projects:
                    lease, now = secrets.token_urlsafe(32), time.time()
                    db.execute("UPDATE jobs SET status='running',worker=?,lease=?,lease_until=?,started=? WHERE id=?",
                               (worker, lease, now + LEASE_SECONDS, now, row["id"]))
                    job = self.public(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
                    return {"job": job, "lease": lease, "lease_seconds": LEASE_SECONDS}
            return None

    @staticmethod
    def owned(db, job_id, lease):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("job not found")
        require(row["lease"] is not None and secrets.compare_digest(row["lease"], lease or ""), "wrong job lease")
        require(row["status"] in ("running", "cancel_requested"), "job lease is no longer active")
        return row

    def heartbeat(self, job_id, lease):
        with self.transaction() as db:
            row = self.owned(db, job_id, lease)
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() + LEASE_SECONDS, job_id))
            return {"cancel": row["status"] == "cancel_requested", "lease_seconds": LEASE_SECONDS}

    def cancel(self, job_id):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("job not found")
            if row["status"] == "queued":
                db.execute("UPDATE jobs SET status='cancelled',finished=? WHERE id=?", (time.time(), job_id))
            elif row["status"] == "running":
                db.execute("UPDATE jobs SET status='cancel_requested' WHERE id=?", (job_id,))
            return self.public(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def append_log(self, job_id, lease, seq, text):
        with self.transaction() as db:
            row = self.owned(db, job_id, lease)
            existing = db.execute("SELECT text FROM logs WHERE job_id=? AND seq=?", (job_id, seq)).fetchone()
            if existing:
                require(existing["text"] == text, "log sequence collision")
                return
            count = len(text.encode())
            require(row["log_bytes"] + count <= 16 * 1024 * 1024, "event log limit reached")
            db.execute("INSERT INTO logs VALUES(?,?,?)", (job_id, seq, text))
            db.execute("UPDATE jobs SET log_bytes=log_bytes+? WHERE id=?", (count, job_id))

    def logs(self, job_id, after):
        with self.transaction() as db:
            require(db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone(), "job not found")
            return [dict(r) for r in db.execute("SELECT seq,text FROM logs WHERE job_id=? AND seq>? ORDER BY seq LIMIT 100",
                                               (job_id, after))]

    def artifact(self, job_id):
        with self.transaction() as db:
            row = db.execute("SELECT artifact,artifact_sha256 FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or not row["artifact"]:
                raise KeyError("artifact not found")
            return self.directory / row["artifact"], row["artifact_sha256"]

    def finish(self, job_id, lease, status, result):
        require(status in ("succeeded", "failed", "cancelled"), "invalid final status")
        with self.transaction() as db:
            current = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            # A retried completion after a lost HTTP reply is safe.
            if current and current["status"] in TERMINAL and secrets.compare_digest(current["lease"] or "", lease or ""):
                require(current["result"] == json_bytes(result).decode() and
                        current["status"] in (status, "cancelled"), "completion conflicts with recorded outcome")
                return self.public(current)
            row = self.owned(db, job_id, lease)
            if row["status"] == "cancel_requested":
                status = "cancelled"
            require(status != "succeeded" or row["artifact"], "successful jobs require an artifact upload")
            db.execute("UPDATE jobs SET status=?,result=?,finished=? WHERE id=?",
                       (status, json_bytes(result).decode(), time.time(), job_id))
            return self.public(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def prune(self, days):
        with self.transaction() as db:
            rows = db.execute("SELECT id,artifact FROM jobs WHERE finished<?", (time.time() - days * 86400,)).fetchall()
            for row in rows:
                if row["artifact"]:
                    (self.directory / row["artifact"]).unlink(missing_ok=True)
                db.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
            return len(rows)
