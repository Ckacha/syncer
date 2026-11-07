import sys

from auth import all_user_ids, spotify_client, youtube_client

if len(sys.argv) < 2:
    sys.exit("usage: python check_auth.py <user id>\nknown: " + ", ".join(all_user_ids()))
uid = sys.argv[1]
sp = spotify_client(uid)
print("Spotify playlists:")
page = sp.current_user_playlists(limit=50)
while page:
    for p in page["items"]:
        print(f"  {p['name']}")
    page = sp.next(page) if page["next"] else None

yt = youtube_client(uid)
print("\nYouTube playlists:")
request = yt.playlists().list(part="snippet,contentDetails", mine=True, maxResults=50)
while request:
    response = request.execute()
    for p in response["items"]:
        print(f"  {p['snippet']['title']} ({p['contentDetails']['itemCount']} videos)")
    request = yt.playlists().list_next(request, response)
 