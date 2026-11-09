import json
import re
import time
from datetime import datetime, timedelta, timezone
from googleapiclient.errors import HttpError
from spotipy.exceptions import SpotifyException
from transfer import API_ERRORS, DIRECTIONS, error_message

import lean
import matcher
import store

def accepted(match):
    if not match:
        return False
    if matcher.excluded(match):
        return False 
    if match.get("approved") is not None:
        return match["approved"] == 1
    return matcher.bucket(match["score"]) == "auto"

def is_missing(e):
    return (isinstance(e, SpotifyException) and e.http_status == 404) or \
           (isinstance(e, HttpError) and e.resp.status == 404)

PICKS = ("newest", "oldest")
MAX_SONGS = 500
EVERY_UNITS = {"hours": 3600, "days": 86400}
ALWAYS_SECONDS = 300  
UNITS = ("always", *EVERY_UNITS, "never")
MAX_EVERY = {"hours": 720, "days": 365}
TRIGGERS = { 
    "added": "added",
    "removed": "removed",
    "changed": "added or removed",
    "any": "always (neither)",
}
COMPARES = {"in": "are", "not_in": "aren't"}  
SLACK_SECONDS = 60  

def interval(auto):
    if auto.get("every_unit") == "always":
        return ALWAYS_SECONDS
    unit = auto.get("every_unit") if auto.get("every_unit") in EVERY_UNITS else "hours"
    return max(1, auto.get("every_n") or 1) * EVERY_UNITS[unit]

def due(auto, now=None):
    if auto.get("every_unit") == "never":
        return False
    last = auto.get("last_ts")
    return not last or (now or time.time()) - last >= interval(auto) - SLACK_SECONDS

def triggered(trigger, before, after):
    """Trigger block. before/after: the source's song keys at the last run and now."""
    added, removed = bool(after - before), bool(before - after)
    return {"added": added, "removed": removed, "changed": added or removed}.get(trigger, True)

def describe(auto):
    what = f"{auto['pick'] or 'newest'} {auto['count']} songs" if auto["count"] else "every song"
    when = f" added in the last {auto['days']} days" if auto["days"] else ""
    return what + when

def pick_items(items, auto, now=None):
    if auto["days"]:
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=auto["days"])).strftime("%Y-%m-%dT%H:%M:%S")
        items = [i for i in items if (i["added_at"] or "")[:19] >= cutoff]
    newest_first = (auto["pick"] or "newest") == "newest"
    items = sorted(items, key=lambda i: i["added_at"] or "", reverse=newest_first)
    return items[:auto["count"]] if auto["count"] else items

FULL_REFRESH_HOURS = 24
READS = {"YouTube": "yt2sp", "Spotify": "sp2yt"}
WRITES = {"Spotify": "yt2sp", "YouTube": "sp2yt"}
SERVICES = tuple(READS)
ALL = "*"  
MAX_ALL = 50  
MAX_NEW_MATCHES = {"yt2sp": 30, "sp2yt": 10}

def reader(service, yt, sp):
    return DIRECTIONS[READS[service]](yt, sp)

def writer(service, yt, sp):
    return DIRECTIONS[WRITES[service]](yt, sp)

def all_label(service):
    return f"all my {service} playlists"

def source_url(service, playlist_id):
    if playlist_id == ALL:
        return all_label(service)
    return DIRECTIONS[WRITES[service]].playlist_url(playlist_id)

def expand(src, d, skip):
    if src["playlist_id"] != ALL:
        return [src["playlist_id"]]
    return [p["id"] for p in d.my_playlists() if not p.get("owner") and p["id"] not in skip][:MAX_ALL]

def service_of(link):
    link = (link or "").strip()
    if not link:
        return None, None
    if "spotify" in link.lower():
        return "Spotify", DIRECTIONS["sp2yt"].playlist_id_from(link)
    if "list=" in link or "youtu" in link.lower():
        return "YouTube", DIRECTIONS["yt2sp"].playlist_id_from(link)
    if re.fullmatch(r"[A-Za-z0-9]{22}", link):
        return "Spotify", link
    return "YouTube", DIRECTIONS["yt2sp"].playlist_id_from(link)

def unchanged_youtube_sources(auto, srcs, yt):
    if not auto.get("dest_id") or auto.get("days") or auto.get("compare_id") or any(s["service"] != "YouTube" or s["playlist_id"] == ALL for s in srcs):
        return False, {}
    counts = {s["id"]: lean.playlist_count(yt, s["playlist_id"]) for s in srcs}
    unchanged = all(counts[s["id"]] is not None and counts[s["id"]] == s["item_count"] and s["checked"]
                    and time.time() - s["checked"] < FULL_REFRESH_HOURS * 3600 for s in srcs)
    return unchanged, counts

def run(auto, yt, sp, db, force=False):
    srcs = store.sources(db, auto["id"])
    if not force:
        store.update_automation(db, auto["id"], last_ts=time.time()) 
    results = []
    try:
        if auto.get("copies", 1) and srcs:
            unchanged, counts = (False, {}) if force else unchanged_youtube_sources(auto, srcs, yt)
            if unchanged:
                results.append("No changes.")
            else:
                results.append(sync(auto, srcs, yt, sp, db, force))
                for s in srcs:
                    if s["service"] == "YouTube" and s["playlist_id"] != ALL and not auto.get("_capped"):
                        count = counts.get(s["id"]) or lean.playlist_count(yt, s["playlist_id"])
                        store.update_source(db, s["id"], item_count=count, checked=time.time())
        if auto.get("dedupe_id"):
            results.append(dedupe({"service": auto["dedupe_service"], "playlist_id": auto["dedupe_id"]}, yt, sp))
    except API_ERRORS as e:
        results.append(f"Stopped: {error_message(e)}")
    result = " ".join(results) or "Nothing to do: add a \"copy into\" or \"remove duplicates\" block."

    store.update_automation(db, auto["id"], last_run=datetime.now().strftime("%Y-%m-%d %H:%M"), last_result=result)
    return result

def sync(auto, srcs, yt, sp, db, force=False):
    dest_service = auto.get("dest_service") or "Spotify"
    items, unreadable, read = [], [], set()
    titles = {}  
    skip = {a["dest_id"] for a in store.automations(db, auto["owner"]) if a.get("dest_id")} | {auto.get("dest_id")}
    for src in srcs:  
        d = reader(src["service"], yt, sp)
        for playlist_id in expand(src, d, skip):
            if (src["service"], playlist_id) in read: 
                continue
            read.add((src["service"], playlist_id))
            if src["playlist_id"] == ALL and len(read) > 1:
                time.sleep(0.25)  
            loaded = d.load(playlist_id)
            if loaded is None:
                unreadable.append(src["title"] or playlist_id)
                continue
            titles[(src["service"], playlist_id)] = loaded[0]
            items += [dict(i, _from=d, _pl=(src["service"], playlist_id)) for i in loaded[1]]
    if unreadable and not items:
        return f"Couldn't open {', '.join(unreadable)}! (private or deleted?)"

    keys = [i["_from"].key(i) for i in items]
    before = json.loads(auto["source_keys"]) if auto.get("source_keys") else None
    trigger = auto.get("trigger") or "any"
    watched, where = keys, ""
    if trigger != "any" and auto.get("trigger_id"):
        spot = (auto["trigger_service"], auto["trigger_id"])
        where = f" in {auto.get('trigger_title') or auto['trigger_id']}"
        if spot in read: 
            watched = [i["_from"].key(i) for i in items if i["_pl"] == spot]
        else:
            td = reader(auto["trigger_service"], yt, sp)
            loaded = td.load(auto["trigger_id"])
            if loaded is None:
                return f"Couldn't open the Trigger playlist {auto.get('trigger_title') or ''}! (private or deleted?)"
            watched = [td.key(i) for i in loaded[1]]
        watched_before = json.loads(auto["trigger_keys"]) if auto.get("trigger_keys") else None
    else:
        watched_before = before
    if not force and watched_before is not None and auto.get("dest_id") \
            and not triggered(trigger, set(watched_before), set(watched)):
        store.update_automation(db, auto["id"], source_keys=json.dumps(keys), trigger_keys=json.dumps(watched))
        return f"No songs {TRIGGERS[trigger]}{where}, playlist left as it was."

    compare = compare_set(auto, items, read, yt, sp, db)
    if isinstance(compare, str):
        return compare
    left_out = 0
    if compare:
        kept = [i for i in items if compare_keeps(compare, i["_from"].source, i["_from"].key(i), dest_service)]
        left_out, items = len(items) - len(kept), kept

    ids, needs_review, waiting, found = [], 0, 0, 0
    unchecked = {} 
    for item in pick_items(items, auto):
        d = item["_from"]
        if d.source == dest_service: 
            ids.append(d.key(item))
            continue
        match = store.get_match(db, d.key(item), d.table)
        if match is None:
            if found >= MAX_NEW_MATCHES[d.slug]:
                waiting += 1
                unchecked[item["_pl"]] = unchecked.get(item["_pl"], 0) + 1
                continue
            found += 1
            match = d.find(item)
            store.save_match(db, match, d.table)
            match = store.get_match(db, d.key(item), d.table)
        if accepted(match) and d.dest_id(match):
            if compare and compare[0] == dest_service and (d.dest_id(match) in compare[1]) != compare[3]:
                left_out += 1  
                continue
            ids.append(d.dest_id(match))
        else:
            needs_review += 1
            if match.get("approved") is None: 
                unchecked[item["_pl"]] = unchecked.get(item["_pl"], 0) + 1

    auto["_capped"] = bool(waiting)
    store.update_automation(db, auto["id"], unchecked=json.dumps([
        {"service": s, "playlist_id": p, "title": titles.get((s, p)) or p, "slug": READS[s], "count": n}
        for (s, p), n in unchecked.items()]))
    if not waiting:
        store.update_automation(db, auto["id"], source_keys=json.dumps(keys), trigger_keys=json.dumps(watched))
    ids = list(dict.fromkeys(ids)) 
    if auto.get("keep_old"): 
        ids = list(dict.fromkeys(json.loads(auto.get("sent_ids") or "[]") + ids))

    out = writer(dest_service, yt, sp)
    if auto.get("dest_existing"):
        result = into_existing(auto, ids, out, yt, sp, db)
        if waiting:
            result += f", {waiting} more get matched on the next runs"
        if needs_review:
            result += f", {needs_review} left out (no confident match, check them on the review page)"
        if left_out:
            result += f", {left_out} left out by Compare"
        if unreadable:
            result += f", couldn't open {', '.join(unreadable)}"
        return result + "."
    if auto["dest_id"]:
        try:
            in_playlist = out.replace(auto["dest_id"], ids)
        except (SpotifyException, HttpError) as e:
            if not is_missing(e):
                raise
            auto["dest_id"] = None

    if not auto["dest_id"]:
        description = "Made by Syncer from " + ", ".join(source_url(s["service"], s["playlist_id"]) for s in srcs)
        dest_id, _ = out.create_empty(auto["dest_name"], srcs[0]["playlist_id"], description=description[:300])
        store.update_automation(db, auto["id"], dest_id=dest_id)
        in_playlist = out.replace(dest_id, ids)

    store.update_automation(db, auto["id"], sent_ids=json.dumps(ids))
    result = f"{in_playlist} songs in the playlist"
    if in_playlist < len(ids):
        result += f", {len(ids) - in_playlist} didn't fit in today's YouTube quota"
    if waiting:
        result += f", {waiting} more get matched on the next runs"
    if needs_review:
        result += f", {needs_review} left out (no confident match, check them on the review page)"
    if left_out:
        result += f", {left_out} left out by Compare"
    if unreadable:
        result += f", couldn't open {', '.join(unreadable)}"
    return result + "."

def compare_set(auto, items, read, yt, sp, db):
    if not auto.get("compare_id"):
        return None
    service = auto["compare_service"]
    cd = reader(service, yt, sp)
    if (service, auto["compare_id"]) in read: 
        keys = {i["_from"].key(i) for i in items if i["_pl"] == (service, auto["compare_id"])}
    else:
        loaded = cd.load(auto["compare_id"])
        if loaded is None:
            return f"Couldn't open the Compare playlist {auto.get('compare_title') or ''}! (private or deleted?)"
        keys = {cd.key(i) for i in loaded[1]}
    other = set()
    for key in keys:
        match = store.get_match(db, key, cd.table)
        if accepted(match) and cd.dest_id(match):
            other.add(cd.dest_id(match))
    return service, keys, other, auto.get("compare_mode") != "not_in"

def compare_keeps(compare, service, key, dest_service):
    cmp_service, keys, other, want = compare
    if service == cmp_service:
        return (key in keys) == want
    if service == dest_service: 
        return (key in other) == want
    return True

def into_existing(auto, ids, out, yt, sp, db):
    dest_service = auto.get("dest_service") or "Spotify"
    d = reader(dest_service, yt, sp)
    loaded = d.load(auto["dest_id"])
    if loaded is None:
        return f"Couldn't open your Copy playlist {auto.get('dest_name') or ''} (deleted, or not yours?)"
    current = list(dict.fromkeys(d.key(i) for i in loaded[1]))
    have, want = set(current), set(ids)
    owned = json.loads(auto.get("sent_ids") or "[]") 
    remove = [x for x in current if x in set(owned) and x not in want]
    add = [x for x in ids if x not in have]
    added, removed = out.add_remove(auto["dest_id"], add, remove) if (add or remove) else ([], [])
    gone = set(removed)
    owned = [x for x in owned if x not in gone] + [x for x in added if x not in set(owned)]
    store.update_automation(db, auto["id"], sent_ids=json.dumps(owned))
    result = f"{len(added)} added, {len(removed)} removed, {len(current) - len(removed) + len(added)} songs in the playlist"
    if len(added) < len(add):
        result += f", {len(add) - len(added)} didn't fit in today's YouTube quota"
    return result

def dedupe(src, yt, sp):
    removed = 0
    if src["service"] == "Spotify":
        order, counts = [], {} 
        page = sp.playlist_items(src["playlist_id"], limit=100, additional_types=("track",))
        while page:
            for entry in page["items"]:
                t = entry.get("item") or entry.get("track") or {}
                uri = t.get("uri") if t.get("id") else None
                if uri:
                    counts[uri] = counts.get(uri, 0) + 1
                if uri is None or counts[uri] == 1:
                    order.append(uri)
            page = sp.next(page) if page.get("next") else None
        doubled = [uri for uri, n in counts.items() if n > 1]
        for i in range(0, len(doubled), 100):
            sp.playlist_remove_all_occurrences_of_items(src["playlist_id"], doubled[i:i + 100])
        doubled = set(doubled)
        for position, uri in enumerate(order): 
            if uri in doubled:
                sp.playlist_add_items(src["playlist_id"], [uri], position=position)
        removed = sum(counts[uri] - 1 for uri in doubled)
    else:
        seen, extra = set(), []
        req = yt.playlistItems().list(part="snippet", playlistId=src["playlist_id"], maxResults=50)
        while req:
            resp = req.execute()
            for item in resp["items"]:
                vid = item["snippet"]["resourceId"].get("videoId")
                if vid in seen:
                    extra.append(item["id"])
                else:
                    seen.add(vid)
            req = yt.playlistItems().list_next(req, resp)
        for item_id in extra:
            yt.playlistItems().delete(id=item_id).execute()
            removed += 1
    if not removed:
        return "No duplicates found."
    return f"Removed {removed} duplicate{'s' if removed != 1 else ''}."

def run_all(clients_for):
    db = store.connect()
    results, clients = [], {}
    for auto in store.automations(db):
        if not due(auto):
            continue
        owner = auto["owner"]
        if owner not in clients:
            try:
                clients[owner] = clients_for(owner)
            except Exception as e: 
                clients[owner] = e
        got = clients[owner]
        if isinstance(got, Exception):
            results.append((auto, f"Skipped, the owner needs to log in to both again ({type(got).__name__})."))
            continue
        results.append((auto, run(auto, *got, db)))
    return results
