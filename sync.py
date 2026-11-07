import os
import sys
import automations
from auth import spotify_client, youtube_client
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

def log(line):
    with open(os.path.join(HERE, "automations.log"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M} {line}\n")

def main():
    for auto, result in automations.run_all(lambda owner: (youtube_client(owner), spotify_client(owner))):
        log(f"#{auto['id']} {auto['source_title']} -> {auto['dest_name']}: {result}")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
        
    except Exception as e:
        log(f"Crashed: {type(e).__name__}: {e}")
        raise
