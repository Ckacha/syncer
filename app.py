import hmac
import os
import re
import secrets
import subprocess
import sys
import automations
import matcher
import store
import auth

from datetime import timedelta
from flask import Flask, abort, flash, redirect, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix
from google_auth_oauthlib.flow import Flow
from googleapiclient.errors import HttpError
from spotipy.exceptions import SpotifyException
from transfer import API_ERRORS, DIRECTIONS, error_message, oembed_cover

from auth import (
    GOOGLE_CLIENT_SECRETS_FILE,
    YOUTUBE_SCOPES,
    NotLoggedIn,
    load_youtube_creds,
    save_youtube_creds,
    spotify_client,
    spotify_logged_in,
    spotify_oauth,
    youtube_client,
)

os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8888/google/callback")
APP_PASSWORD = os.environ.get("APP_PASSWORD")  
TASK_NAME = "Syncer automations"  
BATCH_SIZE = 10  

app = Flask(__name__)
def load_secret_key():
    """FLASK_SECRET_KEY from .env, or one generated once and saved in secrets/, so logins
    survive restarts (a new random key on every restart logs everyone out)."""
    if os.environ.get("FLASK_SECRET_KEY"):
        return os.environ["FLASK_SECRET_KEY"]
    path = os.path.join(auth.SECRETS_DIR, ".flask_secret")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    return key

app.secret_key = load_secret_key()
app.permanent_session_lifetime = timedelta(days=365)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1) 

OPEN_PATHS = {"/", "/password", "/callback", "/google/callback", "/privacy"}

@app.before_request
def require_password():
    if APP_PASSWORD and not session.get("unlocked") and request.path not in OPEN_PATHS \
            and not request.path.startswith("/static/"):
        return redirect("/password")

def api_error(e):
    app.logger.warning("API error on %s: %r", request.path, e)
    msg = error_message(e)
    if request.method == "POST":  
        flash(msg)
        return redirect(request.referrer or "/home")
    return render_template("error.html", message=msg), 503

for _cls in API_ERRORS:
    app.register_error_handler(_cls, api_error)

@app.errorhandler(NotLoggedIn)
def not_logged_in(_e):
    flash("Log in to both Spotify and YouTube first.")
    return redirect("/home")

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/password", methods=["GET", "POST"])
def password():
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", "").encode(), (APP_PASSWORD or "").encode()):
            session["unlocked"] = True
            session.permanent = True
            return redirect("/home")
        flash("Wrong password.")
    return render_template("password.html")

def uid():
    if not auth.UID_RE.fullmatch(session.get("uid") or ""):
        session["uid"] = secrets.token_urlsafe(24)
        session.permanent = True
    return session["uid"]

def connected():
    return spotify_logged_in(uid()), load_youtube_creds(uid()) is not None

def direction(slug):
    if slug not in DIRECTIONS:
        abort(404)
    if not all(connected()):
        flash("Log in to both Spotify and YouTube first.")
        abort(redirect("/home"))
    return DIRECTIONS[slug](youtube_client(uid()), spotify_client(uid()))

def save_ticks(d, db):
    keys = request.form.getlist("key")
    ticked = set(request.form.getlist("confirm"))
    store.set_approved(db, {k: k in ticked for k in keys}, d.table)
    return [k for k in keys if k in ticked]

def current_commit():
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], 
                             capture_output=True, 
                             text=True,
                             cwd=os.path.dirname(os.path.abspath(__file__)), timeout=5)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None

COMMIT = current_commit()

@app.route("/")
def landing():
    return render_template("landing.html", commit=COMMIT,
                           repo="https://github.com/Ckacha/syncer")

ACCOUNTS = {} 

def account_info(service):
    key = (uid(), service)
    if key not in ACCOUNTS:
        try:
            if service == "spotify":
                me = spotify_client(uid()).current_user()
                images = me.get("images") or []
                ACCOUNTS[key] = {"name": me.get("display_name") or me.get("id"),
                                 "pic": images[0]["url"] if images else None}
            else:
                items = youtube_client(uid()).channels().list(part="snippet", mine=True).execute().get("items", [])
                snip = items[0]["snippet"] if items else {}
                ACCOUNTS[key] = {"name": snip.get("title"),
                                 "pic": snip.get("thumbnails", {}).get("default", {}).get("url")}
        except (SpotifyException, HttpError, OSError, KeyError, NotLoggedIn):
            return None
    return ACCOUNTS[key]

@app.route("/home")
def home():
    spotify, youtube = connected()
    return render_template(
        "index.html", spotify=spotify, youtube=youtube,
        spotify_acct=account_info("spotify") if spotify else None,
        youtube_acct=account_info("youtube") if youtube else None,
    )

@app.route("/transfer")
def transfer_start():
    d = direction(request.args.get("dir", "sp2yt"))
    playlists = d.my_playlists()
    groups = {}
    for p in playlists:
        groups.setdefault(p.get("owner"), []).append(p)
    ordered = sorted(groups.items(), key=lambda kv: (kv[0] is not None, (kv[0] or "").lower()))
    groups = [("Made by you" if owner is None else f"By {owner}", items) for owner, items in ordered]
    return render_template("transfer.html", d=d, playlists=playlists, groups=groups)

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

@app.route("/cover/<track_id>")
def spotify_cover(track_id):
    if not re.fullmatch(r"[A-Za-z0-9]{22}", track_id):
        abort(404)
    db = store.connect()
    row = db.execute("SELECT spotify_image FROM matches WHERE spotify_id = ? AND spotify_image IS NOT NULL",
                     (track_id,)).fetchone()
    url = row["spotify_image"] if row else oembed_cover(track_id)
    if not url:
        abort(404)
    if not row:
        store.set_spotify_images(db, {track_id: url})
    resp = redirect(url)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp

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
        except API_ERRORS as e:
            flash(f"Song lookup stopped: {error_message(e)}")
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
    ids = list(dict.fromkeys(ids))  
    if not ids:
        flash("Nothing ticked, confirm at least one match first.")
        return redirect(f"/transfer/{slug}/{playlist_id}")

    name = request.form.get("name", "").strip() or f"Synced from {d.source}"
    try:
        url, added = d.create(name, playlist_id, ids)
    except API_ERRORS as e:
        flash(f"Import stopped: {error_message(e)} Your ticks are saved, try the import again later.")
        return redirect(f"/transfer/{slug}/{playlist_id}")

    return render_template(
        "imported.html", d=d, name=name, url=url, added=added, total=len(ids),
        back=f"/transfer/{slug}/{playlist_id}",
    )

def scheduled():
    if sys.platform == "win32":
        result = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True)
        return result.returncode == 0
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return "sync.py" in result.stdout

@app.route("/automations")
def automations_page():
    db = store.connect()
    autos = store.automations(db, uid())
    for a in autos:
        d = DIRECTIONS[a["direction"]]
        a["arrow"] = f"{d.source} → {d.dest}"
        a["dest_url"] = d.playlist_url(a["dest_id"]) if a["dest_id"] else None
        a["rules"] = automations.describe(a)
    youtube_acct = account_info("youtube") if load_youtube_creds(uid()) else None
    return render_template("automations.html", autos=autos, youtube_acct=youtube_acct, directions=DIRECTIONS.values(),
                           scheduled=scheduled(), task_name=TASK_NAME, windows=sys.platform == "win32", picks=automations.PICKS,
                           max_songs=automations.MAX_SONGS)

def read_rules(form):
    def number(name, top):
        raw = (form.get(name) or "").strip()
        return max(1, min(int(raw), top)) if raw.isdigit() else None
    pick = form.get("pick") if form.get("pick") in automations.PICKS else "newest"
    return number("count", automations.MAX_SONGS), number("days", 3650), pick

@app.route("/automations", methods=["POST"])
def automations_add():
    d, db = direction(request.form["direction"]), store.connect()
    source_id = d.playlist_id_from(request.form.get("source", ""))
    loaded = d.load(source_id) if source_id else None
    if loaded is None:
        flash(f"Couldn't open that {d.source} playlist. Check the link, and that it isn't private.")
        return redirect("/automations")
    count, days, pick = read_rules(request.form)
    rules = automations.describe({"count": count, "days": days, "pick": pick})
    dest_name = request.form.get("dest_name", "").strip() or f"{loaded[0]} ({rules})"
    claim("google", google_account_id)  
    store.add_automation(db, uid(), d.slug, source_id, loaded[0], dest_name, count, days, pick)
    flash(f'Added. Click "Run now" to create "{dest_name}" and fill it.')
    return redirect("/automations")

@app.route("/automations/<int:auto_id>/edit", methods=["GET", "POST"])
def automations_edit(auto_id):
    db = store.connect()
    auto = store.get_automation(db, auto_id, uid())
    if not auto:
        return redirect("/automations")
    if request.method == "POST":
        count, days, pick = read_rules(request.form)
        dest_name = request.form.get("dest_name", "").strip() or auto["dest_name"]
        store.update_automation(db, auto_id, count=count, days=days, pick=pick, dest_name=dest_name)
        msg = "Saved. The playlist changes on the next run."
        if dest_name != auto["dest_name"] and auto["dest_id"]:
            msg += " The new name only applies if the playlist gets recreated, rename the existing one in the app."
        flash(msg)
        return redirect("/automations")
    return render_template("automation_edit.html", a=auto, picks=automations.PICKS,
                           max_songs=automations.MAX_SONGS)

@app.route("/automations/<int:auto_id>/run", methods=["POST"])
def automations_run(auto_id):
    db = store.connect()
    auto = store.get_automation(db, auto_id, uid())
    if auto:
        direction(auto["direction"])
        flash(f"{auto['dest_name']}: {automations.run(auto, youtube_client(uid()), spotify_client(uid()), db)}")
    return redirect("/automations")

@app.route("/automations/<int:auto_id>/pause", methods=["POST"])
def automations_pause(auto_id):
    db = store.connect()
    auto = store.get_automation(db, auto_id, uid())
    if auto:
        paused = not auto["paused"]
        store.set_paused(db, auto_id, uid(), paused)
        flash(f"{auto['dest_name']}: " + ("paused, it won't run until you resume it." if paused else "resumed."))
    return redirect("/automations")

@app.route("/automations/<int:auto_id>/delete", methods=["POST"])
def automations_delete(auto_id):
    store.delete_automation(store.connect(), auto_id, uid())
    flash("Automation deleted. The playlist it made is still there.")
    return redirect("/automations")

@app.route("/login/spotify")
def login_spotify():
    session["spotify_state"] = secrets.token_urlsafe(16)
    return redirect(spotify_oauth(uid(), state=session["spotify_state"]).get_authorize_url())

@app.route("/callback")
def spotify_callback():
    if request.args.get("state") != session.pop("spotify_state", None):
        abort(400, "State mismatch, try logging in again.")
    if "error" in request.args:
        abort(400, request.args["error"])
    me = uid()
    spotify_oauth(me).get_access_token(request.args["code"], as_dict=False)
    claim("spotify", lambda: spotify_client(me).current_user()["id"])
    ACCOUNTS.clear()
    return redirect("/home")

def claim(provider, get_account_id):
    me = uid()
    try:
        account_id = get_account_id()
    except (*API_ERRORS, NotLoggedIn, KeyError, IndexError):
        return  
    if not account_id:
        return
    owner = store.claim_account(store.connect(), provider, account_id, me)
    if owner != me:
        auth.merge_user(me, owner)
        session["uid"] = owner

def google_account_id():
    items = youtube_client(uid()).channels().list(part="id", mine=True).execute().get("items", [])
    return items[0]["id"]

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
    save_youtube_creds(uid(), flow.credentials)
    claim("google", google_account_id)
    ACCOUNTS.clear()
    return redirect("/home")

@app.route("/logout/<service>")
def logout(service):
    if service in auth.TOKEN_FILES:
        auth.logout(uid(), service)
    ACCOUNTS.pop((uid(), service), None)
    return redirect("/home")

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8888, debug=True)
