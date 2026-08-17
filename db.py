import sqlite3
from config import DB_PATH

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_column(conn, table, name, definition):
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

def init_db():
    with db_conn() as conn:
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
        ]:
            ensure_column(conn, "radar_posts", name, definition)

        for name, definition in [
            ("followers_count", "INTEGER DEFAULT 0"),
            ("usual_views", "REAL DEFAULT 0"),
            ("sample_size", "INTEGER DEFAULT 0"),
        ]:
            ensure_column(conn, "tracked_creators", name, definition)
        conn.commit()
