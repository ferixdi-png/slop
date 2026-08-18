import sqlite3
import time

from config import DB_PATH

SQLITE_TIMEOUT_SECONDS = 60
_LOCK_RETRY_DELAYS = (0.05, 0.12, 0.3, 0.7, 1.5)


def _is_lock_error(exc):
    text = str(exc or "").lower()
    return "database is locked" in text or "database is busy" in text or "locked" == text.strip()


class ResilientConnection(sqlite3.Connection):
    """Retry only SQLite lock/busy errors; never hide real SQL/programming errors."""

    def _retry(self, method, *args, **kwargs):
        for attempt in range(len(_LOCK_RETRY_DELAYS) + 1):
            try:
                return method(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not _is_lock_error(exc) or attempt >= len(_LOCK_RETRY_DELAYS):
                    raise
                time.sleep(_LOCK_RETRY_DELAYS[attempt])

    def execute(self, *args, **kwargs):
        return self._retry(super().execute, *args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self._retry(super().executemany, *args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self._retry(super().executescript, *args, **kwargs)

    def commit(self):
        return self._retry(super().commit)


def db_conn():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=SQLITE_TIMEOUT_SECONDS,
        factory=ResilientConnection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-16000")
    conn.execute("PRAGMA journal_size_limit=67108864")
    return conn


def ensure_column(conn, table, name, definition):
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db():
    with db_conn() as conn:
        # WAL allows radar/status writes while the browser reads results.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=250")

        conn.execute("""CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, title TEXT NOT NULL,
            source_url TEXT, views INTEGER DEFAULT 0, viral_score REAL DEFAULT 0,
            model TEXT NOT NULL, result_json TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS radar_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL DEFAULT 'Instagram Reels',
            creator TEXT NOT NULL, post_url TEXT NOT NULL UNIQUE, video_url TEXT DEFAULT '',
            preview_url TEXT DEFAULT '', published_at TEXT NOT NULL, duration_sec REAL DEFAULT 0,
            views INTEGER DEFAULT 0, likes INTEGER DEFAULT 0, comments INTEGER DEFAULT 0,
            hours_since_publish REAL DEFAULT 0, views_per_hour REAL DEFAULT 0, search_term TEXT DEFAULT '',
            caption TEXT DEFAULT '', ai_checked INTEGER DEFAULT 0, ai_match INTEGER DEFAULT 0,
            scene_description TEXT DEFAULT '', characters_json TEXT DEFAULT '[]', joke TEXT DEFAULT '',
            hook TEXT DEFAULT '', ending TEXT DEFAULT '', reproducible INTEGER DEFAULT 0, reason TEXT DEFAULT '')""")
        conn.execute("""CREATE TABLE IF NOT EXISTS tracked_creators (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            best_views_per_hour REAL DEFAULT 0, matching_reels INTEGER DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS radar_meta (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            source_count INTEGER DEFAULT 0,
            average_duration_sec REAL DEFAULT 0,
            report_json TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL)""")

        for name, definition in [
            ("followers_count", "INTEGER DEFAULT 0"),
            ("creator_usual_views", "REAL DEFAULT 0"),
            ("anomaly_multiplier", "REAL DEFAULT 0"),
            ("follower_reach", "REAL DEFAULT 0"),
            ("like_rate", "REAL DEFAULT 0"),
            ("comment_rate", "REAL DEFAULT 0"),
            ("viral_score_v2", "REAL DEFAULT 0"),
            ("screening_profile", "TEXT DEFAULT ''"),
        ]:
            ensure_column(conn, "radar_posts", name, definition)

        for name, definition in [
            ("followers_count", "INTEGER DEFAULT 0"),
            ("usual_views", "REAL DEFAULT 0"),
            ("sample_size", "INTEGER DEFAULT 0"),
        ]:
            ensure_column(conn, "tracked_creators", name, definition)

        # Hot read paths used by /api/radar and candidate cache checks.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_radar_recent_match "
            "ON radar_posts(ai_match,published_at,viral_score_v2)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_radar_creator_recent "
            "ON radar_posts(creator,published_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analyses_source_recent "
            "ON analyses(source_url,created_at)"
        )
        conn.commit()
