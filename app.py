import os
import secrets

from flask import Flask, abort, redirect, render_template, request, session
from google_auth_oauthlib.flow import Flow

from auth import (
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

def youtube_playlist_names():
    yt = youtube_client()
    names = []
    req = yt.playlists().list(part="snippet", mine=True, maxResults=50)
    while req:
        resp = req.execute()
        names += [p["snippet"]["title"] for p in resp["items"]]
        req = yt.playlists().list_next(req, resp)
    return names

@app.route("/")
def home():
    spotify = spotify_playlist_names() if spotify_logged_in() else None
    youtube = youtube_playlist_names() if load_youtube_creds() else None
    return render_template("index.html", spotify=spotify, youtube=youtube)

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
        os.environ["GOOGLE_CLIENT_SECRETS_FILE"],
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
