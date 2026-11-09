import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time
import automations
import lean
import matcher
import store
import auth

from datetime import datetime, timedelta
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix
from google_auth_oauthlib.flow import Flow
from googleapiclient.errors import HttpError
from spotipy.exceptions import SpotifyException
from transfer import API_ERRORS, DIRECTIONS, error_message, lookup_stopped, oembed_cover

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
BATCH_SIZE = {"yt2sp": 30, "sp2yt": 10}

app = Flask(__name__)
def load_secret_key():
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

def review_row(d, db, item):
    match = store.get_match(db, d.key(item), d.table)
    row = d.row(item, match)
    row["scanned"] = match is not None
    if match:
        row["score"] = match["score"]
        row["version"] = ", ".join(sorted(matcher.excluded(match))) 
        row["bucket"] = "skip" if row["version"] else matcher.bucket(match["score"])
        approved = match.get("approved")
        row["checked"] = bool(row["dst_id"]) and not row["version"] and (
            approved == 1 or (approved is None and row["bucket"] == "auto")
        )
    return row

@app.route("/transfer/<slug>/<playlist_id>")
def transfer_review(slug, playlist_id):
    d, db = direction(slug), store.connect()
    loaded = d.load(playlist_id)
    if loaded is None:
        flash(f"Couldn't open that {d.source} playlist. It might be private"
              + (", or a YouTube Mix (those can't be read)." if d.source == "YouTube" else "."))
        return redirect(f"/transfer?dir={slug}")
    title, items = loaded

    rows = [review_row(d, db, item) for item in items]

    scanned = [r for r in rows if r["scanned"]]
    counts = {b: sum(r["bucket"] == b for r in scanned) for b in ("auto", "review", "skip")}
    counts["pending"] = len(rows) - len(scanned)
    ticked = sum(r["checked"] for r in scanned)
    return render_template(
        "preview.html", d=d, playlist_id=playlist_id, title=title, rows=rows,
        counts=counts, scanned=len(scanned), ticked=ticked, batch=BATCH_SIZE[d.slug],
    )

@app.route("/test", methods=["GET", "POST"])
def quota_test():
    if request.method == "GET":
        return render_template("test.html", result=None)
    link = request.form.get("playlist", "").strip()
    sp_id = DIRECTIONS["sp2yt"].playlist_id_from(link) if "spotify" in link or re.fullmatch(r"\s*[A-Za-z0-9]{22}\s*", link) else None
    yt_id = None if sp_id else DIRECTIONS["yt2sp"].playlist_id_from(link)
    if not (sp_id or yt_id):
        flash("Paste a YouTube or Spotify playlist link.")
        return redirect("/test")
    db, me = store.connect(), uid()
    lean.ensure_tables(db)
    rows, spent = [], lean.Meter()

    def measure(fn):
        m = lean.Meter()
        out = fn(lean.metered_youtube(me, m))
        spent.units += m.units
        for k, v in m.calls.items():
            spent.calls[k] = spent.calls.get(k, 0) + v
        return m.units, out

    if yt_id:
        sp = spotify_client(me)
        lean.clear_cache()
        def old_load(yt):
            meta = yt.playlists().list(part="snippet", id=yt_id).execute()["items"]
            return (meta[0]["snippet"]["title"], matcher.youtube_playlist_videos(yt, yt_id)) if meta else None
        cur_load, loaded = measure(old_load)
        if loaded is None:
            flash("Couldn't open that YouTube playlist.")
            return redirect("/test")
        
        title, items = loaded
        unscanned = sum(store.get_match(db, i["video_id"]) is None for i in items)
        clicks = max(1, -(-unscanned // BATCH_SIZE["yt2sp"]))
        lean_first, _ = measure(lambda yt: lean.load_youtube_playlist(yt, db, yt_id))
        lean_cached, _ = measure(lambda yt: lean.load_youtube_playlist(yt, db, yt_id))
        lean.clear_cache()
        lean_warm, _ = measure(lambda yt: lean.load_youtube_playlist(yt, db, yt_id))
        check, _ = measure(lambda yt: lean.playlist_changed(yt, db, yt_id))

        rows += [
            ("Open the review page once", cur_load, lean_first,
             "measured", "first visit; details get saved"),
            ("Open it again (within 10 min)", cur_load, lean_cached, "measured", "kept in memory"),
            ("Open it again (later)", cur_load, lean_warm, "measured", "details already saved"),
            (f"Scan the {unscanned} unscanned songs ({clicks} clicks)", cur_load * (1 + 2 * clicks),
             lean_first + 2 * clicks * lean_cached, "from measured loads",
             "the old version loaded the playlist twice per click"),
            ("Automation: 24 hourly runs", 24 * cur_load, 24 * check + lean_warm,
             "from measured loads", f"now: {check}-unit change check per hour + one full load a day"),
        ]
    else:
        loaded = DIRECTIONS["sp2yt"](None, spotify_client(me)).load(sp_id)
        if loaded is None:
            flash("Couldn't open that Spotify playlist.")
            return redirect("/test")
        
        title, items = loaded
        need = [t for t in items if store.get_match(db, t["track_id"], "reverse_matches") is None]
        reuse = sum(lean.reuse_reverse_match(db, t) is not None for t in need)
        search = 101  
        rows += [
            (f"Scan the {len(need)} unscanned songs", len(need) * search, (len(need) - reuse) * search,
             "counted, not run", f"{reuse} already matched YouTube → Spotify, no search needed"),
            (f"Add all {len(items)} songs to YouTube", len(items) * 50, len(items) * 50,
             "counted, not run", "playlistItems.insert is 50 each either way"),
        ]
    return render_template("test.html", title=title, rows=rows, spent=spent, link=link,
                           result=True, kind="YouTube" if yt_id else "Spotify")

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

@app.route("/transfer/<slug>/tick", methods=["POST"])
def transfer_tick(slug):
    if slug not in DIRECTIONS:
        abort(404)
    if not all(connected()):
        return jsonify(error="Log in to both Spotify and YouTube first."), 403
    key = (request.form.get("key") or "").strip()
    if not key:
        return jsonify(error="No song given."), 400
    store.set_approved(store.connect(), {key: request.form.get("on") == "1"}, DIRECTIONS[slug].table)
    return jsonify(ok=True)

@app.route("/transfer/<slug>/<playlist_id>/scan", methods=["POST"])
def transfer_scan(slug, playlist_id):
    d, db = direction(slug), store.connect()
    save_ticks(d, db)
    loaded = d.load(playlist_id)
    if loaded is None:
        return redirect(f"/transfer/{slug}/{playlist_id}")
    try:
        count = max(1, min(int(request.form.get("count", "")), BATCH_SIZE[d.slug]))
    except ValueError:
        count = BATCH_SIZE[d.slug]
    unscanned = [i for i in loaded[1] if store.get_match(db, d.key(i), d.table) is None]
    done, stopped = 0, False
    for item in unscanned[:count]:
        try:
            store.save_match(db, d.find(item), d.table)
            done += 1
        except API_ERRORS as e:
            flash(lookup_stopped(e))
            stopped = True
            break
    if request.headers.get("X-Scan") == "1":
        return jsonify(done=done, left=len(unscanned) - done, stopped=stopped)
    return redirect(f"/transfer/{slug}/{playlist_id}")

@app.route("/transfer/<slug>/<playlist_id>/rescan/<key>", methods=["POST"])
def transfer_rescan_one(slug, playlist_id, key):
    d, db = direction(slug), store.connect()
    save_ticks(d, db)
    loaded = d.load(playlist_id)
    item = next((i for i in (loaded[1] if loaded else []) if d.key(i) == key), None)
    in_page = request.headers.get("X-Scan") == "1"  
    if item is None:
        if in_page:
            return jsonify(error="That song isn't in this playlist anymore.")
        flash("That song isn't in this playlist anymore.")
        return redirect(f"/transfer/{slug}/{playlist_id}")
    error = None
    try:
        store.save_match(db, d.find(item), d.table)
    except API_ERRORS as e:
        error = lookup_stopped(e)
    if in_page:
        html = render_template("_song_row.html", r=review_row(d, db, item), d=d, playlist_id=playlist_id)
        return jsonify(html=html, error=error)
    if error:
        flash(error)
    return redirect(f"/transfer/{slug}/{playlist_id}#row-{key}")

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

MY_PLAYLISTS = {}  
MY_PLAYLISTS_SECONDS = 600

@app.route("/my-playlists.json")
def my_playlists_json():
    me = uid()
    hit = MY_PLAYLISTS.get(me)
    if hit and time.time() - hit[0] < MY_PLAYLISTS_SECONDS and not request.args.get("fresh"):
        return jsonify(hit[1])
    playlists, problems = [], []
    for service in automations.SERVICES:
        try:
            if service == "YouTube":
                found = DIRECTIONS["yt2sp"](youtube_client(me), None).my_playlists()
            else:
                found = DIRECTIONS["sp2yt"](None, spotify_client(me)).my_playlists()
        except NotLoggedIn:
            problems.append(f"Log into {service} on Home to see your {service} playlists.")
            continue
        except API_ERRORS as e:
            problems.append(f"{service}: {error_message(e)}")
            continue
        playlists += [{"service": service, "id": p["id"], "title": p["title"], "image": p.get("image"),
                       "owner": p.get("owner"), "url": automations.source_url(service, p["id"])} for p in found]
    result = {"playlists": playlists, "problems": problems}
    if not problems:
        MY_PLAYLISTS[me] = (time.time(), result)
    return jsonify(result)

@app.route("/automations")
def automations_page():
    db = store.connect()
    autos = store.automations(db, uid())
    for a in autos:
        a["sources"] = store.sources(db, a["id"])
        for src in a["sources"]:
            src["url"] = automations.source_url(src["service"], src["playlist_id"])
        a["dest_service"] = a.get("dest_service") or "Spotify"
        a["dest_url"] = automations.source_url(a["dest_service"], a["dest_id"]) if a["dest_id"] else None
        if a.get("dedupe_id"):
            a["dedupe_url"] = automations.source_url(a["dedupe_service"], a["dedupe_id"])
        a["trigger_text"] = automations.TRIGGERS.get(a.get("trigger") or "any")
        a["trigger_url"] = automations.source_url(a["trigger_service"], a["trigger_id"]) if a.get("trigger_id") else None
        a["compare_url"] = automations.source_url(a["compare_service"], a["compare_id"]) if a.get("compare_id") else None
        a["next_run"] = next_run(a)
        a["unchecked"] = json.loads(a.get("unchecked") or "[]")
    spotify, youtube = connected()
    logins = [(service, acct) for service, on in (("YouTube", youtube), ("Spotify", spotify))
              if on and (acct := account_info(service.lower()))]  
    return render_template("automations.html", autos=autos, logins=logins,
                           scheduled=scheduled(), task_name=TASK_NAME, windows=sys.platform == "win32", **block_options())

def block_options():
    return {"picks": automations.PICKS, "max_songs": automations.MAX_SONGS, "triggers": automations.TRIGGERS,
            "compares": automations.COMPARES,
            "units": automations.UNITS, "max_every": automations.MAX_EVERY,
            "services": automations.SERVICES, "max_sources": MAX_SOURCES}

MAX_SOURCES = 10

def next_run(a):
    if a.get("paused"):
        return None
    if a.get("every_unit") == "never":
        return "only when you press Run now"
    if a.get("every_unit") == "always":
        return "within 5 minutes"
    if not a.get("last_ts"):
        return "within the hour"
    when = datetime.fromtimestamp(a["last_ts"] + automations.interval(a))
    return "within the hour" if when <= datetime.now() else f"{when:%Y-%m-%d %H}:00 or so"

def read_rules(form):
    def number(name, top):
        raw = (form.get(name) or "").strip()
        return max(1, min(int(raw), top)) if raw.isdigit() else None
    unit = form.get("every_unit") if form.get("every_unit") in automations.UNITS else "hours"
    return {
        "count": number("count", automations.MAX_SONGS),
        "days": number("days", 3650),
        "pick": form.get("pick") if form.get("pick") in automations.PICKS else "newest",
        "every_n": number("every_n", automations.MAX_EVERY.get(unit, 1)) or 1,
        "every_unit": unit,
        "trigger": form.get("trigger") if form.get("trigger") in automations.TRIGGERS else "any",
        "keep_old": int(form.get("keep_old") == "1"),
        "note": (form.get("note") or "").strip()[:300] or None,
    }

def read_name(form, fallback):
    return (form.get("name") or "").strip()[:80] or fallback

def read_sources(form, known=()):
    titles = {(s["service"], s["playlist_id"]): s["title"] for s in known}
    found = []
    for link in [l for l in form.getlist("source") if l.strip()][:MAX_SOURCES]:
        every = re.fullmatch(r"\s*all my (spotify |youtube )?playlists\s*", link, re.I)
        if every:  
            for service in ([every.group(1).strip().title().replace("Youtube", "YouTube")] if every.group(1) else automations.SERVICES):
                if (service, automations.ALL) not in [(s, p) for s, p, _ in found]:
                    found.append((service, automations.ALL, "A" + automations.all_label(service)[1:]))
            continue
        service, playlist_id = automations.service_of(link)
        if not playlist_id:
            flash(f"That doesn't look like a Spotify or YouTube playlist link: {link[:80]}")
            return None
        if (service, playlist_id) in [(s, p) for s, p, _ in found]:
            continue
        title = titles.get((service, playlist_id))
        if title is None:
            loaded = automations.reader(service, youtube_client(uid()), spotify_client(uid())).load(playlist_id)
            if loaded is None:
                flash(f"Couldn't open that {service} playlist. Check the link, and that it isn't private: {link[:80]}")
                return None
            title = loaded[0]
        found.append((service, playlist_id, title))
    return found

def read_blocks(form, known=()):
    copies = form.get("copies") == "1"
    sources = read_sources(form, known) if copies else []
    if sources is None:
        return None
    if copies and not sources:
        flash("A Copy block needs at least one Take block with a playlist in it.")
        return None
    dedupe = {"dedupe_service": None, "dedupe_id": None, "dedupe_title": None}
    if (form.get("dedupe_source") or "").strip():
        found = read_sources(_OneLink(form["dedupe_source"]), known)
        if found is None:
            return None
        dedupe = dict(zip(("dedupe_service", "dedupe_id", "dedupe_title"), found[0]))
    if not copies and not dedupe["dedupe_id"]:
        flash("An automation needs a Copy block or a Clean block.")
        return None
    dedupe.update(trigger_service=None, trigger_id=None, trigger_title=None)
    if form.get("trigger") in ("added", "removed", "changed"):
        link = (form.get("trigger_source") or "").strip()
        if not link:
            flash("Pick the playlist the Trigger block watches.")
            return None
        if re.fullmatch(r"\s*all my (spotify |youtube )?playlists\s*", link, re.I):
            flash("The Trigger block watches one playlist. Pick a single playlist for it.")
            return None
        found = read_sources(_OneLink(link), known)
        if found is None:
            return None
        dedupe.update(zip(("trigger_service", "trigger_id", "trigger_title"), found[0]))
    dedupe.update(compare_mode=None, compare_service=None, compare_id=None, compare_title=None)
    if copies and form.get("compare_mode") in automations.COMPARES:
        link = (form.get("compare_source") or "").strip()
        if not link:
            flash("Pick the playlist the Compare block compares with.")
            return None
        if re.fullmatch(r"\s*all my (spotify |youtube )?playlists\s*", link, re.I):
            flash("The Compare block compares with one playlist. Pick a single playlist for it.")
            return None
        found = read_sources(_OneLink(link), known)
        if found is None:
            return None
        dedupe.update(compare_mode=form["compare_mode"],
                      **dict(zip(("compare_service", "compare_id", "compare_title"), found[0])))
    if form.get("every_unit") == "always" and any(p == automations.ALL for _, p, _ in sources):
        flash('"All my playlists" can run every hour at most due to rate limits. Pick "hours" or "days" in the start block.')
        return None
    dest = {"dest_existing": 0}
    if copies and form.get("dest_mode") == "existing":
        if not (form.get("dest_link") or "").strip():
            flash("Pick the playlist the Copy block should copy into.")
            return None
        found = read_sources(_OneLink(form["dest_link"]), known)
        if found is None:
            return None
        service, playlist_id, title = found[0]
        if (service, playlist_id) in [(s, p) for s, p, _ in sources]:
            flash("The Copy playlist can't also be one of the Take playlists.")
            return None
        if (service, playlist_id) == (dedupe["compare_service"], dedupe["compare_id"]):
            flash("The Compare playlist can't be the playlist Copy copies into.")
            return None
        dest = {"dest_existing": 1, "dest_service": service, "dest_id": playlist_id, "dest_name": title}
    return copies, sources, dedupe, dest

class _OneLink:
    def __init__(self, link):
        self.link = link
    def getlist(self, _):
        return [self.link]

@app.route("/automations", methods=["POST"])
def automations_add():
    db = store.connect()
    blocks = read_blocks(request.form)
    if blocks is None:
        return redirect("/automations")
    copies, sources, dedupe, dest = blocks
    rules = read_rules(request.form)
    titles = " + ".join(t for _, _, t in sources) or f"Clean {dedupe['dedupe_title']}"
    if not dest["dest_existing"]:
        dest["dest_service"] = request.form.get("dest_service") if request.form.get("dest_service") in automations.SERVICES else "Spotify"
        dest["dest_name"] = request.form.get("dest_name", "").strip() or f"{titles} ({automations.describe(rules)})"[:100]
    name = read_name(request.form, titles[:80])
    claim("google", google_account_id)
    store.add_automation(db, uid(), sources, copies=int(copies), name=name, **dest, **dedupe, **rules)
    flash(f'Added "{name}". Click "Run now" to run it straight away.')
    return redirect("/automations")

@app.route("/automations/<int:auto_id>/edit", methods=["GET", "POST"])
def automations_edit(auto_id):
    db = store.connect()
    auto = store.get_automation(db, auto_id, uid())
    if not auto:
        return redirect("/automations")
    srcs = store.sources(db, auto_id)
    if request.method == "POST":
        known = srcs + ([{"service": auto["dedupe_service"], "playlist_id": auto["dedupe_id"], "title": auto["dedupe_title"]}]
                        if auto.get("dedupe_id") else []) \
                     + ([{"service": auto["dest_service"], "playlist_id": auto["dest_id"], "title": auto["dest_name"]}]
                        if auto.get("dest_existing") else []) \
                     + ([{"service": auto["trigger_service"], "playlist_id": auto["trigger_id"], "title": auto["trigger_title"]}]
                        if auto.get("trigger_id") else []) \
                     + ([{"service": auto["compare_service"], "playlist_id": auto["compare_id"], "title": auto["compare_title"]}]
                        if auto.get("compare_id") else [])
        blocks = read_blocks(request.form, known=known)
        if blocks is None:
            return redirect(f"/automations/{auto_id}/edit")
        copies, sources, dedupe, dest = blocks
        if (dedupe["trigger_service"], dedupe["trigger_id"]) != (auto.get("trigger_service"), auto.get("trigger_id")):
            dedupe["trigger_keys"] = None 
        if dest["dest_existing"]:
            if (dest["dest_service"], dest["dest_id"]) != (auto["dest_service"], auto["dest_id"]) or not auto.get("dest_existing"):
                dest["sent_ids"] = "[]"  
            dest_name = dest["dest_name"]
        else:
            dest_name = request.form.get("dest_name", "").strip() or auto["dest_name"]
            dest["dest_name"] = dest_name
            if auto.get("dest_existing"): 
                dest.update(dest_id=None, sent_ids="[]")
            if not dest.get("dest_id", auto["dest_id"]) and request.form.get("dest_service") in automations.SERVICES:
                dest["dest_service"] = request.form["dest_service"]
        store.update_automation(db, auto_id, copies=int(copies),
                                name=read_name(request.form, auto.get("name") or auto["source_title"]),
                                **dest, **dedupe, **read_rules(request.form))
        if sources:
            store.set_sources(db, auto_id, sources)
        msg = "Saved. The playlist changes on the next run."
        if copies and not dest["dest_existing"] and not auto.get("dest_existing") and dest_name != auto["dest_name"] and auto["dest_id"]:
            msg += " The new name only applies if the playlist gets recreated, rename the existing one in the app."
        flash(msg)
        return redirect("/automations")
    for src in srcs:
        src["url"] = automations.source_url(src["service"], src["playlist_id"])
    if auto.get("dedupe_id"):
        auto["dedupe_url"] = automations.source_url(auto["dedupe_service"], auto["dedupe_id"])
    if auto.get("dest_id"):
        auto["dest_url"] = automations.source_url(auto["dest_service"], auto["dest_id"])
    if auto.get("trigger_id"):
        auto["trigger_url"] = automations.source_url(auto["trigger_service"], auto["trigger_id"])
    if auto.get("compare_id"):
        auto["compare_url"] = automations.source_url(auto["compare_service"], auto["compare_id"])
    return render_template("automation_edit.html", a=auto, sources=srcs, **block_options())

@app.route("/automations/<int:auto_id>/run", methods=["POST"])
def automations_run(auto_id):
    db = store.connect()
    auto = store.get_automation(db, auto_id, uid())
    if auto:
        result = automations.run(auto, youtube_client(uid()), spotify_client(uid()), db, force=True)
        flash(f"{auto.get('name') or auto['dest_name']}: {result}")
        unchecked = json.loads(store.get_automation(db, auto_id, uid()).get("unchecked") or "[]")
        if unchecked:
            flash("You haven't checked " + ", ".join(f"{u['count']} song{'s' if u['count'] != 1 else ''} in {u['title']}" for u in unchecked)
                  + " yet. Open the review link on the automation to check the matches.")
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
