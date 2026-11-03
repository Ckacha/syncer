import sqlite3

DB_FILE = "matches.db"

TABLES = {
    "matches": ("video_id", (
        "video_id", "title", "channel", "duration_s", "clean_title", "artist",
        "spotify_id", "spotify_name", "spotify_artists", "score",
    )),
    "reverse_matches": ("track_id", (
        "track_id", "track_name", "track_artists", "duration_s",
        "video_id", "video_title", "video_channel", "score",
    )),
}

def connect():
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            video_id TEXT PRIMARY KEY,
            title TEXT, channel TEXT, duration_s INTEGER,
            clean_title TEXT, artist TEXT,
            spotify_id TEXT, spotify_name TEXT, spotify_artists TEXT,
            score INTEGER,
            approved INTEGER,  -- NULL = not decided, 1 = approved, 0 = rejected
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS reverse_matches (
            track_id TEXT PRIMARY KEY,
            track_name TEXT, track_artists TEXT, duration_s INTEGER,
            video_id TEXT, video_title TEXT, video_channel TEXT,
            score INTEGER,
            approved INTEGER,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS automations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT, source_id TEXT, source_title TEXT,
            dest_id TEXT,      -- NULL until the first run creates it
            dest_name TEXT,
            count INTEGER,     -- keep at most this many songs, NULL = no limit
            days INTEGER,      -- only songs added in the last this many days, NULL = any time
            pick TEXT DEFAULT 'newest',  -- which end to keep when trimming: newest / oldest
            last_run TEXT, last_result TEXT
        )
    """)
    have = {r["name"] for r in db.execute("PRAGMA table_info(automations)")}
    if "days" not in have:
        db.execute("ALTER TABLE automations ADD COLUMN days INTEGER")
    if "pick" not in have:
        db.execute("ALTER TABLE automations ADD COLUMN pick TEXT DEFAULT 'newest'")
    return db

def automations(db):
    return [dict(r) for r in db.execute("SELECT * FROM automations ORDER BY id")]

def get_automation(db, auto_id):
    row = db.execute("SELECT * FROM automations WHERE id = ?", (auto_id,)).fetchone()
    return dict(row) if row else None

def add_automation(db, direction, source_id, source_title, dest_name, count, days=None, pick="newest"):
    db.execute(
        "INSERT INTO automations (direction, source_id, source_title, dest_name, count, days, pick) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (direction, source_id, source_title, dest_name, count, days, pick),
    )
    db.commit()

def update_automation(db, auto_id, **fields):
    db.execute(
        f"UPDATE automations SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
        [*fields.values(), auto_id],
    )
    db.commit()

def delete_automation(db, auto_id):
    db.execute("DELETE FROM automations WHERE id = ?", (auto_id,))
    db.commit()

def get_match(db, key, table="matches"):
    pk, _ = TABLES[table]
    row = db.execute(f"SELECT * FROM {table} WHERE {pk} = ?", (key,)).fetchone()
    return dict(row) if row else None

def set_approved(db, approvals, table="matches"):
    """approvals: {key: True/False}"""
    pk, _ = TABLES[table]
    db.executemany(
        f"UPDATE {table} SET approved = ? WHERE {pk} = ?",
        [(int(ok), key) for key, ok in approvals.items()],
    )
    db.commit()

def delete_matches(db, keys, table="matches"):
    pk, _ = TABLES[table]
    db.executemany(f"DELETE FROM {table} WHERE {pk} = ?", [(k,) for k in keys])
    db.commit()

def save_match(db, match, table="matches"):
    _, fields = TABLES[table]
    db.execute(
        f"INSERT OR REPLACE INTO {table} ({', '.join(fields)}) "
        f"VALUES ({', '.join('?' for _ in fields)})",
        [match.get(f) for f in fields],
    )
    db.commit()
