from datetime import datetime
from googleapiclient.errors import HttpError
from spotipy.exceptions import SpotifyException
from transfer import DIRECTIONS, blocked_message

import matcher
import store

def accepted(match):
    if not match:
        return False
    if match.get("approved") is not None:
        return match["approved"] == 1
    return matcher.bucket(match["score"]) == "auto"

def is_missing(e):
    return (isinstance(e, SpotifyException) and e.http_status == 404) or \
           (isinstance(e, HttpError) and e.resp.status == 404)

def run(auto, yt, sp, db):
    d = DIRECTIONS[auto["direction"]](yt, sp)
    try:
        result = sync(d, auto, db)

    except (SpotifyException, HttpError) as e:
        msg = blocked_message(e)
        if not msg:
            raise
        result = f"Stopped, nothing changed. {msg}"

    store.update_automation(db, auto["id"], last_run=datetime.now().strftime("%Y-%m-%d %H:%M"), last_result=result) 
    return result 

def sync(d, auto, db):
    loaded = d.load(auto["source_id"])

    if loaded is None:
        return f"Couldn't open the {d.source} playlist! (It might be private/deleted)"
    _, items = loaded
    newest = sorted(items, key=lambda i: i["added_at"], reverse=True)[:auto["count"]]
    ids, needs_review = [], 0

    for item in newest:
        match = store.get_match(db, d.key(item), d.table)
        if match is None:
            match = d.find(item)
            store.save_match(db, match, d.table)
            match = store.get_match(db, d.key(item), d.table)
        if accepted(match) and d.dest_id(match):
            ids.append(d.dest_id(match))
        else:
            needs_review += 1
    
    ids = list(dict.fromkeys(ids))

    if auto["dest_id"]:
        try:
            in_playlist = d.replace(auto["dest_id"], ids)
        except (SpotifyException, HttpError) as e:
            if not is_missing(e):
                raise
            auto["dest_id"] = None  
    
    if not auto["dest_id"]:
        dest_id, _ = d.create_empty(auto["dest_name"], auto["source_id"])
        store.update_automation(db, auto["id"], dest_id=dest_id)
        in_playlist = d.replace(dest_id, ids)

    result = f"{in_playlist} songs in the playlist"
    
    if in_playlist < len(ids):
        result += f", {len(ids) - in_playlist} didn't fit in today's YouTube quota"
    
    if needs_review:
        result += f", {needs_review} left out (no confident match, tick them on the review page)" 
    return result + "."

def run_all(yt, sp):
    db = store.connect()
    return [(auto, run(auto, yt, sp, db)) for auto in store.automations(db)]
