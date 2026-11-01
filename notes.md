spotify stuff
- Web API docs: https://developer.spotify.com/documentation/web-api
- spotipy (Python library): https://spotipy.readthedocs.io

yt
-  API v3 https://developers.google.com/youtube/v3
probably want t ocheck how quotas are used

gauth
https://console.cloud.google.com

spotify 100 max per request
tspmo

Need to do:
yt to spotify
better clean/strip links 
check how to scan/search both english and the base language it is (japanaese, korean, etc)

scoring so i dont forget
- title matches: +4 (+2 if its off by like 1/2 characters)
- artist matches: +3
- same length: +2 (+1 if close)
- if its the right topic channel: +1
- one is instrumental/remix/live/cover and the other isnt: -5
- 8+ gets added automatically, 5-7 user checks, under 5 skipped(let users see it tho)

saved stuff
saved as matches.db!! 
not sure if the way im styling right now is an issue 

automations
still need to do/define
should probably add proper error handling

limits are a pain...
