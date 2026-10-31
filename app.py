import os
import re
import secrets

from flask import Flask, abort, redirect, render_template, request, session
from google_auth_oauthlib.flow import Flow
from spotipy.exceptions import SpotifyException

import matcher
import store
from auth import (
    GOOGLE_CLIENT_SECRETS_FILE,
    YOUTUBE_SCOPES,
    load_youtube_creds,
    save_youtube_creds,
    spotify_client,
    spotify_logged_in,
    spotify_oauth,
    youtube_client,
)

os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

GOOGLE_REDIRECT_URI = "http://127.0.0.1:8888/google/callback"
BATCH_SIZE = 10    
BATCH_DELAY_S = 10  

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

def spotify_playlist_names():
    sp = spotify_client()
    names = []
    page = sp.current_user_playlists(limit=50)
    while page:
        names += [p["name"] for p in page["items"]]
        page = sp.next(page) if page["next"] else None
    return names

def youtube_playlists():
    yt = youtube_client()
    playlists = []
    req = yt.playlists().list(part="snippet", mine=True, maxResults=50)
    while req:
        resp = req.execute()
        playlists += [{"id": p["id"], "title": p["snippet"]["title"]} for p in resp["items"]]
        req = yt.playlists().list_next(req, resp)
    return playlists

@app.route("/")
def home():
    spotify = spotify_playlist_names() if spotify_logged_in() else None
    youtube = youtube_playlists() if load_youtube_creds() else None
    return render_template("index.html", spotify=spotify, youtube=youtube)

def playlist_id_from(text):
    """Accepts a full playlist/video url with ?list=... or a bare playlist id."""
    text = text.strip()
    m = re.search(r"[?&]list=([\w-]+)", text)
    if m:
        return m.group(1)
    return text if re.fullmatch(r"[\w-]+", text) else None

@app.route("/preview")
def preview_from_link():
    playlist_id = playlist_id_from(request.args.get("playlist", ""))
    if not playlist_id:
        abort(400, "That doesn't look like a YouTube playlist link.")
    return redirect(f"/preview/{playlist_id}")

@app.route("/preview/<playlist_id>")
def preview(playlist_id):
    if not (spotify_logged_in() and load_youtube_creds()):
        return redirect("/")
    yt, sp, db = youtube_client(), spotify_client(), store.connect()

    meta = yt.playlists().list(part="snippet", id=playlist_id).execute()["items"]
    if not meta:
        abort(404, "Playlist not found. It might be private, or a YouTube Mix (those can't be read).")
    playlist_title = meta[0]["snippet"]["title"]

    videos = matcher.youtube_playlist_videos(yt, playlist_id)
    if request.args.get("rescan") == "1":
        store.delete_matches(db, [v["video_id"] for v in videos])
        return redirect(f"/preview/{playlist_id}")

    matches, rate_limited, searched = [], None, 0
    for video in videos:
        match = store.get_match(db, video["video_id"])
        if match is None:
            if rate_limited or searched >= BATCH_SIZE:
                continue
            searched += 1
            try:
                match = matcher.find_match(sp, video)
            except SpotifyException as e:
                if e.http_status != 429:
                    raise
                wait = int((e.headers or {}).get("Retry-After", 0))
                rate_limited = f"{wait // 3600}h {wait % 3600 // 60}m" if wait else "a while"
                continue
            store.save_match(db, match)
        match["bucket"] = matcher.bucket(match["score"])
        approved = match.get("approved")
        match["checked"] = bool(match["spotify_id"]) and (
            approved == 1 or (approved is None and match["bucket"] == "auto")
        )
        matches.append(match)

    counts = {b: sum(m["bucket"] == b for m in matches) for b in ("auto", "review", "skip")}
    return render_template(
        "preview.html", playlist_title=playlist_title, matches=matches, counts=counts,
        rate_limited=rate_limited, unscanned=len(videos) - len(matches), total=len(videos),
        batch_delay=BATCH_DELAY_S, playlist_id=playlist_id,
    )

@app.route("/import/<playlist_id>", methods=["POST"])
def import_playlist(playlist_id):
    if not spotify_logged_in():
        return redirect("/")
    sp, db = spotify_client(), store.connect()

    video_ids = request.form.getlist("video")
    ticked = set(request.form.getlist("confirm"))
    store.set_approved(db, {vid: vid in ticked for vid in video_ids})

    track_ids = []
    for vid in video_ids:
        match = store.get_match(db, vid)
        if vid in ticked and match and match["spotify_id"]:
            track_ids.append(match["spotify_id"])
    track_ids = list(dict.fromkeys(track_ids)) 
    if not track_ids:
        abort(400, "Nothing ticked, confirm at least one match first.")

    name = request.form.get("name", "").strip() or "Synced from YouTube"
    try:
        new = sp.current_user_playlist_create(
            name, public=False,
            description=f"Imported from https://www.youtube.com/playlist?list={playlist_id}",
        )
        for i in range(0, len(track_ids), 100):
            sp.playlist_add_items(new["id"], track_ids[i:i + 100])
    except SpotifyException as e:
        if e.http_status != 429:
            raise
        abort(429, "Spotify rate limit hit, your ticks are saved. Try the import again later.")

    return render_template(
        "imported.html", name=name, count=len(track_ids),
        url=new["external_urls"]["spotify"], playlist_id=playlist_id,
    )

@app.route("/login/spotify")
def login_spotify():
    session["spotify_state"] = secrets.token_urlsafe(16)
    return redirect(spotify_oauth(state=session["spotify_state"]).get_authorize_url())

@app.route("/callback")
def spotify_callback():
    if request.args.get("state") != session.pop("spotify_state", None):
        abort(400, "State mismatch, try logging in again.")
    if "error" in request.args:
        abort(400, request.args["error"])
    spotify_oauth().get_access_token(request.args["code"], as_dict=False)
    return redirect("/")

def google_flow(state=None):
    return Flow.from_client_secrets_file(
        GOOGLE_CLIENT_SECRETS_FILE,
        scopes=YOUTUBE_SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
        state=state,
    )

@app.route("/login/youtube")
def login_youtube():
    flow = google_flow()
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    session["google_state"] = state
    session["google_verifier"] = flow.code_verifier
    return redirect(url)

@app.route("/google/callback")
def google_callback():
    flow = google_flow(state=session.pop("google_state", None))
    flow.code_verifier = session.pop("google_verifier", None)
    flow.fetch_token(authorization_response=request.url)
    save_youtube_creds(flow.credentials)
    return redirect("/")

@app.route("/logout/<service>")
def logout(service):
    path = {"spotify": ".cache", "youtube": "token.json"}.get(service)
    if path and os.path.exists(path):
        os.remove(path)
    return redirect("/")

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8888, debug=True)
