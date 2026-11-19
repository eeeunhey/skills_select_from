"""SQLite store (PRD 5.4: Python + SQLite FTS5). One file, migrations applied in order."""
import datetime as dt
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "lab.db"
MIGRATIONS = ROOT / "migrations"


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def connect(path=DB_PATH):
    """Open (and migrate) the DB. path=':memory:' for tests."""
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)  # autocommit; use `with tx(conn)`
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    migrate(conn)
    return conn


def migrate(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.stem not in done:
            conn.executescript("BEGIN;" + f.read_text(encoding="utf-8") +
                               f"\nINSERT INTO schema_migrations VALUES ('{f.stem}', '{now()}');COMMIT;")


class tx:
    """`with tx(conn):` -> BEGIN IMMEDIATE ... COMMIT/ROLLBACK (atomic reservations, checkpoints)."""
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, *_):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
