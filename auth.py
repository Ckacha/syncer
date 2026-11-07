import os
import re
import shutil
import spotipy

from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from requests.adapters import HTTPAdapter
from spotipy.oauth2 import SpotifyOAuth
from urllib3.util.retry import Retry

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS_DIR = os.path.join(HERE, "secrets")
load_dotenv(os.path.join(SECRETS_DIR, ".env"))
GOOGLE_CLIENT_SECRETS_FILE = os.path.join(SECRETS_DIR, os.environ["GOOGLE_CLIENT_SECRETS_FILE"])

SPOTIFY_SCOPES = "playlist-read-private playlist-modify-private playlist-modify-public"
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube"]

USERS_DIR = os.path.join(HERE, "users")
UID_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
TOKEN_FILES = {"spotify": "spotify.json", "youtube": "youtube.json"}

class NotLoggedIn(Exception):
    pass

def user_dir(uid):
    if not uid or not UID_RE.fullmatch(uid):
        raise ValueError("bad user id")
    path = os.path.join(USERS_DIR, uid)
    os.makedirs(path, exist_ok=True)
    return path

def token_file(uid, service):
    return os.path.join(user_dir(uid), TOKEN_FILES[service])

def all_user_ids():
    if not os.path.isdir(USERS_DIR):
        return []
    return [u for u in os.listdir(USERS_DIR) if UID_RE.fullmatch(u)]

def logout(uid, service):
    path = token_file(uid, service)
    if os.path.exists(path):
        os.remove(path)

def merge_user(src, dst):
    for name in TOKEN_FILES.values():
        f = os.path.join(user_dir(src), name)
        if os.path.exists(f):
            os.replace(f, os.path.join(user_dir(dst), name))
    shutil.rmtree(user_dir(src), ignore_errors=True)


def spotify_oauth(uid, state=None):
    return SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=os.environ["SPOTIFY_REDIRECT_URI"],
        scope=SPOTIFY_SCOPES,
        cache_path=token_file(uid, "spotify"),
        state=state,
        open_browser=False,
    )

def spotify_logged_in(uid):
    oauth = spotify_oauth(uid)
    return oauth.validate_token(oauth.cache_handler.get_cached_token()) is not None

def spotify_client(uid):
    if not spotify_logged_in(uid):
        raise NotLoggedIn("spotify")
    sp = spotipy.Spotify(auth_manager=spotify_oauth(uid))
    retry = Retry(
        total=3, backoff_factor=0.3, status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE"]),
        respect_retry_after_header=False,
    )
    sp._session.mount("https://", HTTPAdapter(max_retries=retry))
    return sp


def load_youtube_creds(uid):
    path = token_file(uid, "youtube")
    if not os.path.exists(path):
        return None
    creds = Credentials.from_authorized_user_file(path, YOUTUBE_SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            os.remove(path)
            return None
        save_youtube_creds(uid, creds)
    return creds if creds.valid else None

def save_youtube_creds(uid, creds):
    with open(token_file(uid, "youtube"), "w") as f:
        f.write(creds.to_json())

def youtube_client(uid):
    creds = load_youtube_creds(uid)
    if not creds:
        raise NotLoggedIn("youtube")
    return build("youtube", "v3", credentials=creds)
