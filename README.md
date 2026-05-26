# Spotify Playlist Picker

This project is a small command-line script that talks to Spotify and helps you:

- pick random songs from your own playlists
- pick from playlists named like `Rediscover - Jan 19th`
- filter your own playlists by name using either plain text or a custom regex
- pick from all of your saved playlists
- pick from a random mix of your playlists
- search public Spotify playlists by keywords or phrases
- use a "Surprise me" mode driven by the local `emotions_genres.txt` list
- use `random-song.com` as an additional true-random song source inside `Surprise me`
- optionally look up selected songs on Spotify, YouTube, or both, and open the links
- optionally export the results to `.txt` or `.html`
- optionally create a new private Spotify playlist from the selected songs
- optionally skip very large public playlists by setting a maximum playlist size

## Fresh Machine Setup

If you are setting this up on a brand-new machine, here is the shortest path.

### 1. Install what you need

You need:

- `git`
- Python 3
- `pip` for Python 3

Check whether they are already installed:

```bash
git --version
python3 --version
python3 -m pip --version
```

If one of those commands fails:

- On macOS, install Xcode Command Line Tools with `xcode-select --install`
- Then install Python 3 from [python.org](https://www.python.org/downloads/) or with Homebrew

If you use Homebrew on macOS:

```bash
brew install python git
```

### 2. Clone the repository

```bash
git clone https://github.com/mijucik/spotify-playlist-picker.git
cd spotify-playlist-picker
```

### 3. Install Python dependencies

```bash
python3 -m pip install -r requirements.txt
```

### 4. Create a Spotify app

You need your own Spotify developer app so the script can authenticate to your account.

At a high level, you will:

1. Sign in at the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard/).
2. Create an app.
3. Choose `Web API` when Spotify asks which APIs you plan to use.
4. Open the app settings.
5. Copy the `Client ID`.
6. Reveal and copy the `Client Secret`.
7. Add this redirect URI to the app:

```text
http://127.0.0.1:8080/callback
```

Important:

- The redirect URI must match exactly.
- Use `127.0.0.1`, not `localhost`.

### 5. Provide your Spotify credentials

You have two options.

Option A: let the script prompt you.

```bash
python3 main.py
```

Option B: export environment variables first.

```bash
export SPOTIPY_CLIENT_ID="your-client-id"
export SPOTIPY_CLIENT_SECRET="your-client-secret"
export SPOTIPY_REDIRECT_URI="http://127.0.0.1:8080/callback"
python3 main.py
```

If you prefer, you can also copy [.env.example](/Users/kevintang/Downloads/spotify-scripts-main/.env.example:1) and use it as a reference for the values you need, but this project does not auto-load `.env` files.

### 6. Authorize the app in your browser

The first time you run the script, Spotify should open a browser window and ask you to approve access.

After you approve it:

- the script stores the auth token in `~/.spotify-scripts/token_cache.json`
- later runs should usually reuse that token automatically

### 7. Run it again later

Once dependencies are installed, the normal command is:

```bash
cd spotify-playlist-picker
python3 main.py
```

## Security improvements

This version makes a few security-focused changes:

- no Spotify client secret is stored in the code
- the script prompts for Spotify credentials if environment variables are missing
- auth tokens are cached in `~/.spotify-scripts/token_cache.json` with owner-only permissions
- `python-dotenv` was removed, which also removes the flagged `set_key` advisory from this project
- `spotipy` was bumped to `2.26.0`

## Running The Script

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Run the main script:

```bash
python3 main.py
```

Useful flows inside the app:

- `2` lets you filter your own playlists by `Rediscover`, `contains`, or `regex`
- `5` searches public playlists by your keywords
- `6` now has multiple surprise modes:
- random emotions/genres searched through public playlists
- `random-song.com` with completely random configurations
- `random-song.com` with your own custom genre/market/decade settings
- after songs are chosen, you can optionally look up links on `Spotify`, `YouTube`, or `both`
- when using public playlists, you can set a maximum playlist size to avoid huge playlists

## Notes

- `main.py` is the best entrypoint to use.
- For most people, playlist-name `contains` matching is simpler than regex. Regex is available when you want more control.
- The `song_cache_*.json` files are local caches of playlist track data, not Spotify credentials.
- The old root-level `.cache` file was Spotipy's token cache format.
