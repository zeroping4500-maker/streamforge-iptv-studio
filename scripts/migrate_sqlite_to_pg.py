#!/usr/bin/env python3
"""Copy an existing SQLite database (data/iptv_studio.db) into Neon Postgres.

    python3 scripts/migrate_sqlite_to_pg.py [path/to/iptv_studio.db]

Idempotent: rows that already exist (same primary key) are skipped, so it is
safe to run more than once.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402

TABLES = [
    ("playlists", ["id", "name", "description", "epg_url", "created_at", "updated_at"]),
    (
        "channels",
        [
            "id", "playlist_id", "name", "url", "logo", "category", "tvg_id", "tvg_name",
            "chno", "user_agent", "referrer", "status", "latency_ms", "last_checked_at",
            "fail_count", "last_error", "enabled", "position",
        ],
    ),
    (
        "health_logs",
        ["id", "ran_at", "trigger_type", "total_checked", "online_count", "offline_count", "failovers_count"],
    ),
    (
        "provider_sources",
        ["id", "name", "url", "badge", "provider_type", "channel_count", "last_synced_at", "enabled", "is_system", "position"],
    ),
    (
        "channel_sources",
        [
            "id", "channel_id", "name", "url", "source_name", "quality", "priority", "status",
            "latency_ms", "last_checked_at", "confidence", "last_error",
        ],
    ),
]


def main(argv):
    path = argv[0] if argv else os.path.join(db.BASE_DIR, "data", "iptv_studio.db")
    if not os.path.exists(path):
        print(f"No SQLite database at {path}; nothing to migrate.")
        return 1
    db.init_schema()
    source = sqlite3.connect(path)
    source.row_factory = sqlite3.Row
    total = 0
    for table, columns in TABLES:
        try:
            rows = source.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
        except sqlite3.Error as exc:
            print(f"{table}: skipped ({exc})")
            continue
        if not rows:
            print(f"{table}: 0 rows")
            continue
        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
            "ON CONFLICT (id) DO NOTHING"
        )
        payload = [tuple(row[col] for col in columns) for row in rows]
        with db.get_db() as conn:
            conn.executemany(sql, payload)
        print(f"{table}: {len(payload)} rows copied")
        total += len(payload)
    source.close()
    print(f"Done. {total} rows migrated into Neon Postgres.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
