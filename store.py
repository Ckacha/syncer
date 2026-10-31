import sqlite3

DB_FILE = "matches.db" 
FIELDS = (
    "video_id", "title", "channel", "duration_s", "clean_title", "artist",
    "spotify_id", "spotify_name", "spotify_artists", "score",
)

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
    
    return db

def get_match(db, video_id):
    row = db.execute("SELECT * FROM matches WHERE video_id = ?", (video_id,)).fetchone()
    return dict(row) if row else None

def set_approved(db, approvals):
    """approvals: {video_id: True/False}"""
    db.executemany(
        "UPDATE matches SET approved = ? WHERE video_id = ?",
        [(int(ok), vid) for vid, ok in approvals.items()],
    )
    db.commit()

def delete_matches(db, video_ids):
    db.executemany("DELETE FROM matches WHERE video_id = ?", [(v,) for v in video_ids])
    db.commit() 

def save_match(db, match):
    db.execute(
        f"INSERT OR REPLACE INTO matches ({', '.join(FIELDS)}) "
        f"VALUES ({', '.join('?' for _ in FIELDS)})",
        [match.get(f) for f in FIELDS],
    )

    db.commit()