import re
import matcher

from googleapiclient.errors import HttpError
from spotipy.exceptions import SpotifyException

YT_VIDEO = "https://www.youtube.com/watch?v={}"
SP_TRACK = "https://open.spotify.com/track/{}"


def blocked_message(e):
    if isinstance(e, SpotifyException) and e.http_status == 429:
        wait = int((e.headers or {}).get("Retry-After", 0))
        when = f"{wait // 3600}h {wait % 3600 // 60}m" if wait else "a while"
        return f"Spotify rate limit hit. Try again in {when}, everything scanned so far is saved."
    if isinstance(e, HttpError) and e.resp.status == 403 and b"quotaExceeded" in e.content:
        return "YouTube daily quota used up, it resets at midnight Pacific time. Everything scanned so far is saved."
    return None


class YouTubeToSpotify:
    slug, source, dest = "yt2sp", "YouTube", "Spotify"
    table = "matches"
    cost_note = ("Scanning uses Spotify searches. No daily limit, but going too fast "
                 "gets the app blocked for hours.")

    def __init__(self, yt, sp):
        self.yt, self.sp = yt, sp

    @staticmethod
    def playlist_id_from(text):
        """Full playlist/video url with ?list=... or a bare playlist id."""
        text = text.strip()
        m = re.search(r"[?&]list=([\w-]+)", text)
        if m:
            return m.group(1)
        return text if re.fullmatch(r"[\w-]+", text) else None

    def my_playlists(self):
        playlists = []
        req = self.yt.playlists().list(part="snippet", mine=True, maxResults=50)
        while req:
            resp = req.execute()
            playlists += [{"id": p["id"], "title": p["snippet"]["title"]} for p in resp["items"]]
            req = self.yt.playlists().list_next(req, resp)
        return playlists

    def load(self, playlist_id):
        meta = self.yt.playlists().list(part="snippet", id=playlist_id).execute()["items"]
        if not meta:
            return None
        return meta[0]["snippet"]["title"], matcher.youtube_playlist_videos(self.yt, playlist_id)

    def key(self, item):
        return item["video_id"]

    def find(self, item):
        return matcher.find_match(self.sp, item)

    def row(self, item, match):
        row = {"key": item["video_id"], "src_title": item["title"],
               "src_url": YT_VIDEO.format(item["video_id"]), "src_sub": item["channel"]}
        if match:
            row.update(dst_id=match["spotify_id"], dst_title=match["spotify_name"],
                       dst_url=SP_TRACK.format(match["spotify_id"]), dst_sub=match["spotify_artists"])
        return row

    def dest_id(self, match):
        return match["spotify_id"]

    def import_cost(self, count):
        return ""

    def create_empty(self, name, source_id):
        new = self.sp.current_user_playlist_create(
            name, public=False,
            description=f"Imported from https://www.youtube.com/playlist?list={source_id}",
        )
        return new["id"], new["external_urls"]["spotify"]

    def replace(self, dest_id, ids):
        self.sp.playlist_replace_items(dest_id, ids[:100]) 
        for i in range(100, len(ids), 100):
            self.sp.playlist_add_items(dest_id, ids[i:i + 100])
        return len(ids)

    @staticmethod
    def playlist_url(dest_id):
        return f"https://open.spotify.com/playlist/{dest_id}"

    def create(self, name, source_id, ids):
        """Returns (playlist url, how many got added)."""
        dest_id, url = self.create_empty(name, source_id)
        return url, self.replace(dest_id, ids)


class SpotifyToYouTube:
    slug, source, dest = "sp2yt", "Spotify", "YouTube"
    table = "reverse_matches"
    cost_note = ("Each scan of 10 uses about 1,010 of your 10,000 daily YouTube units "
                 "(a YouTube search costs 100).")

    def __init__(self, yt, sp):
        self.yt, self.sp = yt, sp

    @staticmethod
    def playlist_id_from(text):
        """open.spotify.com/playlist/<id>, spotify:playlist:<id> or a bare id."""
        m = re.search(r"playlist[/:]([A-Za-z0-9]{22})", text) or re.fullmatch(r"\s*([A-Za-z0-9]{22})\s*", text)
        return m.group(1) if m else None

    def my_playlists(self):
        playlists = []
        page = self.sp.current_user_playlists(limit=50)
        while page:
            playlists += [{"id": p["id"], "title": p["name"]} for p in page["items"] if p]
            page = self.sp.next(page) if page["next"] else None
        return playlists

    def load(self, playlist_id):
        try:
            title = self.sp.playlist(playlist_id, fields="name")["name"]
        except SpotifyException as e:
            if e.http_status in (403, 404):
                return None
            raise
        return title, matcher.spotify_playlist_tracks(self.sp, playlist_id)

    def key(self, item):
        return item["track_id"]

    def find(self, item):
        return matcher.find_video(self.yt, item)

    def row(self, item, match):
        row = {"key": item["track_id"], "src_title": item["track_name"],
               "src_url": SP_TRACK.format(item["track_id"]), "src_sub": item["track_artists"]}
        if match:
            row.update(dst_id=match["video_id"], dst_title=match["video_title"],
                       dst_url=YT_VIDEO.format(match["video_id"]), dst_sub=match["video_channel"])
        return row

    def dest_id(self, match):
        return match["video_id"]

    def import_cost(self, count):
        return f"Uses about {50 + 50 * count:,} of your 10,000 daily YouTube units (50 per song)."

    def create_empty(self, name, source_id):
        new = self.yt.playlists().insert(part="snippet,status", body={
            "snippet": {"title": name,
                        "description": f"Imported from https://open.spotify.com/playlist/{source_id}"},
            "status": {"privacyStatus": "private"},
        }).execute()
        return new["id"], self.playlist_url(new["id"])

    def replace(self, dest_id, ids):
        current = {} 
        req = self.yt.playlistItems().list(part="snippet", playlistId=dest_id, maxResults=50)
        while req:
            resp = req.execute()
            for item in resp["items"]:
                current[item["snippet"]["resourceId"]["videoId"]] = item["id"]
            req = self.yt.playlistItems().list_next(req, resp)

        wanted = set(ids)
        try:
            for vid, item_id in current.items():
                if vid not in wanted:
                    self.yt.playlistItems().delete(id=item_id).execute()
            for vid in ids: 
                if vid not in current:
                    self.yt.playlistItems().insert(part="snippet", body={"snippet": {
                        "playlistId": dest_id,
                        "resourceId": {"kind": "youtube#video", "videoId": vid},
                    }}).execute()
                    current[vid] = None
        except HttpError as e:
            if not blocked_message(e):
                raise
        return len(wanted & current.keys())

    @staticmethod
    def playlist_url(dest_id):
        return f"https://www.youtube.com/playlist?list={dest_id}"

    def create(self, name, source_id, ids):
        dest_id, url = self.create_empty(name, source_id)
        return url, self.replace(dest_id, ids)


DIRECTIONS = {d.slug: d for d in (YouTubeToSpotify, SpotifyToYouTube)}
