import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlparse

import requests

NOISE_WORDS = (
    "official", "video", "audio", "lyric", "lyrics", "hd", "hq", "4k", "mv",
    "visualizer", "visualiser", "music video", "explicit", "clean", "color coded",
)
CHANNEL_NOISE = re.compile(r"vevo|official|music|records|\s-\stopic$", re.I)
FEAT = re.compile(r"\s*[\(\[]?\s*\b(?:ft|feat|featuring)\b\.?\s+(?:[^\)\]/|]|/(?!\s))*[\)\]]?", re.I)
DASHES = re.compile("\\s+[-\u2013\u2014]\\s+")  
SLASH = re.compile(r"\s+/\s*|\s*/\s+|(?<=[^\x00-\x7f])/(?=[^\x00-\x7f])")  
SPOTIFY_LINK = re.compile(r"open\.spotify\.com/(?:intl-[\w-]+/)?(track|album)/([A-Za-z0-9]{22})")
QUOTED = re.compile(r'[“"]([^”"]{2,})[”"]')
JP_TITLE = re.compile(r"[『「]([^』」]+)[』」]")  #need to find more seperating symbols aasdpioasdpasd
CHANNEL_NUMBER = re.compile(r"\s*[\(（]\d+[\)）]\s*$")

LINK_PAGE_HOSTS = (
    "lnk.to", "linkco.re", "tunecore.co.jp", "song.link", "album.link", "odesli.co", "ffm.to",
    "smarturl.it", "orcd.co", "fanlink.to", "fanlink.tv", "found.ee", "bfan.link", "ditto.fm",
    "distrokid.com", "nex-tone.link", "big-up.style", "linkfire.com",
) # if theres more links than these im gonna do something not fun

URL = re.compile(r"https?://[^\s<>\"'()]+")
SPOTIFY_REDIRECT = re.compile(r"https?://[^\s<>\"']+/(?:to|go|redirect|r)/spotify[^\s<>\"']*", re.I)

TOPIC_LINE = re.compile(r"Provided to YouTube by[^\n]*\n\s*\n([^\n]+ · [^\n]+)(?:\n\s*\n(?!℗|Released on|Composer|Lyricist|Auto-generated)([^\n]+))?")
LINKED_BONUS = 3  
MAX_SEARCHES = 6
AUTO_ADD = 8
REVIEW = 5

def tag_word(word, after=""):
    return re.compile(
        rf"[\(\[【][^\)\]】]*\b{word}\b"                              
        rf"|[\-–—]\s*{word}\b\s*(?:{after}ver|version|$)", 
        re.I,
    )

VERSION_TAGS = {
    "instrumental": re.compile(r"instrumental|\binst\b\.?|off ?vocal|オフボーカル|karaoke|カラオケ|backing track", re.I),
    "acapella": re.compile(r"a ?cappella|acapella|vocals? only", re.I),
    "remix": re.compile(r"remix|\bbootleg\b|\bflip\b", re.I),
    "sped up": re.compile(r"sped ?up|speed ?up|nightcore", re.I),
    "slowed": re.compile(r"slowed", re.I),
    "8-bit": re.compile(r"8[ -]?bit", re.I),
    "live": tag_word("live", after=r"at\b|from\b|in\b|on\b|session|"),
    "cover": tag_word("cover", after=r"by\b|"),
    "acoustic": tag_word("acoustic", after=r"session|"),
}
VERSION_PENALTY = 10 

LOOSE_VIDEO_TAGS = {
    "cover": ("cover", re.compile(r"カバー|歌ってみた|歌いました|弾いてみた|叩いてみた|切り抜き|を歌う|合わせてみた")),
    "acoustic": ("acoustic", re.compile(r"アコースティック")),
}

def _nfkc(text):
    return unicodedata.normalize("NFKC", text or "")

def _as_tag(word):
    """word used as a tag, not as part of a song name: "(cover)", "【COVER】", "/ cover", "… cover", "acoustic ver." """
    return re.compile(rf"[\(\[【]\s*[^\)\]】]*\b{word}|\b{word}\w*\s*[\)\]】]|[/／]\s*\S*\s*{word}\b"
                      rf"|\b{word}\w*\s*(?:ver|version)\b|\b{word}\w*\s*$", re.I)

def track_versions(name):
    name = _nfkc(name)
    tags = versions(name)
    if re.search(r"カバー|covered by", name, re.I):
        tags.add("cover")
    return tags

def video_versions(title, track_name=""):
    title = _nfkc(title)
    tags = versions(title)
    for tag, (word, other) in LOOSE_VIDEO_TAGS.items():
        if tag in tags:
            continue
        if other.search(title):
            tags.add(tag)
        elif re.search(rf"\b{word}(?:ed)?\b", title, re.I):
            if not re.search(rf"\b{word}", track_name or "", re.I) or _as_tag(word).search(title):
                tags.add(tag)
    return tags

def version_mismatch(video_title, track_name):
    return video_versions(video_title, track_name) ^ track_versions(track_name)

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
                "added_at": s["publishedAt"],  
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
    artists, extra, other_order = [], [], []

    if is_topic:
        artists.append(channel[: -len(" - Topic")])
    else:
        channel_artist = CHANNEL_NUMBER.sub("", CHANNEL_NOISE.sub("", channel)).strip()
        parts = DASHES.split(cleaned, maxsplit=1)
        if len(parts) == 2:
            left, right = (p.strip() for p in parts)
            if JP_TITLE.search(left) and not JP_TITLE.search(right) or (
                    is_channel(right, channel_artist) and not is_channel(left, channel_artist)):
                artists.append(right)  
                cleaned = left
            elif is_channel(left, channel_artist):
                artists.append(left)   
                cleaned = right
            else:
                artists += [left, right]
                cleaned = right
                other_order = [left]
        else:
            parts = SLASH.split(cleaned, maxsplit=1)
            if len(parts) == 2:
                cleaned = parts[0]
                extra = [a.strip() for a in parts[1].split(",") if not QUOTED.search(a)]
        artists += [channel_artist] + extra

    names = [cleaned]
    jp = JP_TITLE.search(cleaned)
    if jp:
        before = cleaned[:jp.start()].strip(" .-　")
        names.insert(0, jp.group(1).strip())
        if before and not is_topic:
            artists.insert(0, before)
    quoted = [q.strip() for q in QUOTED.findall(unicodedata.normalize("NFKC", title))]
    names[1 if jp else 0:1 if jp else 0] = quoted
    main = re.sub(r"\s*\([^)]*\)", "", cleaned).strip()
    if not main.isascii():
        names += [main] + [p.strip() for p in re.findall(r"\(([^)]*)\)", cleaned)]

    names += other_order
    artists = list(dict.fromkeys(a for a in artists if a))
    return list(dict.fromkeys(n for n in names if n)), artists, is_topic

def is_channel(part, channel_artist):
    a, b = normalize(part), normalize(channel_artist)
    if not a or not b:
        return False
    return a == b or b in a or a in b or artists_overlap([part], [channel_artist]) or any(
        w in a.split() for w in b.split() if len(w) >= 2 and not w.isascii())

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

_KANA = dict(zip(
    "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわゐゑをん"
    "がぎぐげござじずぜぞだぢづでどばびぶべぼぱぴぷぺぽぁぃぅぇぉゃゅょゎゔ",
    "a i u e o ka ki ku ke ko sa shi su se so ta chi tsu te to na ni nu ne no ha hi fu he ho "
    "ma mi mu me mo ya yu yo ra ri ru re ro wa i e o n ga gi gu ge go za ji zu ze zo da ji zu de do "
    "ba bi bu be bo pa pi pu pe po a i u e o ya yu yo wa vu".split()))
_SMALL_Y = set("ゃゅょ")
_SMALL_V = set("ぁぃぅぇぉ")

def romaji(text):
    out, double = [], False
    for ch in text:
        if "ァ" <= ch <= "ヶ":
            ch = chr(ord(ch) - 0x60)  # i did not think id be going this for romanization faushfdoiasfjoasfasf
        if ch == "っ":
            double = True
            continue
        if ch == "ー":
            continue
        r = _KANA.get(ch)
        if r is None:
            out.append(ch)
            double = False
            continue
        if out and ch in _SMALL_Y and out[-1].endswith("i") and len(out[-1]) > 1:
            stem = out[-1][:-1]  
            out[-1] = stem + (r[-1] if stem in ("sh", "ch", "j") else r)
            continue
        if out and ch in _SMALL_V and out[-1][-1:] in "aiueo" and len(out[-1]) > 0:
            prev = out[-1]  
            out[-1] = ("w" + r) if prev == "u" else prev[:-1] + r
            continue
        if double and r[0] not in "aiueon":
            r = ("t" + r) if r.startswith("ch") else r[0] + r
        double = False
        out.append(r)
    return "".join(out)

def skeleton(text):
    t = re.sub(r"[^a-z]", "", romaji(text).lower())
    for a, b in (("ph", "f"), ("th", "s"), ("ck", "k"), ("c", "k"), ("q", "k"), ("x", "ks"),
                 ("l", "r"), ("v", "b"), ("tsu", "ts")):
        t = t.replace(a, b)
    t = re.sub(r"[aeiouy]", "", t)
    return re.sub(r"(.)\1+", r"\1", t)

def title_points(name, sp_title):
    yt_title = normalize(name)
    if not yt_title or not sp_title:
        return 0
    pairs = {(yt_title, sp_title), (normalize(romaji(yt_title)), normalize(romaji(sp_title)))}
    if any(a and a == b for a, b in pairs):
        return 4
    if any(similar(a, b) >= 0.8 or (b and b in a) for a, b in pairs):
        return 2
    if yt_title.isascii() != sp_title.isascii():
        a, b = skeleton(yt_title), skeleton(sp_title)
        if len(a) >= 4 and a == b:
            return 2
        if similar(loose(yt_title), loose(sp_title)) >= 0.8:
            return 2
    return 0

def loose(text):
    t = re.sub(r"[^a-z]", "", romaji(text).lower()).replace("l", "r").replace("v", "b")
    t = re.sub(r"(?<=[^aeiou])u", "", t) 
    return re.sub(r"(.)\1+", r"\1", t)

JP_LIVE = re.compile(r"[\(\[【][^\)\]】]*(?:実演|ライブ)[^\)\]】]*[\)\]】]")
#yup this is specifically for the first take specific songs
FIRST_TAKE = re.compile(r"\bthe\s*first\s*take\b", re.I)

def versions(title):
    tags = {tag for tag, pattern in VERSION_TAGS.items() if pattern.search(title)}
    if JP_LIVE.search(_nfkc(title)) or FIRST_TAKE.search(_nfkc(title)):
        tags.add("live")
    return tags

def spotify_titles(name):
    titles = [normalize(name)]
    parts = DASHES.split(name, maxsplit=1)
    if len(parts) == 2 and not parts[0].isascii() and parts[1].isascii():
        titles.append(normalize(parts[0]))
    return titles

def title_score(names, track):
    return max((title_points(n, t) for n in names for t in spotify_titles(track["name"])), default=0)

def linked_title_ok(names, track):
    if title_score(names, track) > 0:
        return True
    return all(n.isascii() != track["name"].isascii() for n in names)

def score(video, names, artists, is_topic, track, artist_hit=False):
    title = title_score(names, track)
    points = title

    if version_mismatch(video["title"], track["name"]):
        points -= VERSION_PENALTY

    artist_match = artist_hit or artists_overlap(artists, [a["name"] for a in track["artists"]]) \
        or (title > 0 and guest_hit(video, track)) 
    if artist_match:
        points += 3

    diff = None
    if video["duration_s"] is not None:
        diff = abs(video["duration_s"] - track["duration_ms"] / 1000)
        if diff <= 5:
            points += 2
        elif diff <= 15:
            points += 1

    exact_length = diff is not None and diff <= 1.5
    if exact_length and (artist_match or (is_topic and title == 4)):
        points += 1 

    if is_topic:
        points += 1
        if not artist_match and comparable(artists, [a["name"] for a in track["artists"]]):
            points -= TOPIC_OTHER_ARTIST

    return min(points, 10)

def guest_hit(video, track):
    guests = video_guests(video["title"])
    return bool(track and guests and artists_overlap(guests, [a["name"] for a in track["artists"]]))

def better(video, s, track, best_score, best):
    return s > best_score or (s == best_score and guest_hit(video, track) and not guest_hit(video, best))
VIDEO_FEAT = re.compile(r"\b(?:ft|feat|featuring)\b\.?\s+((?:[^\)\]】|/(]|/(?!\s))+)", re.I)

def video_guests(title):
    found = VIDEO_FEAT.findall(_nfkc(title)) 
    return list(dict.fromkeys(g for part in found for g in ARTIST_SPLIT.split(part.strip()) if g))

TOPIC_OTHER_ARTIST = 4
ARTIST_SPLIT = re.compile(r"\s*(?:,|、|&|＆|×|\bx\b|\band\b|\bwith\b|\bfeat\.?|\bft\.?|\bvs\.?)\s*", re.I)

def comparable(yt_artists, sp_artists):
    def scripts(names):
        latin = any(romaji(normalize(n)).replace(" ", "").isascii() for n in names if normalize(n))
        other = any(not romaji(normalize(n)).replace(" ", "").isascii() for n in names if normalize(n))
        return latin, other
    (yl, yo), (sl, so) = scripts(yt_artists), scripts(sp_artists)
    return (yl and sl) or (yo and so)

def artists_overlap(yt_artists, sp_artists):
    def parts(names):
        out = set()
        for n in names:
            for piece in [n] + ARTIST_SPLIT.split(n):
                for form in (normalize(piece), normalize(romaji(normalize(piece)))): 
                    if form:
                        out.add(form.replace(" ", ""))
        return out
    ys, ss = parts(yt_artists), parts(sp_artists)
    return any(y == s or similar(y, s) >= 0.9 for y in ys for s in ss)

def find_match(sp, video):
    names, artists, is_topic = [], [], False
    for title in video.get("titles") or [video["title"]]:
        n, a, is_topic = parse_video(title, video["channel"])
        names += n
        artists += a
    if is_topic: 
        title_line = TOPIC_LINE.search(video.get("description") or "")
        if title_line:
            parts = [p.strip() for p in title_line.group(1).split(" · ")]
            names.append(parts[0])
            artists += parts[1:]
            release = (title_line.group(2) or "").strip()
            if release.isascii() and not parts[0].isascii(): 
                names.append(release)
    names, artists = list(dict.fromkeys(names)), list(dict.fromkeys(artists))

    best, best_score = None, 0
    seen = []  

    linked = [t for t in linked_tracks(sp, video.get("description") or "") if linked_title_ok(names, t)]
    for track in linked:
        s = score(video, names, artists, is_topic, track) + LINKED_BONUS
        if len(linked) == 1 and not version_mismatch(video["title"], track["name"]):
            s = max(s, AUTO_ADD)  
        if s > best_score:
            best, best_score = track, min(s, 10)

    queries = [(f'track:"{n}" artist:"{a}"', True) for a in artists[:2] for n in names[:3] if n != a]
    romanized = [(f"{names[0]} {romaji(a)}", False) for a in artists[:2]
                 if names and not a.isascii() and romaji(a) != a]
    plain = [(n, False) for n in names[:2]]
    plain += [(bare, False) for n in names[:1] if (bare := n.rstrip("。、！？!?.・ ")) and bare != n]  
    guests = [(f'track:"{names[0]}" artist:"{g}"', True) for g in video_guests(video["title"])
              if names and g.isascii() and g not in artists[:2]][:2]
    queries = queries[:2] + guests + romanized + plain + queries[2:]
    queries = list(dict.fromkeys(queries))[:MAX_SEARCHES]

    for q, artist_hit in queries:
        if best_score >= AUTO_ADD:
            break
        results = sp.search(q=q, type="track", limit=5 if artist_hit else 10)["tracks"]["items"]
        seen += [t for t in results if t]
        for track in results:
            if not track:
                continue
            s = score(video, names, artists, is_topic, track, artist_hit)
            if better(video, s, track, best_score, best):
                best, best_score = track, s

    if best_score < AUTO_ADD and not video["title"].isascii():
        found = translated_release(sp, video, names, artists, seen)
        if found:
            best, best_score = found, max(score(video, names, artists, is_topic, found), AUTO_ADD)

    best = prefer_single(sp, best, seen)

    return {
        **video,
        "clean_title": names[0] if names else video["title"],
        "artist": artists[0] if artists else "",
        "spotify_id": best["id"] if best else None,
        "spotify_name": best["name"] if best else None,
        "spotify_artists": ", ".join(a["name"] for a in best["artists"]) if best else None,
        "spotify_image": cover(best) if best else None,
        "score": best_score,
    }

def translated_release(sp, video, names, artists, seen):
    def base(name):
        return normalize(re.sub(r"\s*\([^)]*\)\s*$", "", _nfkc(name)))
    wanted = {normalize(n) for n in names}
    anchor = next((t for t in seen if base(t["name"]) in wanted
                   and artists_overlap(artists, [a["name"] for a in t["artists"]])), None)
    if not anchor:
        return None
    length = anchor["duration_ms"] / 1000
    if video["duration_s"] is not None and abs(video["duration_s"] - length) > 3:
        return None
    anchor_ids = {a["id"] for a in anchor["artists"]}
    for artist in anchor["artists"][:3]:
        try:
            results = sp.search(q=f'artist:"{artist["name"]}"', type="track", limit=10)["tracks"]["items"]
        except Exception:
            continue
        candidates = sorted(
            (abs(t["duration_ms"] / 1000 - length), t) for t in results
            if t and t["id"] != anchor["id"] and {a["id"] for a in t["artists"]} <= anchor_ids
            and not version_mismatch(video["title"], t["name"]))
        if candidates and candidates[0][0] <= 1 and (len(candidates) == 1 or candidates[1][0] - candidates[0][0] >= 1.5):
            return candidates[0][1]
    return None

def album_type(track):
    return ((track or {}).get("album") or {}).get("album_type")

def prefer_single(sp, best, seen):
    if not best or album_type(best) in (None, "single"):
        return best
    artist_ids = {a["id"] for a in best["artists"]}

    def same_single(t):
        return (t and t.get("id") != best["id"] and album_type(t) == "single"
                and normalize(t["name"]) == normalize(best["name"])
                and {a["id"] for a in t["artists"]} == artist_ids
                and abs(t["duration_ms"] - best["duration_ms"]) <= 5000)

    single = next((t for t in seen if same_single(t)), None)
    if single is None:
        try:
            q = f'track:"{best["name"]}" artist:"{best["artists"][0]["name"]}"'
            single = next((t for t in sp.search(q=q, type="track", limit=10)["tracks"]["items"] if same_single(t)), None)
        except Exception: 
            single = None
    return single or best

def cover(track):
    images = (track.get("album") or {}).get("images") or []
    return images[-1]["url"] if images else None

_page_cache = {}

def fetch_link_page(url, hops=3):
    if url in _page_cache:
        return _page_cache[url]
    result = (None, "")
    current = url
    try:
        for _ in range(hops + 1):
            host = (urlparse(current).hostname or "").lower()
            if host == "open.spotify.com":
                result = (current, "")
                break
            if not any(host == h or host.endswith("." + h) for h in LINK_PAGE_HOSTS):
                break
            r = requests.get(current, timeout=6, allow_redirects=False, stream=True,
                             headers={"User-Agent": "Mozilla/5.0 (Syncer playlist matcher)"})
            if r.is_redirect:
                current = urljoin(current, r.headers.get("Location", ""))
                r.close()
                continue
            html = r.raw.read(1_000_000, decode_content=True).decode(r.encoding or "utf-8", "replace") if r.ok else ""
            r.close()
            result = (current, html)
            break
    except requests.RequestException:
        pass
    _page_cache[url] = result
    return result

def linked_spotify_ids(description, max_pages=3):
    found = [(kind, sid) for kind, sid in SPOTIFY_LINK.findall(description)]
    pages = [u for u in URL.findall(description)
             if any((urlparse(u).hostname or "").lower().endswith(h) for h in LINK_PAGE_HOSTS)]
    for page in list(dict.fromkeys(pages))[:max_pages]:  # all of them: a description can link several songs
        final, html = fetch_link_page(page)
        found += SPOTIFY_LINK.findall(final or "") + SPOTIFY_LINK.findall(html)
        if not found: 
            for redirect in SPOTIFY_REDIRECT.findall(html)[:1]:
                final, _ = fetch_link_page(redirect)
                found += SPOTIFY_LINK.findall(final or "")
    return list(dict.fromkeys(found))

def linked_tracks(sp, description):
    tracks = []
    for kind, sid in linked_spotify_ids(description)[:3]:
        try:
            if kind == "track":
                tracks.append(sp.track(sid))
            else:
                tracks += [t for t in sp.album_tracks(sid, limit=50)["items"] if t and t.get("id")]
        except Exception: 
            continue
    return tracks

def match_titles(match):
    if "track_name" in match: 
        return match.get("video_title"), match.get("track_name")
    return match.get("title"), match.get("spotify_name")

def excluded(match):
    return version_mismatch(*match_titles(match)) if match else set()

def bucket(score):
    if score >= AUTO_ADD:
        return "auto"
    if score >= REVIEW:
        return "review"
    return "skip"

def spotify_playlist_tracks(sp, playlist_id):
    tracks = []
    page = sp.playlist_items(playlist_id, limit=100, additional_types=("track",))
    while page:
        for entry in page["items"]:
            t = entry.get("item") or entry.get("track")
            if not t or t.get("type") != "track" or not t.get("id"):
                continue 
            tracks.append({
                "track_id": t["id"],
                "track_name": t["name"],
                "artists": [a["name"] for a in t["artists"]],
                "track_artists": ", ".join(a["name"] for a in t["artists"]),
                "duration_s": t["duration_ms"] // 1000,
                "added_at": entry.get("added_at") or "",
                "image": cover(t),
            })
        page = sp.next(page) if page["next"] else None
    return tracks

def find_video(yt, track):
    artist = track["artists"][0] if track["artists"] else ""
    resp = yt.search().list(part="id", q=f"{artist} {track['track_name']}", type="video", maxResults=5).execute()
    ids = [item["id"]["videoId"] for item in resp["items"]]

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
