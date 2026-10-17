"""Stage 3: SQLite storage. Idempotent upsert by natural key + content hash, with an audit trail.

Re-importing the same files changes nothing (rows are 'unchanged'). A changed row
is updated in place and the old/new payloads land in audit_log. Derived tables
(dq_issue, fact_*) are cleared and rebuilt from the source records on every run.
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from .ingest import key_of

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_run (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, source TEXT NOT NULL,
    inserted INTEGER, updated INTEGER, unchanged INTEGER, missing INTEGER);
CREATE TABLE IF NOT EXISTS src_record (
    entity TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, row_hash TEXT NOT NULL,
    first_run INTEGER NOT NULL, last_run INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (entity, key));
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, at TEXT NOT NULL,
    entity TEXT NOT NULL, key TEXT NOT NULL, change TEXT NOT NULL, old_payload TEXT, new_payload TEXT);
"""
DERIVED = ("dq_issue", "fact_reservation", "fact_payment", "fact_expense")


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _payload(row: dict) -> str:
    return json.dumps({k: v for k, v in row.items() if not k.startswith("_")}, sort_keys=True, ensure_ascii=False)


def _hash(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()


def import_rows(conn, rows_by_entity: dict[str, list[dict]], source: str) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats = dict(inserted=0, updated=0, unchanged=0, missing=0)
    with conn:
        run_id = conn.execute("INSERT INTO import_run (started_at, source) VALUES (?, ?)", (now, source)).lastrowid
        for entity, rows in rows_by_entity.items():
            seen = set()
            for row in rows:
                key, payload = key_of(entity, row), _payload(row)
                digest = _hash(payload)
                seen.add(key)
                old = conn.execute("SELECT payload, row_hash, active FROM src_record WHERE entity=? AND key=?",
                                   (entity, key)).fetchone()
                if old is None:
                    conn.execute("INSERT INTO src_record VALUES (?,?,?,?,?,?,1)", (entity, key, payload, digest, run_id, run_id))
                    _audit(conn, run_id, now, entity, key, "insert", None, payload)
                    stats["inserted"] += 1
                elif old["row_hash"] != digest:
                    conn.execute("UPDATE src_record SET payload=?, row_hash=?, last_run=?, active=1 WHERE entity=? AND key=?",
                                 (payload, digest, run_id, entity, key))
                    _audit(conn, run_id, now, entity, key, "update", old["payload"], payload)
                    stats["updated"] += 1
                else:
                    conn.execute("UPDATE src_record SET last_run=?, active=1 WHERE entity=? AND key=?", (run_id, entity, key))
                    if not old["active"]:
                        _audit(conn, run_id, now, entity, key, "reappeared", None, payload)
                    stats["unchanged"] += 1
            for gone in conn.execute("SELECT key, payload FROM src_record WHERE entity=? AND active=1", (entity,)).fetchall():
                if gone["key"] not in seen:
                    conn.execute("UPDATE src_record SET active=0 WHERE entity=? AND key=?", (entity, gone["key"]))
                    _audit(conn, run_id, now, entity, gone["key"], "missing_in_source", gone["payload"], None)
                    stats["missing"] += 1
        conn.execute("UPDATE import_run SET inserted=?, updated=?, unchanged=?, missing=? WHERE run_id=?",
                     (stats["inserted"], stats["updated"], stats["unchanged"], stats["missing"], run_id))
    return {"run_id": run_id, **stats}


def _audit(conn, run_id, at, entity, key, change, old, new):
    conn.execute("INSERT INTO audit_log (run_id, at, entity, key, change, old_payload, new_payload) VALUES (?,?,?,?,?,?,?)",
                 (run_id, at, entity, key, change, old, new))


def load(conn, entity: str) -> list[dict]:
    cur = conn.execute("SELECT payload FROM src_record WHERE entity=? AND active=1 ORDER BY key", (entity,))
    return [json.loads(r["payload"]) for r in cur]


def replace_derived(conn, table: str, rows: list[dict], columns: list[str]):
    """Derived tables are disposable: clear and refill so reruns can never double count."""
    assert table in DERIVED, table
    with conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(columns)})")
        conn.execute(f"DELETE FROM {table}")
        conn.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(columns))})",
                         [tuple(r[c] for c in columns) for r in rows])


def read_table(conn, table: str) -> list[dict]:
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
