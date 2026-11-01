"""Runs every automation once. Windows Task Scheduler calls this, see the automations page.
Output goes to automations.log next to this file."""
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

from googleapiclient.discovery import build

import automations
from auth import load_youtube_creds, spotify_client, spotify_logged_in


def log(line):
    with open(os.path.join(HERE, "automations.log"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M} {line}\n")

def main():
    creds = load_youtube_creds()
    if not creds or not spotify_logged_in():
        log("Not logged in to both Spotify and YouTube, open the app and log in again.")
        return 1
    yt = build("youtube", "v3", credentials=creds)
    for auto, result in automations.run_all(yt, spotify_client()):
        log(f"#{auto['id']} {auto['source_title']} -> {auto['dest_name']}: {result}")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"Crashed: {type(e).__name__}: {e}")
        raise
