# Spotify Playlist Picker

This is a command-line tool for finding random songs and building Spotify playlists.

It can:

- pull from all of your playlists, only your own playlists, or a random subset
- limit your personal playlist sources to `any`, `public only`, or `private only`
- filter your playlists by name with a Rediscover preset, plain text, or regex
- discover public songs through YouTube Music metadata, without returning random non-music videos
- use `random-song.com` inside `Surprise me` for truly random song discovery
- automatically add Spotify search links and YouTube links
- export results to `.txt` or `.html`
- create a new private Spotify playlist from the selected songs

The public discovery paths now use unauthenticated YouTube Music metadata through `ytmusicapi` and avoid Spotify's API entirely. Spotify API access is mainly used for your own playlists and for the final playlist-creation step when the app needs real Spotify track URIs.

## Fresh Machine Setup

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

On Windows:

- Install `Git for Windows` from [git-scm.com](https://git-scm.com/downloads/win)
- Install Python 3 from [python.org](https://www.python.org/downloads/windows/)
- Make sure `Add python.exe to PATH` is enabled during install

Then verify in `PowerShell`:

```powershell
git --version
python --version
python -m pip --version
```

On Ubuntu/Debian Linux:

```bash
sudo apt update
sudo apt install -y git python3 python3-pip
```

On Fedora:

```bash
sudo dnf install -y git python3 python3-pip
```

### 2. Clone the repository

```bash
git clone https://github.com/mijucik/spotify-playlist-picker.git
cd spotify-playlist-picker
```

On Windows PowerShell:

```powershell
git clone https://github.com/mijucik/spotify-playlist-picker.git
cd spotify-playlist-picker
```

### 3. Install Python dependencies

```bash
python3 -m pip install -r requirements.txt
```

On Windows PowerShell:

```powershell
python -m pip install -r requirements.txt
```

### 3a. Use the web app if you want a simpler launcher

If you want a simpler browser-based launcher instead of the raw CLI, run:

```bash
python3 web.py
```

On Windows PowerShell:

```powershell
python web.py
```

The web app will try to install missing Python packages from [requirements.txt](/Users/kevintang/Downloads/spotify-scripts-main/requirements.txt:1) automatically the first time you run it, so most people only need:

- Python 3
- `pip`
- their own Spotify app credentials

The web app lets people:

- enter Spotify app credentials once and save them locally
- move through the same decision flow as the terminal app without seeing every option at once
- choose common playlist/search options from dropdowns
- watch the run happen in a small status feed that shows only the current phase
- answer follow-up prompts like cache refresh, playlist creation, and playlist naming directly in the browser
- preview generated HTML output inside the web app and open generated files from there
- recognize existing local files in `generated/` and list them in the browser for reopening
- open the local launcher in a browser automatically
- run the public discovery flows even with blank Spotify credentials

The app also tries to be conservative with Spotify API usage now:

- it throttles Spotify Web API calls slightly instead of bursting them all at once
- it honors Spotify `429` backoff windows when they are short enough to retry automatically
- if Spotify keeps rate-limiting for too long, the run exits early instead of hammering the API
- it avoids some unnecessary repeat Spotify lookups when a song already has a Spotify URL or URI
- public discovery no longer spends Spotify quota on track or playlist search

If Spotify says a retry window is over 2 minutes, the current run stops immediately instead of waiting.
For personal-playlist sources, it offers a no-Spotify-API YouTube Music Surprise Me fallback instead of forcing a full restart.
The app also disables Spotipy's own long-lived HTTP retry behavior so these fallback decisions happen immediately instead of waiting on Spotify's full `Retry-After` window.

### 4. Create a Spotify app

You need your own Spotify developer app so the script can authenticate to your account:

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
- Playlist permissions are granted during the OAuth login flow, not in the Spotify dashboard.

### 5. Provide your Spotify credentials

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

On Windows PowerShell:

```powershell
$env:SPOTIPY_CLIENT_ID="your-client-id"
$env:SPOTIPY_CLIENT_SECRET="your-client-secret"
$env:SPOTIPY_REDIRECT_URI="http://127.0.0.1:8080/callback"
python main.py
```

You can also use [.env.example](/Users/kevintang/Downloads/spotify-scripts-main/.env.example:1) as a reference, but this project does not auto-load `.env` files.

If you save credentials through `web.py`, the CLI will reuse those saved app settings automatically on future runs.

### 6. Authorize the app in your browser

The first time you run the script, Spotify should open a browser window and ask you to approve access. The auth token is then stored in `~/.spotify-scripts/token_cache.json` for later runs.

### 7. Run it again later

Once dependencies are installed, the normal command is:

```bash
cd spotify-playlist-picker
python3 main.py
```

On Windows PowerShell:

```powershell
cd spotify-playlist-picker
python main.py
```

## Security improvements

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

Useful flows:

- `2` filters your own playlists by `Rediscover`, `contains`, or `regex`
- `1`, `2`, `3`, and `4` can be limited to `any`, `public only`, or `private only` playlists
- `5` searches YouTube Music by keywords
- public discovery searches YouTube Music playlists first, then falls back to YouTube Music song search if needed
- `5` and the public `Surprise me` mode both support minimum and maximum playlist-size filters
- `6` includes public-discovery surprise mode plus two `random-song.com` modes
- public discovery can limit playlists to a size range with both minimum and maximum song counts
- after songs are chosen, the app automatically adds `Spotify` search links and `YouTube` links
- public discovery can still generate terminal, text, or HTML output even if you do not log into Spotify

## Notes

- `main.py` is the best entrypoint to use.
- For most people, playlist-name `contains` matching is simpler than regex. Regex is available when you want more control.
- Public discovery results carry YouTube links from YouTube Music `videoId` metadata. Spotify links in those flows are usually search pages unless the song already came with an exact Spotify track URL.
- The `song_cache_*.json` files are local caches of playlist track data, not Spotify credentials.
- Generated output files and cache files are now ignored by git, so each machine can create its own local data without shipping personal artifacts in the repo.
- The old root-level `.cache` file was Spotipy's token cache format.
