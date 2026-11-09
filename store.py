import sqlite3

DB_FILE = "matches.db"

TABLES = {
    "matches": ("video_id", (
        "video_id", "title", "channel", "duration_s", "clean_title", "artist",
        "spotify_id", "spotify_name", "spotify_artists", "score", "spotify_image",
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
    if "spotify_image" not in {r["name"] for r in db.execute("PRAGMA table_info(matches)")}:
        db.execute("ALTER TABLE matches ADD COLUMN spotify_image TEXT")  
    have = {r["name"] for r in db.execute("PRAGMA table_info(automations)")}
    if "days" not in have:
        db.execute("ALTER TABLE automations ADD COLUMN days INTEGER")
    if "pick" not in have:
        db.execute("ALTER TABLE automations ADD COLUMN pick TEXT DEFAULT 'newest'")
    if "owner" not in have:
        db.execute("ALTER TABLE automations ADD COLUMN owner TEXT")
    if "paused" not in have:
        db.execute("ALTER TABLE automations ADD COLUMN paused INTEGER DEFAULT 0")
    if "source_count" not in have:
        db.execute("ALTER TABLE automations ADD COLUMN source_count INTEGER")
        db.execute("ALTER TABLE automations ADD COLUMN source_checked REAL")
    for column, kind in (("every_n", "INTEGER DEFAULT 1"), ("every_unit", "TEXT DEFAULT 'hours'"),
                         ("trigger", "TEXT DEFAULT 'any'"), ("keep_old", "INTEGER DEFAULT 0"),
                         ("source_keys", "TEXT"),   
                         ("sent_ids", "TEXT"),    
                         ("last_ts", "REAL"),      
                         ("name", "TEXT"), ("note", "TEXT"),  
                         ("kind", "TEXT DEFAULT 'copy'"),
                         ("dest_service", "TEXT"),            
                         ("copies", "INTEGER DEFAULT 1"),      #
                         ("dest_existing", "INTEGER DEFAULT 0"),
                         ("trigger_service", "TEXT"), ("trigger_id", "TEXT"), ("trigger_title", "TEXT"),
                         ("trigger_keys", "TEXT"),
                         ("compare_mode", "TEXT"), ("compare_service", "TEXT"), ("compare_id", "TEXT"),
                         ("compare_title", "TEXT"),
                         ("unchecked", "TEXT"), 
                         ("dedupe_service", "TEXT"), ("dedupe_id", "TEXT"), ("dedupe_title", "TEXT")):  # "remove duplicates" block
        if column not in have:
            db.execute(f"ALTER TABLE automations ADD COLUMN {column} {kind}")
    db.execute("UPDATE automations SET copies = 0, dedupe_service = dest_service, dedupe_id = dest_id, "
               "dedupe_title = dest_name, kind = 'copy' WHERE kind = 'dedupe'")
    db.execute("""
        CREATE TABLE IF NOT EXISTS automation_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            automation_id INTEGER NOT NULL,
            service TEXT NOT NULL,        -- YouTube / Spotify
            playlist_id TEXT NOT NULL,
            title TEXT,
            item_count INTEGER, checked REAL  -- YouTube quota saver: skip a run when nothing changed
        )
    """)
    for a in db.execute("SELECT * FROM automations WHERE id NOT IN (SELECT automation_id FROM automation_sources)").fetchall():
        source, dest = {"yt2sp": ("YouTube", "Spotify"), "sp2yt": ("Spotify", "YouTube")}.get(a["direction"], (None, None))
        if source and a["source_id"]:
            db.execute("INSERT INTO automation_sources (automation_id, service, playlist_id, title, item_count, checked) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (a["id"], source, a["source_id"], a["source_title"],
                                                     a["source_count"], a["source_checked"]))
            db.execute("UPDATE automations SET dest_service = IFNULL(dest_service, ?) WHERE id = ?", (dest, a["id"]))
    db.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            provider TEXT NOT NULL,     -- 'spotify' or 'google'
            account_id TEXT NOT NULL,   -- Spotify user id / YouTube channel id
            uid TEXT NOT NULL,
            PRIMARY KEY (provider, account_id)
        )
    """)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'").fetchone():
        db.execute("INSERT OR IGNORE INTO accounts SELECT 'spotify', spotify_id, uid FROM users")
        db.execute("DROP TABLE users")
    db.commit()
    return db

def claim_account(db, provider, account_id, me):
    row = db.execute("SELECT uid FROM accounts WHERE provider = ? AND account_id = ?",
                     (provider, account_id)).fetchone()
    if row and row["uid"] != me:
        known = row["uid"]
        db.execute("UPDATE automations SET owner = ? WHERE owner = ?", (known, me))
        db.execute("UPDATE OR REPLACE accounts SET uid = ? WHERE uid = ?", (known, me))
        db.commit()
        return known
    if not row:
        db.execute("INSERT INTO accounts (provider, account_id, uid) VALUES (?, ?, ?)", (provider, account_id, me))
        db.commit()
    return me

def set_paused(db, auto_id, owner, paused):
    db.execute("UPDATE automations SET paused = ? WHERE id = ? AND owner = ?", (int(paused), auto_id, owner))
    db.commit()

def automations(db, owner=None):
    if owner is None:
        rows = db.execute("SELECT * FROM automations WHERE owner IS NOT NULL AND NOT IFNULL(paused, 0) "
                          "ORDER BY owner, id")
    else:
        rows = db.execute("SELECT * FROM automations WHERE owner = ? ORDER BY id", (owner,))
    return [dict(r) for r in rows]

def get_automation(db, auto_id, owner):
    row = db.execute("SELECT * FROM automations WHERE id = ? AND owner = ?", (auto_id, owner)).fetchone()
    return dict(row) if row else None

def add_automation(db, owner, sources, **fields):
    first = sources[0] if sources else (None, None, fields.get("dedupe_title"))
    fields = {"owner": owner, "source_id": first[1], "source_title": first[2], **fields}
    cur = db.execute(f"INSERT INTO automations ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                     list(fields.values()))
    set_sources(db, cur.lastrowid, sources)
    return cur.lastrowid

def sources(db, auto_id):
    return [dict(r) for r in db.execute("SELECT * FROM automation_sources WHERE automation_id = ? ORDER BY id", (auto_id,))]

def set_sources(db, auto_id, new):
    old = {(s["service"], s["playlist_id"]): s for s in sources(db, auto_id)}
    db.execute("DELETE FROM automation_sources WHERE automation_id = ?", (auto_id,))
    for service, playlist_id, title in new:
        kept = old.get((service, playlist_id)) or {}
        db.execute("INSERT INTO automation_sources (automation_id, service, playlist_id, title, item_count, checked) "
                   "VALUES (?, ?, ?, ?, ?, ?)", (auto_id, service, playlist_id, title,
                                                 kept.get("item_count"), kept.get("checked")))
    if new:
        db.execute("UPDATE automations SET source_id = ?, source_title = ? WHERE id = ?", (new[0][1], new[0][2], auto_id))
    db.commit()

def update_source(db, source_id, **fields):
    db.execute(f"UPDATE automation_sources SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
               [*fields.values(), source_id])
    db.commit()

def update_automation(db, auto_id, **fields):
    db.execute(
        f"UPDATE automations SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
        [*fields.values(), auto_id],
    )
    db.commit()

def delete_automation(db, auto_id, owner):
    if db.execute("DELETE FROM automations WHERE id = ? AND owner = ?", (auto_id, owner)).rowcount:
        db.execute("DELETE FROM automation_sources WHERE automation_id = ?", (auto_id,))
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

def set_spotify_images(db, images):
    db.executemany("UPDATE matches SET spotify_image = ? WHERE spotify_id = ?",
                   [(url, tid) for tid, url in images.items()])
    db.commit()

def save_match(db, match, table="matches"):
    _, fields = TABLES[table]
    db.execute(
        f"INSERT OR REPLACE INTO {table} ({', '.join(fields)}) "
        f"VALUES ({', '.join('?' for _ in fields)})",
        [match.get(f) for f in fields],
    )
    db.commit()
