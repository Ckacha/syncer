import json
import time
import auth
import matcher

from googleapiclient.discovery import build
from googleapiclient.http import HttpRequest

COSTS = {"search.list": 100, "insert": 50, "update": 50, "delete": 50, "list": 1}
CACHE_SECONDS = 600


def cost_of(method_id):
    name = method_id.split(".", 1)[-1] if method_id else ""
    if name in COSTS:
        return COSTS[name]
    return COSTS.get(name.rsplit(".", 1)[-1], 1)


class Meter:
    def __init__(self):
        self.units = 0
        self.calls = {}

    def add(self, method_id):
        self.units += cost_of(method_id)
        key = method_id.replace("youtube.", "")
        self.calls[key] = self.calls.get(key, 0) + 1


def metered_youtube(uid, meter):
    class CountingRequest(HttpRequest):
        def execute(self, *args, **kwargs):
            meter.add(self.methodId)
            return super().execute(*args, **kwargs)

    creds = auth.load_youtube_creds(uid)
    if not creds:
        raise auth.NotLoggedIn("youtube")
    return build("youtube", "v3", credentials=creds, requestBuilder=CountingRequest) 

def ensure_tables(db):
    cols = {r["name"] for r in db.execute("PRAGMA table_info(video_details)")}
    if "titles" in cols: 
        db.execute("DROP TABLE video_details")
    db.execute("""
        CREATE TABLE IF NOT EXISTS video_details (
            video_id TEXT PRIMARY KEY,
            duration_s INTEGER, description TEXT,
            localized TEXT,  -- json list of the uploader's translated titles
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS playlist_counts (
            playlist_id TEXT PRIMARY KEY, item_count INTEGER, checked_at REAL
        )
    """)
    db.commit()

def saved_details(db, ids):
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        rows = db.execute(f"SELECT * FROM video_details WHERE video_id IN ({','.join('?' * len(chunk))})", chunk)
        out.update({r["video_id"]: r for r in rows})
    return out

_playlists = {}  

def clear_cache():
    _playlists.clear()

def _login_key(yt):
    creds = getattr(getattr(yt, "_http", None), "credentials", None)
    return hash(getattr(creds, "refresh_token", None) or getattr(creds, "token", None) or id(yt))

def load_youtube_playlist(yt, db, playlist_id, use_cache=True):
    ensure_tables(db)
    key = (playlist_id, _login_key(yt))
    hit = _playlists.get(key)
    if use_cache and hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]

    meta = yt.playlists().list(part="snippet", id=playlist_id).execute()["items"]
    if not meta:
        return None
    videos = []
    req = yt.playlistItems().list(part="snippet", playlistId=playlist_id, maxResults=50)
    while req:
        resp = req.execute()
        for item in resp["items"]:
            s = item["snippet"]
            if "videoOwnerChannelTitle" not in s:
                continue
            videos.append({"video_id": s["resourceId"]["videoId"], "title": s["title"],
                           "channel": s["videoOwnerChannelTitle"], "added_at": s["publishedAt"]})
        req = yt.playlistItems().list_next(req, resp)

    known = saved_details(db, [v["video_id"] for v in videos])
    missing = [v["video_id"] for v in videos if v["video_id"] not in known]
    for i in range(0, len(missing), 50):
        resp = yt.videos().list(part="contentDetails,snippet,localizations", id=",".join(missing[i:i + 50])).execute()
        for item in resp["items"]:
            localized = [l["title"] for l in item.get("localizations", {}).values()]
            db.execute("INSERT OR REPLACE INTO video_details (video_id, duration_s, description, localized) "
                       "VALUES (?, ?, ?, ?)",
                       (item["id"], matcher.parse_iso_duration(item["contentDetails"]["duration"]),
                        item["snippet"].get("description", ""), json.dumps(localized)))
    db.commit()
    known = saved_details(db, [v["video_id"] for v in videos])

    for v in videos:
        d = known.get(v["video_id"])
        v["duration_s"] = d["duration_s"] if d else None
        v["description"] = d["description"] if d else ""
        v["titles"] = list(dict.fromkeys([v["title"]] + (json.loads(d["localized"]) if d else [])))
    result = (meta[0]["snippet"]["title"], videos)
    _playlists[key] = (time.time(), result)
    return result

def playlist_count(yt, playlist_id):
    items = yt.playlists().list(part="contentDetails", id=playlist_id).execute()["items"]
    return items[0]["contentDetails"]["itemCount"] if items else None

def playlist_changed(yt, db, playlist_id, full_refresh_hours=24):
    ensure_tables(db)
    items = yt.playlists().list(part="contentDetails", id=playlist_id).execute()["items"]
    if not items:
        return True
    count = items[0]["contentDetails"]["itemCount"]
    row = db.execute("SELECT * FROM playlist_counts WHERE playlist_id = ?", (playlist_id,)).fetchone()
    changed = (not row or row["item_count"] != count
               or time.time() - row["checked_at"] > full_refresh_hours * 3600)
    if changed:
        db.execute("INSERT OR REPLACE INTO playlist_counts VALUES (?, ?, ?)", (playlist_id, count, time.time()))
        db.commit()
    return changed

def reuse_reverse_match(db, track):
    rows = db.execute(
        "SELECT * FROM matches WHERE spotify_id = ? AND (approved = 1 OR (approved IS NULL AND score >= ?)) "
        "ORDER BY score DESC", (track["track_id"], matcher.AUTO_ADD)).fetchall()
    row = next((r for r in rows if not matcher.version_mismatch(r["title"], track["track_name"])), None)
    if not row:
        return None
    return {**track, "video_id": row["video_id"], "video_title": row["title"],
            "video_channel": row["channel"], "score": row["score"]}
