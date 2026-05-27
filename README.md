# Playlist Maker

Playlist Maker finds random songs and can build Spotify playlists from them.

It can:

- pull from all of your playlists, only your own playlists, or a random subset
- limit your personal playlist sources to `any`, `public only`, or `private only`
- filter your playlists by name with a Rediscover preset, plain text, or regex
- discover public songs through YouTube Music metadata, without returning random non-music videos
- use `random-song.com` inside `Surprise me` for truly random song discovery
- automatically add exact Spotify links when possible, with Spotify search fallbacks, plus YouTube links
- export results to `.txt` or `.html`
- create a new private Spotify playlist from the selected songs

Public discovery uses unauthenticated YouTube Music metadata through `ytmusicapi` and avoids Spotify's API entirely. Spotify API access is mainly used for your own playlists and for the final playlist-creation step when the app needs real Spotify track URIs.

---

## Quick Start — Mac

> No terminal experience required. Everything is handled for you.

1. Download or clone this repository.
2. Double-click **`start.command`** in Finder.

That's it. The script will:

- check that Python 3 is installed
- create a self-contained virtual environment inside the project folder (`venv/`)
- install all dependencies inside that virtual environment automatically
- open the Playlist Maker web app in your browser

On first run it installs everything, which may take about a minute. Every run after that starts in a few seconds.

> **If macOS says "start.command cannot be opened because it is from an unidentified developer":**
> Right-click (or Control-click) the file → Open → Open anyway.
> You only need to do this once.

---

## Quick Start — Windows

> No terminal experience required. Everything is handled for you.

1. Install Python 3 from [python.org](https://www.python.org/downloads/windows/) if you haven't already.
   - During install, check **"Add python.exe to PATH"** — this is required.
2. Download or clone this repository.
3. Double-click **`start.bat`** in File Explorer.

That's it. The script will:

- check that Python 3 is installed and on your PATH
- create a self-contained virtual environment inside the project folder (`venv/`)
- install all dependencies inside that virtual environment automatically
- open the Playlist Maker web app in your browser

On first run it installs everything, which may take about a minute. Every run after that starts in a few seconds.

> **If Windows shows a "Windows protected your PC" (SmartScreen) warning:**
> Click **More info** → **Run anyway**.
> You only need to do this once.

---

## Manual Setup

If you prefer the terminal, or you are on Linux, follow these steps.

### 1. Install Python 3

**macOS:**

```bash
# Check if you already have it
python3 --version

# If not, install with Homebrew
brew install python
# or download from https://www.python.org/downloads/
```

**Windows:**

Download Python 3 from [python.org](https://www.python.org/downloads/windows/) and check `Add python.exe to PATH` during install.

**Ubuntu / Debian:**

```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv
```

---

### 2. Clone the repository

```bash
git clone https://github.com/mijucik/playlist-maker.git
cd playlist-maker
```

---

### 3. Create a virtual environment

A virtual environment keeps the app's dependencies isolated from everything else on your machine. This avoids version conflicts and is the recommended approach.

**macOS / Linux:**

```bash
python3 -m venv venv
source venv/bin/activate
```

**Windows (PowerShell):**

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
```

> You will see `(venv)` at the start of your prompt when the virtual environment is active.
> Run the activate command again any time you open a new terminal window.

---

### 4. Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

---

### 5. Run the app

```bash
python3 web.py
```

**Windows:**

```powershell
python web.py
```

The web app opens in your browser automatically. It lets you:

- enter Spotify credentials once and save them locally
- choose options from dropdowns instead of the raw CLI
- watch a live status feed as the run progresses
- answer follow-up prompts (cache refresh, playlist creation, naming) directly in the browser
- see a summary with song count, output filename, link counts, and playlist status
- preview generated HTML inside the app and open generated files from there

To run the plain terminal version instead:

```bash
python3 main.py
```

---

### 6. Create a Spotify app (optional, needed for personal playlists)

Public discovery and Surprise Me work without Spotify credentials. You only need a Spotify app if you want to use your own playlists, get exact Spotify track links, or create a playlist.

1. Sign in at the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard/).
2. Create an app and choose **Web API**.
3. Open the app settings and copy the **Client ID** and **Client Secret**.
4. Add this redirect URI exactly as shown:

```
http://127.0.0.1:8080/callback
```

Use `127.0.0.1`, not `localhost`. The redirect URI must match exactly.

Enter your credentials in the web app settings panel, or export them before running:

**macOS / Linux:**

```bash
export SPOTIPY_CLIENT_ID="your-client-id"
export SPOTIPY_CLIENT_SECRET="your-client-secret"
export SPOTIPY_REDIRECT_URI="http://127.0.0.1:8080/callback"
python3 web.py
```

**Windows (PowerShell):**

```powershell
$env:SPOTIPY_CLIENT_ID="your-client-id"
$env:SPOTIPY_CLIENT_SECRET="your-client-secret"
$env:SPOTIPY_REDIRECT_URI="http://127.0.0.1:8080/callback"
python web.py
```

The first time you use a personal-playlist feature, Spotify opens a browser window asking you to approve access. Your auth token is stored locally in `~/.spotify-scripts/token_cache.json` for future runs.

---

### 7. Running again later

If you used `start.command`, just double-click it. It activates the virtual environment and starts the app.

If you set up manually, re-activate the virtual environment first:

**macOS / Linux:**

```bash
cd playlist-maker
source venv/bin/activate
python3 web.py
```

**Windows:**

```powershell
cd playlist-maker
venv\Scripts\Activate.ps1
python web.py
```

---

## Notes

- The `venv/` folder lives inside the project and is not tracked by git. Each person gets their own.
- Cache files (`song_cache_*.json`) and generated output files are also local and not tracked by git.
- The app throttles Spotify API calls and respects `429` backoff windows automatically. If Spotify's retry window is over 2 minutes, the run stops early rather than waiting.
- Public discovery results carry YouTube links from YouTube Music video metadata. Spotify links resolve to exact tracks when credentials are available, and fall back to Spotify search pages otherwise.
- Auth tokens are cached in `~/.spotify-scripts/token_cache.json` with owner-only permissions.
