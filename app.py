import os
import secrets
import subprocess

from flask import Flask, abort, flash, redirect, render_template, request, session
from google_auth_oauthlib.flow import Flow
from googleapiclient.errors import HttpError
from spotipy.exceptions import SpotifyException

import automations
import matcher
import store
from transfer import DIRECTIONS, blocked_message
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
TASK_NAME = "Syncer automations"  # windows task scheduler entry that runs sync.py
BATCH_SIZE = 10  # songs searched per click of "Scan next"

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

def connected():
    return spotify_logged_in(), load_youtube_creds() is not None

def direction(slug):
    if slug not in DIRECTIONS:
        abort(404)
    if not all(connected()):
        flash("Log in to both Spotify and YouTube first.")
        abort(redirect("/"))
    return DIRECTIONS[slug](youtube_client(), spotify_client())

def save_ticks(d, db):
    """The form lists every scanned row in playlist order, ticked ones are confirmed."""
    keys = request.form.getlist("key")
    ticked = set(request.form.getlist("confirm"))
    store.set_approved(db, {k: k in ticked for k in keys}, d.table)
    return [k for k in keys if k in ticked]

@app.route("/")
def home():
    spotify, youtube = connected()
    return render_template("index.html", spotify=spotify, youtube=youtube)

@app.route("/transfer")
def transfer_start():
    slug = request.args.get("dir")
    if slug is None:
        return render_template("transfer.html", d=None, directions=DIRECTIONS.values())
    d = direction(slug)
    return render_template("transfer.html", d=d, playlists=d.my_playlists())

@app.route("/transfer/<slug>/open")
def transfer_open(slug):
    d = direction(slug)
    playlist_id = d.playlist_id_from(request.args.get("playlist", ""))
    if not playlist_id:
        flash(f"That doesn't look like a {d.source} playlist link.")
        return redirect(f"/transfer?dir={slug}")
    return redirect(f"/transfer/{slug}/{playlist_id}")

@app.route("/transfer/<slug>/<playlist_id>")
def transfer_review(slug, playlist_id):
    d, db = direction(slug), store.connect()
    loaded = d.load(playlist_id)
    if loaded is None:
        flash(f"Couldn't open that {d.source} playlist. It might be private"
              + (", or a YouTube Mix (those can't be read)." if d.source == "YouTube" else "."))
        return redirect(f"/transfer?dir={slug}")
    title, items = loaded

    rows = []
    for item in items:
        match = store.get_match(db, d.key(item), d.table)
        row = d.row(item, match)
        row["scanned"] = match is not None
        if match:
            row["score"] = match["score"]
            row["bucket"] = matcher.bucket(match["score"])
            # ticked by default only for auto-adds, after that whatever the user last saved wins
            approved = match.get("approved")
            row["checked"] = bool(row["dst_id"]) and (
                approved == 1 or (approved is None and row["bucket"] == "auto")
            )
        rows.append(row)

    scanned = [r for r in rows if r["scanned"]]
    counts = {b: sum(r["bucket"] == b for r in scanned) for b in ("auto", "review", "skip")}
    ticked = sum(r["checked"] for r in scanned)
    return render_template(
        "preview.html", d=d, playlist_id=playlist_id, title=title, rows=rows,
        counts=counts, scanned=len(scanned), ticked=ticked, batch=BATCH_SIZE,
    )

@app.route("/transfer/<slug>/<playlist_id>/scan", methods=["POST"])
def transfer_scan(slug, playlist_id):
    d, db = direction(slug), store.connect()
    save_ticks(d, db)
    loaded = d.load(playlist_id)
    if loaded is None:
        return redirect(f"/transfer/{slug}/{playlist_id}")
    todo = [i for i in loaded[1] if store.get_match(db, d.key(i), d.table) is None][:BATCH_SIZE]
    for item in todo:
        try:
            store.save_match(db, d.find(item), d.table)
        except (SpotifyException, HttpError) as e:
            msg = blocked_message(e)
            if not msg:
                raise
            flash(msg)
            break
    return redirect(f"/transfer/{slug}/{playlist_id}")

@app.route("/transfer/<slug>/<playlist_id>/rescan", methods=["POST"])
def transfer_rescan(slug, playlist_id):
    d, db = direction(slug), store.connect()
    loaded = d.load(playlist_id)
    if loaded:
        store.delete_matches(db, [d.key(i) for i in loaded[1]], d.table)
    return redirect(f"/transfer/{slug}/{playlist_id}")

@app.route("/transfer/<slug>/<playlist_id>/import", methods=["POST"])
def transfer_import(slug, playlist_id):
    d, db = direction(slug), store.connect()
    ids = []
    for key in save_ticks(d, db):
        match = store.get_match(db, key, d.table)
        if match and d.dest_id(match):
            ids.append(d.dest_id(match))
    ids = list(dict.fromkeys(ids))  # same song twice in the source playlist
    if not ids:
        flash("Nothing ticked, confirm at least one match first.")
        return redirect(f"/transfer/{slug}/{playlist_id}")

    name = request.form.get("name", "").strip() or f"Synced from {d.source}"
    try:
        url, added = d.create(name, playlist_id, ids)
    except (SpotifyException, HttpError) as e:
        msg = blocked_message(e)
        if not msg:
            raise
        flash(msg + " Your ticks are saved, try the import again later.")
        return redirect(f"/transfer/{slug}/{playlist_id}")

    return render_template(
        "imported.html", d=d, name=name, url=url, added=added, total=len(ids),
        back=f"/transfer/{slug}/{playlist_id}",
    )

def scheduled():
    """Is the windows task that runs sync.py set up?"""
    result = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True)
    return result.returncode == 0

@app.route("/automations")
def automations_page():
    db = store.connect()
    autos = store.automations(db)
    for a in autos:
        d = DIRECTIONS[a["direction"]]
        a["arrow"] = f"{d.source} → {d.dest}"
        a["dest_url"] = d.playlist_url(a["dest_id"]) if a["dest_id"] else None
    return render_template("automations.html", autos=autos, directions=DIRECTIONS.values(),
                           scheduled=scheduled(), task_name=TASK_NAME)

@app.route("/automations", methods=["POST"])
def automations_add():
    d, db = direction(request.form["direction"]), store.connect()
    source_id = d.playlist_id_from(request.form.get("source", ""))
    loaded = d.load(source_id) if source_id else None
    if loaded is None:
        flash(f"Couldn't open that {d.source} playlist. Check the link, and that it isn't private.")
        return redirect("/automations")
    count = max(1, min(int(request.form.get("count") or 30), 100))
    dest_name = request.form.get("dest_name", "").strip() or f"{loaded[0]} (newest {count})"
    store.add_automation(db, d.slug, source_id, loaded[0], dest_name, count)
    flash(f'Added. Click "Run now" to create "{dest_name}" and fill it.')
    return redirect("/automations")

@app.route("/automations/<int:auto_id>/run", methods=["POST"])
def automations_run(auto_id):
    db = store.connect()
    auto = store.get_automation(db, auto_id)
    if auto:
        direction(auto["direction"])  # login check
        flash(f"{auto['dest_name']}: {automations.run(auto, youtube_client(), spotify_client(), db)}")
    return redirect("/automations")

@app.route("/automations/<int:auto_id>/delete", methods=["POST"])
def automations_delete(auto_id):
    store.delete_automation(store.connect(), auto_id)
    flash("Automation deleted. The playlist it made is still there.")
    return redirect("/automations")

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
