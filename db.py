"""PostgreSQL data layer for ZEMYIPTV (Neon).

The application was written against SQLite. This module keeps the same
``with get_db() as conn: conn.execute("... ?", params)`` style but runs every
statement on PostgreSQL, so the rest of the codebase did not have to change.

Everything is configured through environment variables (see .env.example):
``DATABASE_URL`` is required; ``NEON_AUTH_*`` is read by auth.py.
"""

import os
import re
import threading
import time

import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_ENV_LOCK = threading.Lock()
_ENV_LOADED = False


def load_env(path: str = None) -> None:
    """Load key=value pairs from .env without overriding real environment vars."""
    global _ENV_LOADED
    with _ENV_LOCK:
        if _ENV_LOADED:
            return
        _ENV_LOADED = True
        path = path or os.path.join(BASE_DIR, ".env")
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
        except OSError:
            pass


def database_url() -> str:
    load_env()
    return (os.environ.get("DATABASE_URL") or "").strip()


def configured() -> bool:
    return bool(database_url())


# ---------------------------------------------------------------------------
# SQL translation: SQLite dialect -> PostgreSQL dialect
# ---------------------------------------------------------------------------

_PRAGMA = re.compile(r"^\s*pragma\b", re.IGNORECASE)
_ROWID = re.compile(r"\browid\b", re.IGNORECASE)


def _split_quoted(sql: str):
    """Yield (segment, is_quoted) so rewrites never touch string literals."""
    buf, quote, i = [], None, 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:  # escaped ''
                    buf.append(sql[i + 1])
                    i += 1
                else:
                    quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            if buf:
                yield "".join(buf), False
                buf = []
            quote = ch
            buf.append(ch)
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        yield "".join(buf), False


def to_pg(sql: str, escape_percent: bool = False):
    """Translate a SQLite statement to PostgreSQL. Returns None for no-ops."""
    if _PRAGMA.match(sql):
        return None
    or_ignore = bool(re.search(r"\bINSERT\s+OR\s+IGNORE\b", sql, re.IGNORECASE))
    parts = []
    for chunk, quoted in _split_quoted(sql):
        if quoted:
            parts.append(chunk)
            continue
        if escape_percent:
            chunk = chunk.replace("%", "%%")
        chunk = chunk.replace("?", "%s")
        chunk = _ROWID.sub("id", chunk)
        chunk = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", chunk, flags=re.IGNORECASE)
        parts.append(chunk)
    out = "".join(parts)
    if or_ignore and "ON CONFLICT" not in out.upper():
        out = out.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return out


def _escape_percent(sql: str) -> str:
    return sql.replace("%", "%%")


class Row(dict):
    """dict-like result row that also supports positional access like sqlite3.Row."""

    __slots__ = ("_values",)

    def __init__(self, mapping):
        super().__init__(mapping)
        self._values = tuple(mapping.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return dict.__getitem__(self, key)

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


class _EmptyCursor:
    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, size=None):
        return []

    @property
    def rowcount(self):
        return 0

    def __iter__(self):
        return iter(())

    def close(self):
        pass


class Cursor:
    def __init__(self, raw):
        self._raw = raw

    def _row(self, item):
        if item is None:
            return None
        return Row(item)

    def fetchone(self):
        return self._row(self._raw.fetchone())

    def fetchall(self):
        return [self._row(r) for r in self._raw.fetchall()]

    def fetchmany(self, size=None):
        rows = self._raw.fetchmany(size) if size else self._raw.fetchmany()
        return [self._row(r) for r in rows]

    @property
    def rowcount(self):
        return self._raw.rowcount

    def __iter__(self):
        for item in self._raw:
            yield self._row(item)

    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass


class Connection:
    """Thin sqlite3-compatible facade over one psycopg2 connection."""

    def __init__(self, raw, releaser=None):
        self._raw = raw
        self._releaser = releaser
        self._broken = False
        self._in_tx = False

    # -- statement execution ------------------------------------------------
    def _execute(self, sql, params, many=False):
        has_params = bool(params)
        pg_sql = to_pg(sql, escape_percent=has_params)
        if pg_sql is None:
            return _EmptyCursor()
        cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            if many:
                seq = [tuple(p) for p in (params or [])]
                if not seq:
                    return Cursor(cur)
                psycopg2.extras.execute_batch(cur, pg_sql, seq, page_size=200)
            elif has_params:
                cur.execute(pg_sql, tuple(params))
            else:
                cur.execute(pg_sql)
        except psycopg2.ProgrammingError as exc:
            # SQLite is loosely typed; retry once with text params before failing.
            if has_params and "operator does not exist" in str(exc):
                cur.close()
                cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                text_params = tuple(None if p is None else str(p) for p in params)
                cur.execute(pg_sql, text_params)
            else:
                cur.close()
                raise
        except Exception:
            cur.close()
            raise
        return Cursor(cur)

    def execute(self, sql, params=()):
        return self._execute(sql, params)

    def executemany(self, sql, seq_of_params):
        return self._execute(sql, seq_of_params, many=True)

    def executescript(self, script):
        for statement in [s.strip() for s in script.split(";")]:
            if statement:
                self.execute(statement)

    # -- transaction control ------------------------------------------------
    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._release()

    def _release(self):
        if self._releaser is not None:
            try:
                self._releaser(self._raw)
            except Exception:
                try:
                    self._raw.close()
                except Exception:
                    pass
            self._releaser = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        except Exception:
            try:
                self._raw.rollback()
            except Exception:
                pass
            self._broken = True
        finally:
            self._release()
        return False


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------

_POOL = None
_POOL_LOCK = threading.Lock()
POOL_MIN = int(os.environ.get("PG_POOL_MIN", "1") or 1)
POOL_MAX = int(os.environ.get("PG_POOL_MAX", "8") or 8)


def _dsn() -> str:
    url = database_url()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Add your Neon Postgres connection string "
            "to the environment (see .env.example)."
        )
    return url


def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = pg_pool.ThreadedConnectionPool(
                POOL_MIN,
                POOL_MAX,
                dsn=_dsn(),
                connect_timeout=15,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=3,
                application_name="zemyiptv",
            )
    return _POOL


def _acquire():
    pool = _get_pool()
    try:
        return pool.getconn()
    except pg_pool.PoolError:
        time.sleep(0.2)
        return pool.getconn()


def _discard(raw):
    try:
        _POOL.putconn(raw, close=True)
    except Exception:
        try:
            raw.close()
        except Exception:
            pass


def get_db():
    """Context manager returning a sqlite3-like connection backed by Postgres."""
    raw = _acquire()
    conn = Connection(raw, releaser=_put_back)
    try:
        raw.cursor().execute("SELECT 1")
    except Exception:
        _discard(raw)
        raw = _acquire()
        conn = Connection(raw, releaser=_put_back)
    return conn


def _put_back(raw):
    try:
        raw.rollback()
    except Exception:
        _discard(raw)
        return
    try:
        _POOL.putconn(raw)
    except Exception:
        _discard(raw)


def connect():
    """Direct connection for scripts and one-off work (no pooling)."""
    return psycopg2.connect(_dsn(), connect_timeout=20, application_name="zemyiptv-script")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS playlists (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    epg_url TEXT DEFAULT '',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    logo TEXT DEFAULT '',
    category TEXT DEFAULT 'Uncategorized',
    tvg_id TEXT DEFAULT '',
    tvg_name TEXT DEFAULT '',
    chno TEXT DEFAULT '',
    user_agent TEXT DEFAULT '',
    referrer TEXT DEFAULT '',
    status TEXT DEFAULT 'unknown',
    latency_ms INTEGER DEFAULT 0,
    last_checked_at BIGINT DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    last_error TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    position INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_channels_playlist ON channels(playlist_id, position);
CREATE INDEX IF NOT EXISTS idx_channels_name ON channels(lower(name));

CREATE TABLE IF NOT EXISTS health_logs (
    id TEXT PRIMARY KEY,
    ran_at BIGINT NOT NULL,
    trigger_type TEXT NOT NULL,
    total_checked INTEGER DEFAULT 0,
    online_count INTEGER DEFAULT 0,
    offline_count INTEGER DEFAULT 0,
    failovers_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS provider_sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    badge TEXT NOT NULL,
    provider_type TEXT NOT NULL DEFAULT 'fast_platform',
    channel_count INTEGER DEFAULT 0,
    last_synced_at BIGINT DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    is_system INTEGER DEFAULT 0,
    position INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS channel_sources (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    source_name TEXT DEFAULT 'Backup Stream',
    quality TEXT DEFAULT 'HD',
    priority INTEGER DEFAULT 1,
    status TEXT DEFAULT 'unknown',
    latency_ms INTEGER DEFAULT 0,
    last_checked_at BIGINT DEFAULT 0,
    confidence INTEGER DEFAULT 90,
    last_error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_channel_sources_channel ON channel_sources(channel_id, priority);

CREATE TABLE IF NOT EXISTS admin_users (
    user_id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin' CHECK (role IN ('owner', 'admin', 'viewer')),
    granted_by TEXT DEFAULT '',
    created_at BIGINT NOT NULL,
    last_seen_at BIGINT DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_users_email ON admin_users(lower(email));

CREATE TABLE IF NOT EXISTS admin_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    email TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL,
    last_seen_at BIGINT NOT NULL,
    ip TEXT DEFAULT '',
    user_agent TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_expiry ON admin_sessions(expires_at);

CREATE TABLE IF NOT EXISTS admin_audit (
    id BIGSERIAL PRIMARY KEY,
    at BIGINT NOT NULL,
    actor_user_id TEXT DEFAULT '',
    actor_email TEXT DEFAULT '',
    action TEXT NOT NULL,
    detail TEXT DEFAULT '',
    ip TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_at ON admin_audit(at DESC);
"""


def init_schema() -> None:
    with get_db() as conn:
        conn.executescript(SCHEMA_SQL)


def health() -> dict:
    try:
        with get_db() as conn:
            row = conn.execute("SELECT current_database() AS db, version() AS v").fetchone()
        return {"storage": "postgres", "database": row["db"], "version": row["v"].split(" ")[1]}
    except Exception as exc:  # pragma: no cover - reported to the health endpoint
        return {"storage": "unavailable", "error": str(exc)[:200]}
