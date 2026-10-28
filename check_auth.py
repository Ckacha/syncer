from auth import spotify_client, youtube_client

"""verify connection & display playlists on both accounts, still need to check if sub-playlists on spotify display right"""
sp = spotify_client()
print("Spotify playlists:")
page = sp.current_user_playlists(limit=50)
while page:
    for p in page["items"]:
        print(f"  {p['name']}")
    page = sp.next(page) if page["next"] else None

yt = youtube_client()
print("\nYouTube playlists:")
request = yt.playlists().list(part="snippet,contentDetails", mine=True, maxResults=50)
while request:
    response = request.execute()
    for p in response["items"]:
        print(f"  {p['snippet']['title']} ({p['contentDetails']['itemCount']} videos)")
    request = yt.playlists().list_next(request, response)
