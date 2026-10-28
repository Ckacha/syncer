import os
import spotipy

"""main login helpers, double check refs in the notes file for specifics"""

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()

SPOTIFY_SCOPES = "playlist-read-private playlist-modify-private playlist-modify-public"
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube"]
YOUTUBE_TOKEN_FILE = "token.json"

def spotify_oauth(state=None):
    return SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=os.environ["SPOTIFY_REDIRECT_URI"],
        scope=SPOTIFY_SCOPES,
        cache_path=".cache",
        state=state,
        open_browser=False,
    )

def spotify_logged_in():
    oauth = spotify_oauth()
    return oauth.validate_token(oauth.cache_handler.get_cached_token()) is not None

def spotify_client():
    return spotipy.Spotify(auth_manager=spotify_oauth())

def load_youtube_creds():
    """either gonna return valid or none"""
    if not os.path.exists(YOUTUBE_TOKEN_FILE):
        return None
    creds = Credentials.from_authorized_user_file(YOUTUBE_TOKEN_FILE, YOUTUBE_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_youtube_creds(creds)
    return creds if creds.valid else None

def save_youtube_creds(creds):
    with open(YOUTUBE_TOKEN_FILE, "w") as f:
        f.write(creds.to_json())

def youtube_client():
    creds = load_youtube_creds()
    if not creds:
        flow = InstalledAppFlow.from_client_secrets_file(
            os.environ["GOOGLE_CLIENT_SECRETS_FILE"], YOUTUBE_SCOPES
        )
        creds = flow.run_local_server(port=0)
        save_youtube_creds(creds)
    return build("youtube", "v3", credentials=creds)
