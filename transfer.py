"""The two transfer directions. Both expose the same methods so app.py and the
review page don't care which way songs are going."""
import re

from googleapiclient.errors import HttpError
from spotipy.exceptions import SpotifyException

import matcher

YT_VIDEO = "https://www.youtube.com/watch?v={}"
SP_TRACK = "https://open.spotify.com/track/{}"


def blocked_message(e):
    """User facing text if e is a rate limit / quota error, otherwise None (a real bug)."""
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
        """Returns (title, items) or None if the playlist can't be read."""
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

    def create(self, name, source_id, ids):
        """Returns (playlist url, how many got added)."""
        new = self.sp.current_user_playlist_create(
            name, public=False,
            description=f"Imported from https://www.youtube.com/playlist?list={source_id}",
        )
        for i in range(0, len(ids), 100):  # spotify takes max 100 per request
            self.sp.playlist_add_items(new["id"], ids[i:i + 100])
        return new["external_urls"]["spotify"], len(ids)


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

    def create(self, name, source_id, ids):
        new = self.yt.playlists().insert(part="snippet,status", body={
            "snippet": {"title": name,
                        "description": f"Imported from https://open.spotify.com/playlist/{source_id}"},
            "status": {"privacyStatus": "private"},
        }).execute()
        url = f"https://www.youtube.com/playlist?list={new['id']}"
        added = 0
        for vid in ids:  # youtube only adds one video per request
            try:
                self.yt.playlistItems().insert(part="snippet", body={"snippet": {
                    "playlistId": new["id"],
                    "resourceId": {"kind": "youtube#video", "videoId": vid},
                }}).execute()
            except HttpError as e:
                if blocked_message(e):
                    break  # quota ran out midway, keep what got added
                raise
            added += 1
        return url, added


DIRECTIONS = {d.slug: d for d in (YouTubeToSpotify, SpotifyToYouTube)}
