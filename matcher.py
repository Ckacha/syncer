import re
import unicodedata
from difflib import SequenceMatcher

NOISE_WORDS = (
    "official", "video", "audio", "lyric", "lyrics", "hd", "hq", "4k", "mv",
    "visualizer", "visualiser", "music video", "explicit", "clean", "color coded",
)
CHANNEL_NOISE = re.compile(r"vevo|official|music|records|\s-\stopic$", re.I)
FEAT = re.compile(r"\s*[\(\[]?\s*\b(?:ft|feat|featuring)\b\.?\s+[^\)\]]*[\)\]]?", re.I)
DASHES = re.compile("\\s+[-\u2013\u2014]\\s+")  
SLASH = re.compile(r"\s+/\s*|\s*/\s+") 
SPOTIFY_LINK = re.compile(r"open\.spotify\.com/(?:intl-\w+/)?track/([A-Za-z0-9]{22})")

""" 6 seems to be the best overall but that does sorta use up too many searches imo, try eventually figuring out a better method!!!"""
MAX_SEARCHES = 6
AUTO_ADD = 8
REVIEW = 5     

def youtube_playlist_videos(yt, playlist_id):
    videos = []
    req = yt.playlistItems().list(part="snippet", playlistId=playlist_id, maxResults=50)
    while req:
        resp = req.execute()
        for item in resp["items"]:
            s = item["snippet"]
            if "videoOwnerChannelTitle" not in s:
                continue
            videos.append({
                "video_id": s["resourceId"]["videoId"],
                "title": s["title"],
                "channel": s["videoOwnerChannelTitle"],
            })
        req = yt.playlistItems().list_next(req, resp)

    details = {}

    for i in range(0, len(videos), 50):
        ids = ",".join(v["video_id"] for v in videos[i:i + 50])
        resp = yt.videos().list(part="contentDetails,snippet,localizations", id=ids).execute()
        for item in resp["items"]:
            details[item["id"]] = item

    for v in videos:
        item = details.get(v["video_id"], {})
        v["duration_s"] = parse_iso_duration(item["contentDetails"]["duration"]) if item else None
        v["description"] = item.get("snippet", {}).get("description", "")
        titles = [v["title"]] + [loc["title"] for loc in item.get("localizations", {}).values()]
        v["titles"] = list(dict.fromkeys(titles))
    return videos

def parse_iso_duration(value):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s

def strip_noise(title):
    def drop_if_noise(m):
        inner = m.group(0)[1:-1].lower()
        return "" if any(w in inner for w in NOISE_WORDS) else m.group(0)

    title = re.sub(r"[\(\[][^\)\]]*[\)\]]", drop_if_noise, title)
    title = re.sub(r"【[^】]*】", " ", title)
    title = title.split("|")[0]             
    title = FEAT.sub("", title)
    title = re.sub(r"\b(official\s+)?(music\s+)?video\b|\blyrics?\b", "", title, flags=re.I)

    return re.sub(r"\s+", " ", title).strip(" -\u2013\u2014")  #

def parse_video(title, channel):
    is_topic = channel.endswith(" - Topic")
    cleaned = strip_noise(unicodedata.normalize("NFKC", title))
    artists, extra = [], []

    if is_topic:
        artists.append(channel[: -len(" - Topic")])
    else:
        parts = DASHES.split(cleaned, maxsplit=1)
        if len(parts) == 2:
            artists.append(parts[0])
            cleaned = parts[1]
        else:
            parts = SLASH.split(cleaned, maxsplit=1)
            if len(parts) == 2:
                cleaned = parts[0]
                extra = [a.strip() for a in parts[1].split(",")]
        channel_artist = CHANNEL_NOISE.sub("", channel).strip()
        artists += [channel_artist] + extra

    names = [cleaned]
    main = re.sub(r"\s*\([^)]*\)", "", cleaned).strip()
    if not main.isascii():
        names += [main] + [p.strip() for p in re.findall(r"\(([^)]*)\)", cleaned)]

    artists = list(dict.fromkeys(a for a in artists if a))
    return list(dict.fromkeys(n for n in names if n)), artists, is_topic

def fold_accent(c):
    base = unicodedata.normalize("NFKD", c)[0]
    return base if base.isascii() else c

def normalize(text):
    text = "".join(fold_accent(c) for c in unicodedata.normalize("NFKC", text))
    text = FEAT.sub("", text.lower())
    text = re.sub(r"\s+-\s+.*(remaster|version|edit|mix|mono|stereo).*$", "", text) 
    text = re.sub(r"[^\w\s]", "", text.replace("&", "and"))
    return re.sub(r"\s+", " ", text).strip()

def similar(a, b):
    if not a or not b:
        return 0
    return SequenceMatcher(None, a, b).ratio()

def title_points(name, sp_title):
    yt_title = normalize(name)
    if yt_title and yt_title == sp_title:
        return 4
    if similar(yt_title, sp_title) >= 0.8 or (sp_title and sp_title in yt_title):
        return 2
    return 0

def score(video, names, artists, is_topic, track, artist_hit=False):
    sp_title = normalize(track["name"])
    points = max((title_points(n, sp_title) for n in names), default=0)

    sp_artists = [normalize(a["name"]) for a in track["artists"]]
    yt_artists = [normalize(a) for a in artists]
    if artist_hit or any(similar(y.replace(" ", ""), s.replace(" ", "")) >= 0.9 for y in yt_artists for s in sp_artists):
        points += 3

    if video["duration_s"] is not None:
        diff = abs(video["duration_s"] - track["duration_ms"] / 1000)
        if diff <= 5:
            points += 2
        elif diff <= 15:
            points += 1

    if is_topic:
        points += 1

    return points

def find_match(sp, video):
    names, artists, is_topic = [], [], False
    for title in video.get("titles") or [video["title"]]:
        n, a, is_topic = parse_video(title, video["channel"])
        names += n
        artists += a
    names, artists = list(dict.fromkeys(names)), list(dict.fromkeys(artists))

    best, best_score = None, 0

    link = SPOTIFY_LINK.search(video.get("description") or "")
    if link:
        best, best_score = sp.track(link.group(1)), AUTO_ADD

    queries = [(f'track:"{n}" artist:"{a}"', True) for a in artists[:2] for n in names[:3]]
    # hopefully the playlist doesnt have any re-uploaders ashdoashdoiajsoid i hate edge cases
    # ok covers exist i hate my life
    queries += [(n, False) for n in names[:2]]
    queries = queries[:MAX_SEARCHES]

    for q, artist_hit in queries:
        if best_score >= AUTO_ADD:
            break
        results = sp.search(q=q, type="track", limit=5)["tracks"]["items"]
        for track in results:
            if not track:
                continue
            s = score(video, names, artists, is_topic, track, artist_hit)
            if s > best_score:
                best, best_score = track, s
        # put more accurate error handling if i have the time

    return {
        **video,
        "clean_title": names[0] if names else video["title"],
        "artist": artists[0] if artists else "",
        "spotify_id": best["id"] if best else None,
        "spotify_name": best["name"] if best else None,
        "spotify_artists": ", ".join(a["name"] for a in best["artists"]) if best else None,
        "score": best_score,
    }

def bucket(score):
    if score >= AUTO_ADD:
        return "auto"
    if score >= REVIEW:
        return "review"
    return "skip"

# spotify -> youtube

def spotify_playlist_tracks(sp, playlist_id):
    tracks = []
    page = sp.playlist_items(playlist_id, limit=100, additional_types=("track",))
    while page:
        for entry in page["items"]:
            t = entry.get("item") or entry.get("track")  # newer api renamed track -> item
            if not t or t.get("type") != "track" or not t.get("id"):
                continue  # podcasts, local files
            tracks.append({
                "track_id": t["id"],
                "track_name": t["name"],
                "artists": [a["name"] for a in t["artists"]],
                "track_artists": ", ".join(a["name"] for a in t["artists"]),
                "duration_s": t["duration_ms"] // 1000,
            })
        page = sp.next(page) if page["next"] else None
    return tracks

def find_video(yt, track):
    """One youtube search per track, it costs 100 of the 10k daily quota so no retries with other queries."""
    artist = track["artists"][0] if track["artists"] else ""
    resp = yt.search().list(part="id", q=f"{artist} {track['track_name']}", type="video", maxResults=5).execute()
    ids = [item["id"]["videoId"] for item in resp["items"]]

    # pretend the track came from spotify search so score() works unchanged
    as_spotify = {
        "name": track["track_name"],
        "artists": [{"name": a} for a in track["artists"]],
        "duration_ms": track["duration_s"] * 1000,
    }
    best, best_score = None, 0
    if ids:
        details = yt.videos().list(part="contentDetails,snippet", id=",".join(ids)).execute()["items"]
        for item in details:
            video = {
                "video_id": item["id"],
                "title": item["snippet"]["title"],
                "channel": item["snippet"]["channelTitle"],
                "duration_s": parse_iso_duration(item["contentDetails"]["duration"]),
            }
            names, artists, is_topic = parse_video(video["title"], video["channel"])
            s = score(video, names, artists, is_topic, as_spotify)
            if s > best_score:
                best, best_score = video, s

    return {
        **track,
        "video_id": best["video_id"] if best else None,
        "video_title": best["title"] if best else None,
        "video_channel": best["channel"] if best else None,
        "score": best_score,
    }
