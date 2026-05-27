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

1. Download or clone this repository.
2. Double-click **`start.command`** in Finder.

The script checks for Python, creates a virtual environment, installs all dependencies, and opens the app in your browser. First run takes about a minute; every run after that is a few seconds.

### If macOS blocks the file

macOS flags scripts downloaded from the internet. There are two ways around it — use whichever feels easier.

**Option A — right-click to open (one time only):**

1. Right-click (or Control-click) `start.command` in Finder.
2. Choose **Open** from the menu.
3. Click **Open** again in the dialog that appears.

macOS remembers your choice, so double-clicking works normally from then on.

**Option B — run it from Terminal (always works):**

Open Terminal, paste this, and press Enter:

```bash
bash ~/Downloads/playlist-maker/start.command
```

Adjust the path if you saved the folder somewhere else. Running via `bash` bypasses Gatekeeper entirely — no approval dialog needed.

**Option C — remove the quarantine flag (one time only):**

Open Terminal, `cd` into the project folder, then run:

```bash
chmod +x start.command
xattr -d com.apple.quarantine start.command
```

After that, double-clicking works normally.

---

## Quick Start — Windows

1. Install Python 3 from [python.org](https://www.python.org/downloads/windows/) if you haven't already.
   - During install, check **"Add python.exe to PATH"** — this is required.
2. Download or clone this repository.
3. Double-click **`start.bat`** in File Explorer.

The script checks for Python, creates a virtual environment, installs all dependencies, and opens the app in your browser. First run takes about a minute; every run after that is a few seconds.

### If Windows blocks the file

Windows SmartScreen flags batch files downloaded from the internet. There are two ways around it.

**Option A — "More info" in the SmartScreen dialog (one time only):**

1. When the blue "Windows protected your PC" dialog appears, click **More info**.
2. Click **Run anyway**.

Windows remembers your choice, so double-clicking works normally from then on.

**Option B — unblock via file properties (one time only):**

1. Right-click `start.bat` in File Explorer → **Properties**.
2. At the bottom of the General tab, check the **Unblock** box.
3. Click **OK**.

After that, double-clicking works without any warning.

**Option C — run it from Command Prompt (always works):**

Open Command Prompt, `cd` into the project folder, then run:

```
start.bat
```

Running from Command Prompt bypasses SmartScreen entirely.

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
