# Spotify Playlist Picker

This project is a small command-line script that talks to Spotify and helps you:

- pick random songs from your own playlists
- pick from playlists named like `Rediscover - Jan 19th`
- filter your own playlists by name using either plain text or a custom regex
- pick from all of your saved playlists
- pick from a random mix of your playlists
- search public Spotify playlists by keywords or phrases
- use a "Surprise me" mode driven by the local `emotions_genres.txt` list
- optionally look up selected songs on YouTube and open the links
- optionally export the results to `.txt` or `.html`
- optionally create a new private Spotify playlist from the selected songs
- optionally skip very large public playlists by setting a maximum playlist size

## Security improvements

This version makes a few security-focused changes:

- no Spotify client secret is stored in the code
- the script prompts for Spotify credentials if environment variables are missing
- auth tokens are cached in `~/.spotify-scripts/token_cache.json` with owner-only permissions
- `python-dotenv` was removed, which also removes the flagged `set_key` advisory from this project
- `spotipy` was bumped to `2.26.0`

## Run it

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Run the main script:

```bash
python3 main.py
```

## Spotify app setup

You need a Spotify app from the Spotify developer dashboard so the script can authenticate.

Recommended redirect URI:

```text
http://127.0.0.1:8080/callback
```

You can provide credentials either by entering them when prompted or by exporting environment variables:

```bash
export SPOTIPY_CLIENT_ID="your-client-id"
export SPOTIPY_CLIENT_SECRET="your-client-secret"
export SPOTIPY_REDIRECT_URI="http://127.0.0.1:8080/callback"
```

## Notes

- `main.py` is the best entrypoint to use.
- For most people, playlist-name `contains` matching is simpler than regex. Regex is available when you want more control.
- The `song_cache_*.json` files are local caches of playlist track data, not Spotify credentials.
- The old root-level `.cache` file was Spotipy's token cache format.
# spotify-playlist-picker
# spotify-playlist-picker
