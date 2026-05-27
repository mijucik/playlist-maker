#!/usr/bin/env python3

import html
import importlib.util
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


APP_NAME = "spotify-scripts"
APP_DIR = Path.home() / f".{APP_NAME}"
CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8080/callback"
REPO_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8765
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
RUNTIME_SETUP_MESSAGE = ""
WEB_STATUS_PREFIX = "WEB_STATUS_JSON:"
RANDOM_SONG_API_BASE_URL = "https://europe-west1-randommusicgenerator-34646.cloudfunctions.net/appV2"
RANDOM_SONG_OPTIONS_CACHE = None

AUTO_RESPONSE_PROMPTS = {
    "Do you want to open the file now? (yes/no): ": "no",
}

INTERACTIVE_PROMPTS = {
    "Enter Spotify credentials now? (yes/no) [no]: ": {
        "id": "enter_spotify_credentials",
        "type": "choice",
        "choices": ["yes", "no"],
        "placeholder": "",
    },
    "Spotify Client ID: ": {
        "id": "spotify_client_id",
        "type": "text",
        "choices": [],
        "placeholder": "Spotify Client ID",
    },
    "Spotify Client Secret: ": {
        "id": "spotify_client_secret",
        "type": "text",
        "choices": [],
        "placeholder": "Spotify Client Secret",
    },
    "Enter the URL you were redirected to: ": {
        "id": "spotify_redirected_url",
        "type": "text",
        "choices": [],
        "placeholder": "Paste the full redirected URL from your browser",
    },
    "Enable optional Spotify API calls for Spotify links? (yes/no) [yes]: ": {
        "id": "enable_optional_spotify_api",
        "type": "choice",
        "choices": ["yes", "no"],
        "placeholder": "",
    },
    "Do you want to check for updates? (yes/no): ": {
        "id": "check_updates",
        "type": "choice",
        "choices": ["yes", "no"],
        "placeholder": "",
    },
    "Do you want to create a Spotify playlist with these songs? (yes/no): ": {
        "id": "create_playlist",
        "type": "choice",
        "choices": ["yes", "no"],
        "placeholder": "",
    },
    "Enter a name for your new playlist: ": {
        "id": "playlist_name",
        "type": "text",
        "choices": [],
        "placeholder": "Playlist name",
    },
    "Do you want to switch to a web-only Surprise Me fallback instead? (yes/no): ": {
        "id": "no_spotify_fallback",
        "type": "choice",
        "choices": ["yes", "no"],
        "placeholder": "",
    },
    "Do you want to switch to a no-Spotify-API Surprise Me fallback instead? (yes/no): ": {
        "id": "no_spotify_fallback",
        "type": "choice",
        "choices": ["yes", "no"],
        "placeholder": "",
    },
    "Do you want to try YouTube links instead? (yes/no): ": {
        "id": "youtube_link_fallback",
        "type": "choice",
        "choices": ["yes", "no"],
        "placeholder": "",
    },
}

PROMPT_SUFFIXES = list(AUTO_RESPONSE_PROMPTS) + list(INTERACTIVE_PROMPTS)
REQUIRED_MODULES = {
    "spotipy": "spotipy==2.26.0",
    "tqdm": "tqdm==4.66.5",
    "ytmusicapi": "ytmusicapi==1.12.0",
}
GENERATED_DIR = REPO_DIR / "generated"
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
WEB_PROGRESS_RE = re.compile(r"(?:\d+%\||█{2,}|song/s|track/s|playlist/s|it/s)")
WEB_NOISE_EXACT_LINES = {
    "1. My Spotify Playlists",
    "1. My Spotify playlists",
    "2. Public Discovery",
    "2. Public discovery",
    "3. Surprise Me",
    "3. Surprise me",
    "Links to include:",
    "1. No links",
    "2. YouTube only",
    "3. Spotify only",
    "3. Spotify only (requires Spotify API access)",
    "4. Spotify and YouTube",
    "4. Spotify and YouTube (Spotify requires API access)",
    "1. All playlists",
    "2. Filter by name",
    "3. Own playlists only",
    "4. Random selection",
    "1. Rediscover preset",
    "2. Contains text",
    "3. Custom regex",
    "1. Random emotions/genres via public discovery",
    "2. random-song.com with its default random configuration",
    "3. random-song.com with custom configuration",
    "1. Any",
    "2. Public only",
    "3. Private only",
}
WEB_NOISE_PREFIXES = (
    "Successfully authenticated as",
    "Spotify API credentials are not configured.",
    "They are needed for",
    "Spotify links need",
    "Spotify links require",
    # "Spotify may ask you to authorize" and "If asked for the redirected URL"
    # are intentionally NOT filtered so users see the OAuth instruction.
    "Enter Spotify credentials now",
    "Spotify Client ID:",
    "Spotify Client Secret:",
    "Enter the URL you were redirected to:",
    "Enable optional Spotify API calls",
    "Credentials saved.",
    "Loaded ",  # special-cased below: shows cache counts, hides startup emotion list
    "How many songs?",
    "Source:",
    "Playlist selection:",
    "Playlist visibility:",
    "Choose link output, 1-4",
    "Filter playlists by name:",
    "Surprise Me mode:",
    "Enter 1, 2, or 3",
    "Enter 1, 2, 3, or 4",
    "Enter a name for your new playlist:",
    "Max playlists to search",
    "Min songs per playlist?",
    "Max songs per playlist?",
    "Max songs to collect before selecting?",
    "Enter one or more words or phrases",
    "Output format (",
    "Avoid optional Spotify API calls",
    "Choose link output, 1-4",
    "Text to match in playlist names:",
    "Text to match in playlist names:",
    "Regex for playlist names:",
    "Configure random-song.com",
    "Do you want to create a Spotify playlist with these songs?",
    "Do you want to open the file now?",
    "Do you want to switch to a web-only Surprise Me fallback instead?",
    "Do you want to switch to a no-Spotify-API Surprise Me fallback instead?",
    "Do you want to try YouTube links instead?",
    "Starting YouTube Music discovery",
    "YouTube Music is the source of truth",
    "Searching YouTube Music",
    "Reading YouTube Music playlist:",
    "Skipping YouTube Music playlist",
    "Recovered ",
    "Compiled ",
    "Finding Spotify links",
    "Building Spotify links",
    "Looking up YouTube links",
    "Spotify API unavailable for Spotify song matching:",
    "Output file will be:",
)


def ensure_private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def load_local_config():
    ensure_private_directory(APP_DIR)
    if not CONFIG_PATH.exists():
        return {}

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
            loaded = json.load(config_file)
            return loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_local_config(config_data):
    ensure_private_directory(APP_DIR)
    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f"{CONFIG_PATH.stem}_",
        suffix=".tmp",
        dir=str(APP_DIR),
    )
    temp_path = Path(temp_name)
    with os.fdopen(temp_fd, "w", encoding="utf-8") as config_file:
        json.dump(config_data, config_file, indent=2)
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def escape(value):
    return html.escape(value or "")


def random_song_api_get(path):
    url = f"{RANDOM_SONG_API_BASE_URL}{path}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.load(response)


def get_random_song_options():
    global RANDOM_SONG_OPTIONS_CACHE
    if RANDOM_SONG_OPTIONS_CACHE is not None:
        return RANDOM_SONG_OPTIONS_CACHE

    try:
        RANDOM_SONG_OPTIONS_CACHE = {
            "markets": random_song_api_get("/getMarkets").get("data", []),
            "genres": random_song_api_get("/getGenres").get("data", []),
            "decades": random_song_api_get("/getDecades").get("data", []),
        }
    except Exception:
        RANDOM_SONG_OPTIONS_CACHE = {
            "markets": [],
            "genres": [],
            "decades": [],
        }
    return RANDOM_SONG_OPTIONS_CACHE


def option_value(entry):
    if isinstance(entry, dict):
        return entry.get("name") or entry.get("value") or ""
    return str(entry or "")


def render_select_options(current, entries, defaults):
    values = []
    seen = set()
    for value in defaults:
        if value not in seen:
            values.append(value)
            seen.add(value)
    for entry in entries:
        value = option_value(entry)
        if value and value not in seen:
            values.append(value)
            seen.add(value)
    if current and current not in seen:
        values.insert(0, current)

    return "\n".join(
        f'<option value="{escape(value)}"{" selected" if current == value else ""}>{escape(value)}</option>'
        for value in values
    )


def strip_ansi(text):
    return ANSI_ESCAPE_RE.sub("", text or "")


def should_include_web_output_line(line):
    cleaned = strip_ansi(line).replace("\r", "").strip()
    if not cleaned:
        return False

    if set(cleaned) in ({"-"}, {"="}):
        return False

    if cleaned in WEB_NOISE_EXACT_LINES:
        return False

    if any(cleaned.startswith(prefix) for prefix in WEB_NOISE_PREFIXES):
        if cleaned.startswith("Loaded ") and "emotions/genres" not in cleaned:
            return True
        return False

    if WEB_PROGRESS_RE.search(cleaned):
        return False

    return True


def ensure_cli_dependencies():
    missing_requirements = [
        requirement
        for module_name, requirement in REQUIRED_MODULES.items()
        if importlib.util.find_spec(module_name) is None
    ]

    if not missing_requirements:
        return ""

    print("Missing Python packages detected. Installing them now...")
    install_command = [sys.executable, "-m", "pip", "install", "-r", str(REPO_DIR / "requirements.txt")]
    result = subprocess.run(
        install_command,
        cwd=REPO_DIR,
        text=True,
        capture_output=True,
    )

    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "Unknown pip error."
        raise RuntimeError(f"Could not auto-install dependencies: {stderr}")

    return "Installed missing CLI dependencies automatically."


def get_saved_spotify_config():
    return load_local_config().get("spotify_app", {})


def list_generated_files(limit=12):
    if not GENERATED_DIR.exists():
        return []

    generated_files = [
        path for path in GENERATED_DIR.iterdir()
        if path.is_file() and not path.name.startswith(".")
    ]
    generated_files.sort(key=lambda path: path.stat().st_mtime, reverse=True)

    entries = []
    for path in generated_files[:limit]:
        entries.append(
            {
                "name": path.name,
                "relative_path": path.relative_to(REPO_DIR).as_posix(),
                "kind": path.suffix.lower().lstrip(".") or "file",
                "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "url": "/generated-file?path=" + urllib.parse.quote(path.relative_to(REPO_DIR).as_posix()),
            }
        )
    return entries


def parse_form_data(raw_body):
    parsed = urllib.parse.parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items()}


def build_default_form_data():
    spotify_config = get_saved_spotify_config()
    return {
        "client_id": spotify_config.get("client_id", ""),
        "client_secret": spotify_config.get("client_secret", ""),
        "redirect_uri": spotify_config.get("redirect_uri", DEFAULT_REDIRECT_URI),
        "num_songs": "1",
        "source": "My Spotify Playlists",
        "playlist_scope": "All playlists",
        "visibility": "Any visibility",
        "filter_mode": "Rediscover preset",
        "filter_value": "",
        "max_playlists": "10",
        "min_playlist_size": "",
        "max_playlist_size": "100",
        "max_songs_pool": "",
        "discovery_mode": "YouTube Music discovery",
        "avoid_spotify_api": "on",
        "keywords": "",
        "surprise_mode": "Random emotions/genres via public discovery",
        "random_genre": "random",
        "random_market": "random",
        "random_decade": "random",
        "random_new": "",
        "random_exclude_singles": "",
        "link_platform": "2",
        "output_format": "terminal",
        "verbose": "",
    }


def merge_with_defaults(form_data):
    defaults = build_default_form_data()
    defaults.update({key: value for key, value in form_data.items() if value is not None})
    return defaults


def build_initial_cli_answers(form):
    lines = [form["num_songs"].strip() or "1"]

    source = form["source"]

    if source == "My Spotify Playlists":
        lines.append("1")  # top-level: My Spotify Playlists
        playlist_scope = form.get("playlist_scope", "All playlists")
        scope_choice = {
            "All playlists": "1",
            "Filter by name": "2",
            "Own playlists only": "3",
            "Random selection": "4",
        }.get(playlist_scope, "1")
        lines.append(scope_choice)
        lines.append({
            "Any visibility": "1",
            "Public only": "2",
            "Private only": "3",
        }[form["visibility"]])
        if scope_choice == "2":
            filter_choice = {
                "Rediscover preset": "1",
                "Contains text": "2",
                "Custom regex": "3",
            }[form["filter_mode"]]
            lines.append(filter_choice)
            if filter_choice in {"2", "3"}:
                lines.append(form["filter_value"].strip())

    elif source == "Public discovery":
        lines.append("2")  # top-level: Public Discovery
        lines.append("yes" if form.get("avoid_spotify_api") else "no")
        lines.append(form["max_playlists"].strip() or "10")
        lines.append(form["min_playlist_size"].strip())
        lines.append(form["max_playlist_size"].strip())
        lines.append(form.get("max_songs_pool", "").strip())
        lines.append(form["keywords"].strip())

    elif source == "Surprise me":
        lines.append("3")  # top-level: Surprise Me
        lines.append("yes" if form.get("avoid_spotify_api") else "no")
        surprise_choice = {
            "Random emotions/genres via public discovery": "1",
            "random-song.com default": "2",
            "random-song.com custom": "3",
        }[form["surprise_mode"]]
        lines.append(surprise_choice)
        if surprise_choice == "1":
            lines.append(form["max_playlists"].strip() or "10")
            lines.append(form["min_playlist_size"].strip())
            lines.append(form["max_playlist_size"].strip())
            lines.append(form.get("max_songs_pool", "").strip())
        elif surprise_choice == "3":
            lines.append(form["random_genre"].strip() or "random")
            lines.append(form["random_market"].strip() or "random")
            lines.append(form["random_decade"].strip() or "random")
            lines.append("yes" if form.get("random_new") else "no")
            lines.append("yes" if form.get("random_exclude_singles") else "no")

    # link_platform and output_format are NOT pre-written here.
    # They are answered via form_auto_responses in InteractiveRunSession when
    # those exact prompt strings appear in subprocess stdout. This prevents
    # misalignment when ensure_spotify_link_access() inserts extra interactive
    # prompts (e.g. "Enable optional Spotify API calls?") between link_platform
    # and output_format, which would otherwise consume the output_format answer
    # from the pre-written buffer and cause prompt_for_output_format() to freeze.

    return "\n".join(lines) + "\n"


def form_requires_spotify_credentials(form):
    return form["source"] == "My Spotify Playlists"


def validate_form(form):
    if form_requires_spotify_credentials(form) and (not form["client_id"].strip() or not form["client_secret"].strip()):
        return "Client ID and Client Secret are required."

    num_songs_value = form["num_songs"].strip().lower()
    try:
        if num_songs_value != "one" and int(num_songs_value or "1") < 1:
            return "Number of songs must be at least 1."
    except ValueError:
        return "Number of songs must be 'one' or a positive integer."

    if (
        form["source"] == "My Spotify Playlists"
        and form.get("playlist_scope") == "Filter by name"
        and form["filter_mode"] in {"Contains text", "Custom regex"}
    ):
        if not form["filter_value"].strip():
            return "Enter playlist filter text or regex."

    if form["source"] == "Public discovery":
        if not form["keywords"].strip():
            return "Enter at least one keyword for public discovery."
        if not form["max_playlists"].strip():
            return "Enter a maximum number of playlists."

    if form["source"] == "Surprise me" and form["surprise_mode"] == "Random emotions/genres via public discovery":
        if not form["max_playlists"].strip():
            return "Enter a maximum number of playlists for public surprise mode."

    if form["source"] in {"Public discovery", "Surprise me"} and form["max_playlists"].strip():
        try:
            if int(form["max_playlists"].strip()) < 1:
                return "Max playlists must be a positive integer."
        except ValueError:
            return "Max playlists must be a positive integer."

    min_size_raw = form["min_playlist_size"].strip()
    max_size_raw = form["max_playlist_size"].strip()
    max_songs_pool_raw = form.get("max_songs_pool", "").strip()
    try:
        min_size = int(min_size_raw) if min_size_raw else None
        max_size = int(max_size_raw) if max_size_raw else None
        max_songs_pool = int(max_songs_pool_raw) if max_songs_pool_raw else None
    except ValueError:
        return "Playlist size limits and max songs must be positive integers when provided."

    if min_size is not None and min_size < 1:
        return "Minimum playlist size must be positive."
    if max_size is not None and max_size < 1:
        return "Maximum playlist size must be positive."
    if min_size is not None and max_size is not None and min_size > max_size:
        return "Minimum playlist size cannot be greater than maximum playlist size."
    if max_songs_pool is not None and max_songs_pool < 1:
        return "Max songs to collect must be a positive integer when provided."
    if form.get("link_platform", "2") not in {"1", "2", "3", "4"}:
        return "Link option must be No links, YouTube only, Spotify only, or Spotify and YouTube."

    if form["output_format"] not in {"terminal", "html", "txt"}:
        return "Output format must be terminal, html, or txt."

    return None


def save_credentials_from_form(form):
    config_data = load_local_config()
    config_data["spotify_app"] = {
        "client_id": form["client_id"].strip(),
        "client_secret": form["client_secret"].strip(),
        "redirect_uri": form["redirect_uri"].strip() or DEFAULT_REDIRECT_URI,
    }
    save_local_config(config_data)


class InteractiveRunSession:
    def __init__(self, form):
        self.id = uuid.uuid4().hex
        self.form = form
        self.process = None
        self.thread = None
        self.lock = threading.Lock()
        self.response_event = threading.Event()
        self.pending_answer = None
        self.raw_output = ""
        self.display_output = ""
        self.tail = ""
        self.display_line_buffer = ""
        self.finished = False
        self.exit_code = None
        self.current_prompt = None
        self.status = "Starting..."
        self.status_phase = "Starting"
        self.status_lines = ["Starting..."]
        self.summary = None
        self.summary_kind = None
        self.report = {}
        self.generated_file = None
        self.error = None
        self.was_cancelled = False
        # Per-session auto-responses: when these exact prompt strings appear in
        # subprocess stdout, reply immediately with the form's pre-selected value.
        # This avoids consuming pre-written stdin buffer entries out of order when
        # ensure_spotify_link_access() inserts extra prompts at runtime.
        self.form_auto_responses = {
            "Enter 1, 2, 3, or 4 [2]: ": form.get("link_platform", "2"),
            "Output format (txt / html / terminal): ": form.get("output_format", "terminal"),
        }

    def start(self):
        with self.lock:
            self.status = "Running..."
        env = os.environ.copy()
        env["SPOTIPY_CLIENT_ID"] = self.form["client_id"].strip()
        env["SPOTIPY_CLIENT_SECRET"] = self.form["client_secret"].strip()
        env["SPOTIPY_REDIRECT_URI"] = self.form["redirect_uri"].strip() or DEFAULT_REDIRECT_URI
        env["SPOTIFY_PICKER_WEB_STATUS"] = "1"
        env["PYTHONUTF8"] = "1"

        self.process = subprocess.Popen(
            [sys.executable, "main.py"],
            cwd=REPO_DIR,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=0,
        )

        initial_answers = build_initial_cli_answers(self.form)
        self.process.stdin.write(initial_answers)
        self.process.stdin.flush()

        self.thread = threading.Thread(target=self._read_output_loop, daemon=True)
        self.thread.start()

    def _read_output_loop(self):
        try:
            while True:
                chunk = self.process.stdout.read(1)
                if not chunk:
                    break
                self._handle_output_chunk(chunk)

            self.process.wait()
            with self.lock:
                self._flush_display_line_locked()
                self.finished = True
                self.exit_code = self.process.returncode
                if self.was_cancelled:
                    self.status = "Run cancelled."
                    self.summary = "Run cancelled."
                    self.summary_kind = "error"
                elif self.exit_code == 0:
                    self.status = "Run finished."
                    if not self.summary:
                        self.summary = self.build_completion_summary_locked()
                        self.summary_kind = self.build_summary_kind_locked()
                else:
                    self.status = f"Run exited with code {self.exit_code}."
                    self.summary = f"The run stopped before completing. Exit code: {self.exit_code}."
                    self.summary_kind = "error"
                self.current_prompt = None
        except Exception as error:
            with self.lock:
                self.finished = True
                self.error = str(error)
                self.status = f"Run failed: {error}"
                self.summary = f"Run failed: {error}"
                self.summary_kind = "error"

    def _handle_output_chunk(self, chunk):
        pending_prompt = None
        auto_response = None

        with self.lock:
            self.raw_output += chunk
            self.tail = (self.tail + chunk)[-300:]
            if chunk == "\r":
                self.display_line_buffer = ""
            elif chunk == "\n":
                self._flush_display_line_locked()
                self._parse_recent_output_locked()
            else:
                self.display_line_buffer += chunk

            for prompt_text, response in AUTO_RESPONSE_PROMPTS.items():
                if self.tail.endswith(prompt_text):
                    auto_response = response
                    break

            if auto_response is None:
                for prompt_text, response in self.form_auto_responses.items():
                    if self.tail.endswith(prompt_text):
                        auto_response = response
                        break

            if auto_response is None:
                for prompt_text, prompt_meta in INTERACTIVE_PROMPTS.items():
                    if self.tail.endswith(prompt_text):
                        pending_prompt = {
                            "prompt_text": prompt_text,
                            **prompt_meta,
                        }
                        self.current_prompt = pending_prompt
                        self.status = "Waiting for your answer..."
                        break

        if auto_response is not None:
            self._write_answer(auto_response)
            return

        if pending_prompt is not None:
            self.response_event.clear()
            self.response_event.wait()
            with self.lock:
                answer = self.pending_answer if self.pending_answer is not None else ""
                self.pending_answer = None
                self.current_prompt = None
                self.status = "Running..."
            self._write_answer(answer)

    def _write_answer(self, answer):
        if not self.process or not self.process.stdin:
            return

        try:
            self.process.stdin.write(answer + "\n")
            self.process.stdin.flush()
            with self.lock:
                self.raw_output += answer + "\n"
                self.tail = (self.tail + answer + "\n")[-300:]
                self._parse_recent_output_locked()
        except Exception:
            pass

    def respond(self, answer):
        with self.lock:
            if not self.current_prompt:
                return False
            self.pending_answer = answer
        self.response_event.set()
        return True

    def cancel(self):
        if self.process and self.process.poll() is None:
            self.process.kill()
        with self.lock:
            self.was_cancelled = True
            self.finished = True
            self.status = "Run cancelled."
            self.current_prompt = None
            self.pending_answer = ""
        self.response_event.set()

    def _parse_recent_output_locked(self):
        for line in self.raw_output.splitlines()[-20:]:
            if line.startswith("Output file will be: "):
                possible_path = Path(line.split("Output file will be: ", 1)[1].strip())
                self.generated_file = possible_path
            elif "written to '" in line:
                match = re.search(r"written to '([^']+)'", line)
                if match:
                    possible_path = Path(match.group(1))
                    self.generated_file = possible_path

    def _flush_display_line_locked(self):
        line = strip_ansi(self.display_line_buffer)
        self.display_line_buffer = ""
        cleaned = line.strip()
        if not cleaned:
            return
        marker_index = cleaned.find(WEB_STATUS_PREFIX)
        if marker_index != -1:
            self._record_status_payload_locked(cleaned[marker_index + len(WEB_STATUS_PREFIX):])
            return
        if not should_include_web_output_line(cleaned):
            return
        self.display_output += line.rstrip() + "\n"

    def _record_status_payload_locked(self, raw_payload):
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            return

        phase = payload.get("phase") or self.status_phase
        message = payload.get("message")
        level = payload.get("level", "info")
        if phase != "report":
            self.status_phase = phase.replace("-", " ").title()
        if payload.get("reset"):
            self.status_lines = []
        if message:
            self.status_lines.append(message)
            self.status_lines = self.status_lines[-4:]
        if level == "error":
            self.summary = message or "The run hit an error."
            self.summary_kind = "error"
        if isinstance(payload.get("report"), dict):
            self._merge_report_locked(payload["report"])

    def _merge_report_locked(self, report):
        for key, value in report.items():
            if key in {"warnings", "errors", "surprise_attempts"}:
                existing = self.report.setdefault(key, [])
                for item in value if isinstance(value, list) else [value]:
                    if item and item not in existing:
                        existing.append(item)
            else:
                self.report[key] = value

    def build_completion_summary_locked(self):
        report = self.report
        if "No tracks found." in self.display_output:
            return "No tracks found. Try broader keywords or relax the playlist size limits."
        if report.get("errors") and not report.get("selected_count"):
            return escape("; ".join(report["errors"]))

        main_parts = []
        notes_parts = []

        selected_count = report.get("selected_count")
        # Headline: keep the conclusion concise. The selected inputs remain visible above.
        if selected_count is not None:
            n = selected_count
            song_word = "song" if n == 1 else "songs"
            picked = f"<strong>{n} {song_word}</strong>"
            headline = f"Picked {picked}."
            main_parts.append(f'<p class="summary-headline">{headline}</p>')

        # Output
        if self.generated_file and self.generated_file.exists():
            kind = self.generated_file.suffix.lower().lstrip(".").upper() or "file"
            filename = escape(report.get("artifact_name") or self.generated_file.name)
            main_parts.append(f'<p>{kind} file <strong>{filename}</strong> was generated.</p>')
        elif report.get("output_format") == "terminal" or self.display_output.strip():
            main_parts.append("<p>Songs are shown below.</p>")

        # Links
        if report.get("links_added"):
            exact_links = report.get("spotify_exact_links", 0)
            search_links = report.get("spotify_search_links", 0)
            youtube_links = report.get("youtube_links", 0)
            link_parts = []
            if exact_links:
                link_parts.append(f"{exact_links} Spotify")
            if youtube_links:
                link_parts.append(f"{youtube_links} YouTube")
            if link_parts:
                total = exact_links + youtube_links
                link_word = "link" if total == 1 else "links"
                main_parts.append(f"<p>Each song includes {' and '.join(link_parts)} {link_word}.</p>")
            if search_links:
                notes_parts.append(
                    f"<p>{search_links} song(s) linked to a Spotify search instead of a direct match — "
                    f"exact track wasn't found.</p>"
                )

        # Playlist creation
        playlist_status = report.get("spotify_playlist_status")
        if playlist_status:
            playlist_name = report.get("spotify_playlist_name")
            status_lower = playlist_status.lower()
            name_part = f' "{escape(playlist_name)}"' if playlist_name else ""
            if any(w in status_lower for w in ("fail", "error", "could not", "unable")):
                notes_parts.append(f"<p>Spotify playlist{name_part} couldn't be created: {escape(playlist_status)}</p>")
            elif "skip" in status_lower:
                notes_parts.append(f"<p>No Spotify playlist was created.</p>")
            else:
                main_parts.append(f"<p>Saved to Spotify playlist{name_part}: {escape(playlist_status)}</p>")

        for warning in report.get("warnings", []):
            notes_parts.append(f"<p>{escape(warning)}</p>")
        for error in report.get("errors", []):
            if error != "No tracks found.":
                notes_parts.append(f"<p>{escape(error)}</p>")

        if not main_parts:
            if self.display_output.strip():
                main_parts.append("<p>Songs are shown below.</p>")
            else:
                main_parts.append("<p>Run complete.</p>")

        html_out = f'<div class="summary-main">{"".join(main_parts)}</div>'
        if notes_parts:
            html_out += f'<div class="summary-notes">{"".join(notes_parts)}</div>'
        return html_out

    def build_summary_kind_locked(self):
        if "No tracks found." in self.display_output:
            return "error"
        if self.report.get("errors"):
            return "error"
        return "success"

    def build_verbose_output_locked(self):
        if not self.form.get("verbose"):
            return ""
        lines = []
        for raw_line in strip_ansi(self.raw_output).splitlines():
            cleaned = raw_line.strip()
            if cleaned.startswith(WEB_STATUS_PREFIX):
                continue
            lines.append(raw_line)
        return "\n".join(lines).strip()

    def artifact_url(self):
        if not self.generated_file or not self.generated_file.exists():
            return None
        return f"/artifact?session_id={urllib.parse.quote(self.id)}"

    def serialize(self):
        with self.lock:
            artifact_path = str(self.generated_file) if self.generated_file else None
            artifact_name = self.generated_file.name if self.generated_file else None
            artifact_kind = None
            artifact_url = None
            if self.generated_file and self.generated_file.exists():
                artifact_kind = self.generated_file.suffix.lower().lstrip(".")
                artifact_url = self.artifact_url()

            return {
                "session_id": self.id,
                "status": self.status,
                "finished": self.finished,
                "exit_code": self.exit_code,
                "output": self.display_output,
                "verbose_output": self.build_verbose_output_locked(),
                "status_feed": {
                    "phase": self.status_phase,
                    "lines": self.status_lines,
                    "summary": self.summary,
                    "kind": self.summary_kind,
                },
                "report": self.report,
                "prompt": self.current_prompt,
                "artifact_path": artifact_path,
                "artifact_name": artifact_name,
                "artifact_kind": artifact_kind,
                "artifact_url": artifact_url,
                "error": self.error,
            }


def create_session(form):
    session = InteractiveRunSession(form)
    with SESSIONS_LOCK:
        SESSIONS[session.id] = session
    session.start()
    return session


def get_session(session_id):
    with SESSIONS_LOCK:
        return SESSIONS.get(session_id)


def render_page(form, status="Ready."):
    option = lambda current, value: " selected" if current == value else ""
    checked = lambda name: " checked" if form.get(name) else ""
    random_options = get_random_song_options()
    random_genre_options = render_select_options(form["random_genre"], random_options["genres"], ["random", "none"])
    random_market_options = render_select_options(form["random_market"], random_options["markets"], ["random"])
    random_decade_options = render_select_options(form["random_decade"], random_options["decades"], ["random", "all"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Playlist Maker</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 0;
      background: linear-gradient(180deg, #f7f7f2 0%, #ecebe3 100%);
      color: #1f2a21;
    }}
    .page {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 24px;
    }}
    h1 {{ margin-top: 0; }}
    .card {{
      background: white;
      border-radius: 10px;
      padding: 18px;
      margin-bottom: 16px;
      box-shadow: 0 12px 30px rgba(0, 0, 0, 0.06);
    }}
    .grid {{
      display: grid;
      grid-template-columns: 220px 1fr;
      gap: 12px 16px;
      align-items: center;
    }}
    input, select, button, textarea {{
      font: inherit;
    }}
    input, select {{
      padding: 10px 12px;
      border: 1px solid #c8cfbf;
      border-radius: 10px;
      width: 100%;
      box-sizing: border-box;
    }}
    input[type="checkbox"] {{
      width: auto;
      padding: 0;
    }}
    .row {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .checkbox-line {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 12px;
      color: #334538;
      font-weight: 600;
    }}
    .compact-card {{
      padding-bottom: 18px;
    }}
    button {{
      border: 0;
      border-radius: 999px;
      padding: 10px 16px;
      background: #1f7a4d;
      color: white;
      cursor: pointer;
    }}
    button.secondary {{
      background: #334538;
    }}
    button.ghost {{
      background: #dfe8df;
      color: #183323;
    }}
    .artifact-open {{
      display: inline-block;
      padding: 10px 16px;
      border-radius: 999px;
      background: #25372c;
      color: #fff;
      text-decoration: none;
      font-weight: 700;
    }}
    .status {{
      white-space: pre-wrap;
      color: #234a31;
      font-weight: 600;
    }}
    .hidden {{
      display: none;
    }}
    .section-title {{
      margin-top: 18px;
      margin-bottom: 10px;
    }}
    .section-copy {{
      color: #536257;
      margin-top: 0;
      margin-bottom: 14px;
    }}
    pre {{
      white-space: pre-wrap;
      background: #101512;
      color: #e7f2e9;
      padding: 16px;
      border-radius: 12px;
      overflow-x: auto;
      min-height: 260px;
      max-height: 520px;
      overflow-y: auto;
    }}
    .status-feed {{
      border: 1px solid #d8dfd8;
      border-radius: 10px;
      padding: 16px;
      background: #fbfcf8;
      min-height: 120px;
    }}
    .status-phase {{
      color: #536257;
      font-size: 0.9rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      margin-bottom: 10px;
    }}
    .status-line {{
      display: flex;
      gap: 10px;
      align-items: center;
      margin: 8px 0;
      color: #1f2a21;
    }}
    .pulse {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #4d6f5a;
      animation: pulse 1s ease-in-out infinite;
      flex: 0 0 auto;
    }}
    .summary-card {{
      border: 1px solid #d8dfd8;
      border-radius: 10px;
      padding: 16px;
      background: #faf8ef;
      color: #25372c;
      font-weight: 600;
    }}
    .summary-card.error {{
      border-color: #e0b4ad;
      background: #fff5f2;
      color: #9a2d1d;
    }}
    .summary-main {{ margin: 0; }}
    .summary-main p {{ margin: 3px 0; }}
    .summary-headline {{ font-size: 1.05em; font-weight: 700; margin: 0 0 8px; }}
    .summary-notes {{
      margin-top: 14px;
      padding-top: 12px;
      border-top: 1px solid #d0c8a8;
      color: #7a5c1e;
      font-weight: 600;
      font-size: 0.95em;
    }}
    .summary-notes p {{ margin: 3px 0; }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 0.35; transform: scale(0.85); }}
      50% {{ opacity: 1; transform: scale(1.15); }}
    }}
    .hint {{
      color: #536257;
      font-size: 0.95rem;
      margin-top: 0;
    }}
    iframe {{
      width: 100%;
      min-height: 520px;
      border: 1px solid #d8dfd8;
      border-radius: 12px;
      background: white;
    }}
    .generated-list {{
      padding-left: 18px;
      margin: 0;
    }}
    .generated-list li {{
      margin-bottom: 10px;
    }}
    .generated-list a {{
      color: #1f7a4d;
      text-decoration: none;
      font-weight: 600;
    }}
    @media (max-width: 720px) {{
      .grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <h1>Playlist Maker</h1>

    <form method="post" action="/save" class="card compact-card" id="settings-form">
      <h2>Spotify App Settings</h2>
      <button type="button" class="ghost" id="spotify-settings-toggle">Tweak Spotify settings</button>
      <div class="hidden" id="spotify-settings-fields" style="margin-top: 14px;">
        <p class="section-copy">Needed for personal playlists, Spotify links, and Spotify playlist creation.</p>
        <div class="grid">
          <label for="client_id">Client ID</label>
          <input id="client_id" name="client_id" value="{escape(form['client_id'])}">

          <label for="client_secret">Client Secret</label>
          <input id="client_secret" name="client_secret" value="{escape(form['client_secret'])}">

          <label for="redirect_uri">Redirect URI</label>
          <input id="redirect_uri" name="redirect_uri" value="{escape(form['redirect_uri'])}">
        </div>
        <div class="row" style="margin-top: 14px;">
          <button type="submit">Save Settings</button>
          <button class="secondary" type="button" onclick="window.open('https://developer.spotify.com/dashboard/', '_blank')">Open Spotify Dashboard</button>
        </div>
      </div>
    </form>

    <form class="card" id="run-form">
      <h2>Run Options</h2>
      <input type="hidden" id="run-client-id" name="client_id" value="{escape(form['client_id'])}">
      <input type="hidden" id="run-client-secret" name="client_secret" value="{escape(form['client_secret'])}">
      <input type="hidden" id="run-redirect-uri" name="redirect_uri" value="{escape(form['redirect_uri'])}">

      <div class="grid">
        <label for="num_songs">Number of songs</label>
        <input id="num_songs" name="num_songs" value="{escape(form['num_songs'])}">

        <label for="source">Source</label>
        <select id="source" name="source">
          <option{option(form['source'], 'My Spotify Playlists')}>My Spotify Playlists</option>
          <option{option(form['source'], 'Public discovery')}>Public discovery</option>
          <option{option(form['source'], 'Surprise me')}>Surprise me</option>
        </select>
      </div>

      <div id="playlist-scope-section">
        <h3 class="section-title">Playlist Selection</h3>
        <p class="section-copy">Choose how to pick playlists from your Spotify library.</p>
        <div class="grid">
          <label for="playlist_scope">Selection</label>
          <select id="playlist_scope" name="playlist_scope">
            <option value="All playlists"{option(form['playlist_scope'], 'All playlists')}>All playlists — your entire Spotify library</option>
            <option value="Filter by name"{option(form['playlist_scope'], 'Filter by name')}>Filter by name — match by keyword, preset, or regex</option>
            <option value="Own playlists only"{option(form['playlist_scope'], 'Own playlists only')}>Own playlists only — playlists you created</option>
            <option value="Random selection"{option(form['playlist_scope'], 'Random selection')}>Random selection — sample across your playlists</option>
          </select>
        </div>
      </div>

      <div id="visibility-section">
        <h3 class="section-title">Playlist Visibility</h3>
        <p class="section-copy">Limit to public, private, or any playlist visibility.</p>
        <div class="grid">
          <label for="visibility">Playlist visibility</label>
          <select id="visibility" name="visibility">
            <option{option(form['visibility'], 'Any visibility')}>Any visibility</option>
            <option{option(form['visibility'], 'Public only')}>Public only</option>
            <option{option(form['visibility'], 'Private only')}>Private only</option>
          </select>
        </div>
      </div>

      <div id="playlist-filter-section">
        <h3 class="section-title">Playlist Name Filter</h3>
        <p class="section-copy">This only appears when you choose to filter your own playlists by name.</p>
        <div class="grid">
          <label for="filter_mode">Filter mode</label>
          <select id="filter_mode" name="filter_mode">
            <option{option(form['filter_mode'], 'Rediscover preset')}>Rediscover preset</option>
            <option{option(form['filter_mode'], 'Contains text')}>Contains text</option>
            <option{option(form['filter_mode'], 'Custom regex')}>Custom regex</option>
          </select>

          <label for="filter_value" id="filter-value-label">Filter text or regex</label>
          <input id="filter_value" name="filter_value" value="{escape(form['filter_value'])}">
        </div>
      </div>

      <div id="surprise-section">
        <h3 class="section-title">Surprise Me</h3>
        <p class="section-copy">Choose whether Surprise Me uses public discovery or random-song.com.</p>
        <div class="grid">
          <label for="surprise_mode">Mode</label>
          <select id="surprise_mode" name="surprise_mode">
            <option{option(form['surprise_mode'], 'Random emotions/genres via public discovery')}>Random emotions/genres via public discovery</option>
            <option{option(form['surprise_mode'], 'random-song.com default')}>random-song.com default</option>
            <option{option(form['surprise_mode'], 'random-song.com custom')}>random-song.com custom</option>
          </select>
        </div>
      </div>

      <div id="public-discovery-options-section">
        <h3 class="section-title">Public Discovery Settings</h3>
        <p class="section-copy">Optional limits for public discovery.</p>
        <input type="hidden" id="discovery_mode" name="discovery_mode" value="YouTube Music discovery">
        <label class="checkbox-line">
          <input type="checkbox" name="avoid_spotify_api"{checked('avoid_spotify_api')}>
          Avoid optional Spotify API calls
        </label>
        <button type="button" class="ghost" id="advanced-discovery-toggle">Tweak discovery settings</button>
        <div class="grid hidden" id="advanced-discovery-fields" style="margin-top: 14px;">
          <label for="max_playlists">Max playlists</label>
          <input id="max_playlists" name="max_playlists" value="{escape(form['max_playlists'])}">

          <label for="min_playlist_size">Min playlist size</label>
          <input id="min_playlist_size" name="min_playlist_size" value="{escape(form['min_playlist_size'])}">

          <label for="max_playlist_size">Max playlist size</label>
          <input id="max_playlist_size" name="max_playlist_size" value="{escape(form['max_playlist_size'])}">

          <label for="max_songs_pool">Max songs to collect</label>
          <input id="max_songs_pool" name="max_songs_pool" placeholder="blank = 20 × max playlists" value="{escape(form.get('max_songs_pool', ''))}">
        </div>
      </div>

      <div id="public-keywords-section">
        <h3 class="section-title">Public Discovery Keywords</h3>
        <p class="section-copy">Enter one or more words or phrases, like <code>"happy"</code>, <code>"yacht rock"</code>, or <code>"summer jazz"</code>.</p>
        <div class="grid">
          <label for="keywords">Keywords</label>
          <input id="keywords" name="keywords" value="{escape(form['keywords'])}">
        </div>
      </div>

      <div id="random-song-custom-section">
        <h3 class="section-title">random-song.com Custom Options</h3>
        <p class="section-copy">Leave values as <code>random</code> when you want random-song.com to choose for you.</p>
        <div class="grid">
          <label for="random_genre">Custom genre</label>
          <select id="random_genre" name="random_genre">
            {random_genre_options}
          </select>

          <label for="random_market">Custom market</label>
          <select id="random_market" name="random_market">
            {random_market_options}
          </select>

          <label for="random_decade">Custom decade</label>
          <select id="random_decade" name="random_decade">
            {random_decade_options}
          </select>
        </div>

        <div class="row" style="margin-top: 12px;">
          <label><input type="checkbox" name="random_new"{checked('random_new')}> New releases only</label>
          <label><input type="checkbox" name="random_exclude_singles"{checked('random_exclude_singles')}> Exclude singles</label>
        </div>
      </div>
      <div id="link-platform-section">
        <h3 class="section-title">Links</h3>
        <p class="section-copy">YouTube links work without Spotify credentials. Spotify links require Spotify API access; the app will ask before using it.</p>
        <div class="grid">
          <label for="link_platform">Links to include</label>
          <select id="link_platform" name="link_platform">
            <option value="1"{option(form['link_platform'], '1')}>No links</option>
            <option value="2"{option(form['link_platform'], '2')}>YouTube only</option>
            <option value="3"{option(form['link_platform'], '3')}>Spotify only</option>
            <option value="4"{option(form['link_platform'], '4')}>Spotify and YouTube</option>
          </select>
        </div>
      </div>
      <div id="output-format-section">
        <h3 class="section-title">Output Format</h3>
        <p class="section-copy">Choose how to output the selected song(s): display in the live terminal or generate a text or HTML file.</p>
        <div class="grid">
          <label for="output_format">Output format</label>
          <select id="output_format" name="output_format">
            <option{option(form['output_format'], 'terminal')}>terminal</option>
            <option{option(form['output_format'], 'html')}>html</option>
            <option{option(form['output_format'], 'txt')}>txt</option>
          </select>
        </div>
      </div>

      <div style="margin-top: 14px;">
        <label><input type="checkbox" name="verbose"{checked('verbose')}> Verbose</label>
      </div>

      <div class="row" style="margin-top: 16px;">
        <button type="submit" id="run-button">Make Playlist</button>
        <button type="button" class="ghost hidden" id="cancel-button">Cancel Run</button>
      </div>
    </form>

    <div class="card">
      <h2>Run Status</h2>
      <div class="status" id="status-text">{escape(status)}</div>
    </div>

    <div class="card hidden" id="prompt-card">
      <h2>Prompt</h2>
      <div class="status" id="prompt-text"></div>
      <div class="row" id="prompt-choice-row"></div>
      <form id="prompt-form" class="hidden" style="margin-top: 14px;">
        <div class="row">
          <input id="prompt-input" autocomplete="off" placeholder="Enter your answer">
          <button type="submit">Send Answer</button>
        </div>
      </form>
    </div>

    <div class="card">
      <h2>Run Progress</h2>
      <div class="status-feed" id="status-feed"></div>
      <div class="summary-card hidden" id="summary-card"></div>
      <pre id="terminal-output" class="hidden"></pre>
    </div>

    <div class="card hidden" id="artifact-card">
      <h2>Generated File</h2>
      <div class="row" id="artifact-actions"></div>
      <p class="hint hidden" id="artifact-note"></p>
      <iframe id="artifact-frame" class="hidden"></iframe>
    </div>

    <div class="card" id="existing-generated-card">
      <h2>Existing Generated Files</h2>
      <p class="section-copy">If this clone already has locally generated output files, you can reopen them here.</p>
      <ul class="generated-list" id="existing-generated-list"></ul>
    </div>
  </div>
  <script>
    let currentSessionId = null;
    let pollTimer = null;
    let activePromptSignature = null;
    let promptDraft = "";
    const settingsClientId = document.getElementById("client_id");
    const settingsClientSecret = document.getElementById("client_secret");
    const settingsRedirectUri = document.getElementById("redirect_uri");
    const spotifySettingsToggle = document.getElementById("spotify-settings-toggle");
    const spotifySettingsFields = document.getElementById("spotify-settings-fields");
    const runClientId = document.getElementById("run-client-id");
    const runClientSecret = document.getElementById("run-client-secret");
    const runRedirectUri = document.getElementById("run-redirect-uri");
    const sourceSelect = document.getElementById("source");
    const numSongsInput = document.getElementById("num_songs");
    const playlistScopeSelect = document.getElementById("playlist_scope");
    const visibilitySelect = document.getElementById("visibility");
    const filterModeSelect = document.getElementById("filter_mode");
    const surpriseModeSelect = document.getElementById("surprise_mode");
    const playlistScopeSection = document.getElementById("playlist-scope-section");
    const visibilitySection = document.getElementById("visibility-section");
    const playlistFilterSection = document.getElementById("playlist-filter-section");
    const publicDiscoveryOptionsSection = document.getElementById("public-discovery-options-section");
    const advancedDiscoveryToggle = document.getElementById("advanced-discovery-toggle");
    const advancedDiscoveryFields = document.getElementById("advanced-discovery-fields");
    const publicKeywordsSection = document.getElementById("public-keywords-section");
    const surpriseSection = document.getElementById("surprise-section");
    const randomSongCustomSection = document.getElementById("random-song-custom-section");
    const outputFormatSection = document.getElementById("output-format-section");
    const filterValueInput = document.getElementById("filter_value");
    const filterValueLabel = document.getElementById("filter-value-label");
    const runForm = document.getElementById("run-form");
    const runButton = document.getElementById("run-button");
    const cancelButton = document.getElementById("cancel-button");
    const statusText = document.getElementById("status-text");
    const statusFeed = document.getElementById("status-feed");
    const summaryCard = document.getElementById("summary-card");
    const terminalOutput = document.getElementById("terminal-output");
    const promptCard = document.getElementById("prompt-card");
    const promptText = document.getElementById("prompt-text");
    const promptChoiceRow = document.getElementById("prompt-choice-row");
    const promptForm = document.getElementById("prompt-form");
    const promptInput = document.getElementById("prompt-input");
    const artifactCard = document.getElementById("artifact-card");
    const artifactActions = document.getElementById("artifact-actions");
    const artifactNote = document.getElementById("artifact-note");
    const artifactFrame = document.getElementById("artifact-frame");
    const existingGeneratedList = document.getElementById("existing-generated-list");

    function setHidden(element, shouldHide) {{
      element.classList.toggle("hidden", shouldHide);
    }}

    function wantsMultipleSongs() {{
      const value = numSongsInput.value.trim().toLowerCase();
      if (value === "one") {{
        return false;
      }}
      const parsed = parseInt(value || "1", 10);
      return !Number.isNaN(parsed) && parsed > 1;
    }}

    function updateFlow() {{
      const source = sourceSelect.value;
      const playlistScope = playlistScopeSelect.value;
      const filterMode = filterModeSelect.value;
      const surpriseMode = surpriseModeSelect.value;

      const isMyPlaylists = source === "My Spotify Playlists";
      setHidden(playlistScopeSection, !isMyPlaylists);
      playlistScopeSelect.disabled = !isMyPlaylists;

      setHidden(visibilitySection, !isMyPlaylists);
      visibilitySelect.disabled = !isMyPlaylists;

      setHidden(playlistFilterSection, !(isMyPlaylists && playlistScope === "Filter by name"));
      setHidden(publicDiscoveryOptionsSection, !(source === "Public discovery" || (source === "Surprise me" && surpriseMode === "Random emotions/genres via public discovery")));
      setHidden(publicKeywordsSection, source !== "Public discovery");
      setHidden(surpriseSection, source !== "Surprise me");
      setHidden(randomSongCustomSection, !(source === "Surprise me" && surpriseMode === "random-song.com custom"));
      setHidden(outputFormatSection, false);

      const needsFilterValue = filterMode !== "Rediscover preset";
      filterValueInput.disabled = !needsFilterValue;
      filterValueLabel.textContent = filterMode === "Custom regex" ? "Regex" : "Filter text";
    }}

    function syncSettingsIntoRunForm() {{
      runClientId.value = settingsClientId.value;
      runClientSecret.value = settingsClientSecret.value;
      runRedirectUri.value = settingsRedirectUri.value;
    }}

    function setRunningState(isRunning) {{
      runButton.disabled = isRunning;
      cancelButton.classList.toggle("hidden", !isRunning);
    }}

    function renderStatusFeed(data) {{
      const feed = data.status_feed || {{}};
      const hasSummary = Boolean(feed.summary) && data.finished;
      setHidden(statusFeed, hasSummary);
      setHidden(summaryCard, !hasSummary);

      if (hasSummary) {{
        summaryCard.innerHTML = feed.summary;
        summaryCard.classList.toggle("error", feed.kind === "error");
        return;
      }}

      const lines = feed.lines && feed.lines.length ? feed.lines : ["Starting..."];
      const phase = feed.phase || "Working";
      statusFeed.innerHTML = "";
      const phaseElement = document.createElement("div");
      phaseElement.className = "status-phase";
      phaseElement.textContent = phase;
      statusFeed.appendChild(phaseElement);
      lines.forEach((line) => {{
        const row = document.createElement("div");
        row.className = "status-line";
        const pulse = document.createElement("span");
        pulse.className = "pulse";
        const text = document.createElement("span");
        text.textContent = line;
        row.appendChild(pulse);
        row.appendChild(text);
        statusFeed.appendChild(row);
      }});
    }}

    function renderArtifact(data) {{
      const hasArtifact = Boolean(data.artifact_url);
      setHidden(artifactCard, !hasArtifact);
      artifactActions.innerHTML = "";
      artifactNote.textContent = "";
      artifactNote.classList.add("hidden");
      artifactFrame.classList.add("hidden");
      artifactFrame.removeAttribute("src");

      if (!hasArtifact) {{
        return;
      }}

      const openButton = document.createElement("a");
      openButton.href = data.artifact_url;
      openButton.target = "_blank";
      openButton.rel = "noreferrer";
      openButton.textContent = `Open ${{data.artifact_kind || 'file'}}`;
      openButton.className = "artifact-open";
      artifactActions.appendChild(openButton);

      if (data.artifact_kind === "html") {{
        artifactFrame.src = data.artifact_url;
        artifactFrame.classList.remove("hidden");
      }} else if (data.artifact_kind === "txt") {{
        const fileName = data.artifact_name || "text file";
        artifactNote.textContent = `TXT file "${{fileName}}" was generated. Use the Open button above to open it.`;
        artifactNote.classList.remove("hidden");
      }}
    }}

    async function refreshExistingGeneratedFiles() {{
      const response = await fetch("/api/generated-files");
      const data = await response.json();
      const files = data.files || [];

      if (!files.length) {{
        existingGeneratedList.innerHTML = "<li>No generated files found yet.</li>";
        return;
      }}

      existingGeneratedList.innerHTML = files.map((file) => (
        `<li><a href="${{file.url}}" target="_blank" rel="noreferrer">${{file.name}}</a> ` +
        `<span>(${{file.kind}}, updated ${{file.mtime}})</span></li>`
      )).join("");
    }}

    function renderPrompt(data) {{
      const prompt = data.prompt;
      if (!prompt) {{
        activePromptSignature = null;
        promptDraft = "";
        setHidden(promptCard, true);
        promptChoiceRow.innerHTML = "";
        promptForm.classList.add("hidden");
        promptInput.value = "";
        promptText.textContent = "";
        return;
      }}

      setHidden(promptCard, false);
      const promptSignature = `${{prompt.id}}::${{prompt.prompt_text}}`;
      const promptChanged = activePromptSignature !== promptSignature;
      activePromptSignature = promptSignature;
      promptText.textContent = prompt.prompt_text;

      if (prompt.type === "choice") {{
        if (promptChanged) {{
          promptDraft = "";
          promptChoiceRow.innerHTML = "";
          prompt.choices.forEach((choice) => {{
            const button = document.createElement("button");
            button.type = "button";
            button.textContent = choice;
            button.addEventListener("click", () => sendPromptAnswer(choice));
            promptChoiceRow.appendChild(button);
          }});
        }}
        promptForm.classList.add("hidden");
      }} else {{
        if (promptChanged) {{
          promptDraft = "";
        }}
        promptChoiceRow.innerHTML = "";
        promptInput.placeholder = prompt.placeholder || "Enter your answer";
        promptForm.classList.remove("hidden");
        if (promptInput.value !== promptDraft) {{
          promptInput.value = promptDraft;
        }}
        if (promptChanged) {{
          promptInput.focus();
        }}
      }}
    }}

    function renderSession(data) {{
      statusText.textContent = data.status || "Running...";
      renderStatusFeed(data);
      const verboseOutput = data.verbose_output || "";
      terminalOutput.textContent = verboseOutput || data.output || "";
      const shouldShowTerminal = Boolean(verboseOutput) || Boolean(data.finished && data.output && !data.artifact_url);
      terminalOutput.classList.toggle("hidden", !shouldShowTerminal);
      terminalOutput.scrollTop = terminalOutput.scrollHeight;
      renderPrompt(data);
      renderArtifact(data);

      if (data.finished) {{
        setRunningState(false);
        refreshExistingGeneratedFiles();
        if (pollTimer) {{
          clearTimeout(pollTimer);
          pollTimer = null;
        }}
      }} else {{
        setRunningState(true);
        pollTimer = setTimeout(pollSession, 700);
      }}
    }}

    async function pollSession() {{
      if (!currentSessionId) {{
        return;
      }}
      const response = await fetch(`/api/session?session_id=${{encodeURIComponent(currentSessionId)}}`);
      const data = await response.json();
      renderSession(data);
    }}

    async function startRun(formData) {{
      const response = await fetch("/api/start", {{
        method: "POST",
        body: new URLSearchParams(formData),
      }});
      const data = await response.json();
      if (!response.ok) {{
        statusText.textContent = data.error || "Could not start run.";
        setRunningState(false);
        return;
      }}
      currentSessionId = data.session_id;
      terminalOutput.textContent = "";
      terminalOutput.classList.add("hidden");
      statusFeed.classList.remove("hidden");
      summaryCard.classList.add("hidden");
      statusFeed.innerHTML = '<div class="status-phase">Starting</div><div class="status-line"><span class="pulse"></span><span>Starting...</span></div>';
      renderArtifact({{}});
      renderPrompt({{ prompt: null }});
      statusText.textContent = "Run started...";
      pollSession();
    }}

    async function sendPromptAnswer(answer) {{
      if (!currentSessionId) {{
        return;
      }}
      promptDraft = "";
      await fetch("/api/respond", {{
        method: "POST",
        body: new URLSearchParams({{ session_id: currentSessionId, answer }}),
      }});
      pollSession();
    }}

    async function cancelRun() {{
      if (!currentSessionId) {{
        return;
      }}
      await fetch("/api/cancel", {{
        method: "POST",
        body: new URLSearchParams({{ session_id: currentSessionId }}),
      }});
      pollSession();
    }}

    settingsClientId.addEventListener("input", syncSettingsIntoRunForm);
    settingsClientSecret.addEventListener("input", syncSettingsIntoRunForm);
    settingsRedirectUri.addEventListener("input", syncSettingsIntoRunForm);
    spotifySettingsToggle.addEventListener("click", () => {{
      const shouldShow = spotifySettingsFields.classList.contains("hidden");
      spotifySettingsFields.classList.toggle("hidden", !shouldShow);
      spotifySettingsToggle.textContent = shouldShow ? "Hide Spotify settings" : "Tweak Spotify settings";
    }});
    sourceSelect.addEventListener("change", updateFlow);
    numSongsInput.addEventListener("input", updateFlow);
    playlistScopeSelect.addEventListener("change", updateFlow);
    filterModeSelect.addEventListener("change", updateFlow);
    surpriseModeSelect.addEventListener("change", updateFlow);
    advancedDiscoveryToggle.addEventListener("click", () => {{
      const shouldShow = advancedDiscoveryFields.classList.contains("hidden");
      advancedDiscoveryFields.classList.toggle("hidden", !shouldShow);
      advancedDiscoveryToggle.textContent = shouldShow ? "Hide discovery settings" : "Tweak discovery settings";
    }});
    cancelButton.addEventListener("click", cancelRun);
    promptForm.addEventListener("submit", (event) => {{
      event.preventDefault();
      sendPromptAnswer(promptInput.value);
    }});
    promptInput.addEventListener("input", () => {{
      promptDraft = promptInput.value;
    }});
    runForm.addEventListener("submit", (event) => {{
      event.preventDefault();
      syncSettingsIntoRunForm();
      setRunningState(true);
      if (pollTimer) {{
        clearTimeout(pollTimer);
        pollTimer = null;
      }}
      startRun(new FormData(runForm));
    }});

    syncSettingsIntoRunForm();
    updateFlow();
    refreshExistingGeneratedFiles();
  </script>
</body>
</html>"""


class WebHandler(BaseHTTPRequestHandler):
    def _send_html(self, html_body, status_code=200):
        encoded = html_body.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload, status_code=200):
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_form(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        return merge_with_defaults(parse_form_data(self.rfile.read(content_length)))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/":
            self._send_html(render_page(build_default_form_data()))
            return

        if parsed.path == "/api/session":
            session_id = query.get("session_id", [""])[0]
            session = get_session(session_id)
            if not session:
                self._send_json({"error": "Session not found."}, status_code=404)
                return
            self._send_json(session.serialize())
            return

        if parsed.path == "/api/generated-files":
            self._send_json({"files": list_generated_files()})
            return

        if parsed.path == "/generated-file":
            relative_path = query.get("path", [""])[0]
            if not relative_path:
                self.send_error(404, "Generated file not found.")
                return

            target_path = (REPO_DIR / relative_path).resolve()
            try:
                target_path.relative_to(REPO_DIR.resolve())
            except ValueError:
                self.send_error(403, "Invalid generated file path.")
                return

            if not target_path.exists() or not target_path.is_file():
                self.send_error(404, "Generated file not found.")
                return

            mime_type = mimetypes.guess_type(str(target_path))[0] or "application/octet-stream"
            payload = target_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if parsed.path == "/artifact":
            session_id = query.get("session_id", [""])[0]
            session = get_session(session_id)
            if not session or not session.generated_file or not session.generated_file.exists():
                self.send_error(404, "Artifact not found.")
                return

            mime_type = mimetypes.guess_type(str(session.generated_file))[0] or "application/octet-stream"
            payload = session.generated_file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_error(404)

    def do_POST(self):
        if self.path == "/save":
            form = self._read_form()
            client_id = form["client_id"].strip()
            client_secret = form["client_secret"].strip()
            if bool(client_id) != bool(client_secret):
                self._send_html(
                    render_page(
                        form,
                        status="Save both Client ID and Client Secret together, or leave both blank.",
                    )
                )
                return

            save_credentials_from_form(form)
            self._send_html(render_page(form, status=f"Saved settings to {CONFIG_PATH}."))
            return

        if self.path == "/api/start":
            form = self._read_form()
            validation_error = validate_form(form)
            if validation_error:
                self._send_json({"error": validation_error}, status_code=400)
                return

            save_credentials_from_form(form)
            session = create_session(form)
            self._send_json({"session_id": session.id})
            return

        if self.path == "/api/respond":
            form = self._read_form()
            session = get_session(form.get("session_id", ""))
            if not session:
                self._send_json({"error": "Session not found."}, status_code=404)
                return
            if not session.respond(form.get("answer", "")):
                self._send_json({"error": "No prompt is waiting for input."}, status_code=409)
                return
            self._send_json({"ok": True})
            return

        if self.path == "/api/cancel":
            form = self._read_form()
            session = get_session(form.get("session_id", ""))
            if not session:
                self._send_json({"error": "Session not found."}, status_code=404)
                return
            session.cancel()
            self._send_json({"ok": True})
            return

        self.send_error(404)

    def log_message(self, format, *args):
        return


def start_server():
    port = DEFAULT_PORT
    while True:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), WebHandler)
            return server, port
        except OSError:
            port += 1


if __name__ == "__main__":
    try:
        RUNTIME_SETUP_MESSAGE = ensure_cli_dependencies()
    except RuntimeError as error:
        RUNTIME_SETUP_MESSAGE = str(error)
    server, port = start_server()
    url = f"http://127.0.0.1:{port}"
    print(f"Playlist Maker running at {url}")
    print("Press Ctrl+C to stop.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
