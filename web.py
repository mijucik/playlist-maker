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

AUTO_RESPONSE_PROMPTS = {
    "Do you want to open the file now? (yes/no): ": "no",
    "Do you want to open Spotify link(s) now? (yes/no): ": "no",
    "Do you want to open YouTube link(s) now? (yes/no): ": "no",
    "Do you want to open Spotify/YouTube link(s) now? (yes/no): ": "no",
    "Which platform should be opened first? (Enter 'spotify' or 'youtube'): ": "spotify",
    "Open all song links? (yes/no): ": "no",
}

INTERACTIVE_PROMPTS = {
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
}
GENERATED_DIR = REPO_DIR / "generated"


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


def ensure_cli_dependencies():
    missing_requirements = [
        requirement
        for module_name, requirement in REQUIRED_MODULES.items()
        if importlib.util.find_spec(module_name) is None
    ]

    if not missing_requirements:
        return "CLI dependencies already available."

    print("Missing Python packages detected for the picker. Installing them now...")
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
        "source": "All playlists",
        "visibility": "Any visibility",
        "filter_mode": "Rediscover preset",
        "filter_value": "",
        "max_playlists": "10",
        "min_playlist_size": "",
        "max_playlist_size": "",
        "discovery_mode": "Hybrid",
        "keywords": "",
        "surprise_mode": "Random emotions/genres via public discovery",
        "random_genre": "random",
        "random_market": "random",
        "random_decade": "random",
        "random_new": "",
        "random_exclude_singles": "",
        "link_choice": "No links",
        "output_format": "terminal",
    }


def merge_with_defaults(form_data):
    defaults = build_default_form_data()
    defaults.update({key: value for key, value in form_data.items() if value is not None})
    return defaults


def build_initial_cli_answers(form):
    lines = [form["num_songs"].strip() or "1"]

    source_choice = {
        "All playlists": "1",
        "Filter your playlists by name": "2",
        "Your own playlists": "3",
        "Random saved playlists": "4",
        "Public discovery": "5",
        "Surprise me": "6",
    }[form["source"]]
    lines.append(source_choice)

    if source_choice in {"1", "2", "3", "4"}:
        lines.append({
            "Any visibility": "1",
            "Public only": "2",
            "Private only": "3",
        }[form["visibility"]])

    if source_choice == "2":
        filter_choice = {
            "Rediscover preset": "1",
            "Contains text": "2",
            "Custom regex": "3",
        }[form["filter_mode"]]
        lines.append(filter_choice)
        if filter_choice in {"2", "3"}:
            lines.append(form["filter_value"].strip())

    if source_choice == "5":
        lines.append(form["max_playlists"].strip() or "10")
        lines.append(form["min_playlist_size"].strip())
        lines.append(form["max_playlist_size"].strip())
        lines.append({
            "Hybrid": "1",
            "Spotify public playlists": "2",
            "YouTube playlists": "3",
            "Track search only": "4",
            "No Spotify API": "5",
        }[form["discovery_mode"]])
        lines.append(form["keywords"].strip())

    if source_choice == "6":
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
            lines.append({
                "Hybrid": "1",
                "Spotify public playlists": "2",
                "YouTube playlists": "3",
                "Track search only": "4",
                "No Spotify API": "5",
            }[form["discovery_mode"]])
        elif surprise_choice == "3":
            lines.append(form["random_genre"].strip() or "random")
            lines.append(form["random_market"].strip() or "random")
            lines.append(form["random_decade"].strip() or "random")
            lines.append("yes" if form.get("random_new") else "no")
            lines.append("yes" if form.get("random_exclude_singles") else "no")

    lines.append({
        "No links": "1",
        "Spotify links": "2",
        "YouTube links": "3",
        "Both Spotify and YouTube": "4",
    }[form["link_choice"]])

    num_songs_raw = form["num_songs"].strip().lower()
    if num_songs_raw != "one":
        try:
            if int(num_songs_raw or "1") > 1:
                lines.append(form["output_format"])
        except ValueError:
            pass

    return "\n".join(lines) + "\n"


def form_requires_spotify_credentials(form):
    source = form["source"]
    discovery_mode = form["discovery_mode"]
    surprise_mode = form["surprise_mode"]
    link_choice = form["link_choice"]

    if source in {
        "All playlists",
        "Filter your playlists by name",
        "Your own playlists",
        "Random saved playlists",
    }:
        return True

    if source == "Public discovery" and discovery_mode != "No Spotify API":
        return True

    if source == "Surprise me":
        if surprise_mode == "Random emotions/genres via public discovery" and discovery_mode != "No Spotify API":
            return True
        if surprise_mode in {"random-song.com default", "random-song.com custom"}:
            return False

    if link_choice in {"Spotify links", "Both Spotify and YouTube"}:
        return True

    return False


def validate_form(form):
    if form_requires_spotify_credentials(form) and (not form["client_id"].strip() or not form["client_secret"].strip()):
        return "Client ID and Client Secret are required."

    num_songs_value = form["num_songs"].strip().lower()
    try:
        if num_songs_value != "one" and int(num_songs_value or "1") < 1:
            return "Number of songs must be at least 1."
    except ValueError:
        return "Number of songs must be 'one' or a positive integer."

    if form["source"] == "Filter your playlists by name" and form["filter_mode"] in {"Contains text", "Custom regex"}:
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
    try:
        min_size = int(min_size_raw) if min_size_raw else None
        max_size = int(max_size_raw) if max_size_raw else None
    except ValueError:
        return "Playlist size limits must be positive integers when provided."

    if min_size is not None and min_size < 1:
        return "Minimum playlist size must be positive."
    if max_size is not None and max_size < 1:
        return "Maximum playlist size must be positive."
    if min_size is not None and max_size is not None and min_size > max_size:
        return "Minimum playlist size cannot be greater than maximum playlist size."

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
        self.output = ""
        self.tail = ""
        self.finished = False
        self.exit_code = None
        self.current_prompt = None
        self.status = "Starting..."
        self.generated_file = None
        self.error = None
        self.was_cancelled = False

    def start(self):
        with self.lock:
            self.status = "Running..."
        env = os.environ.copy()
        env["SPOTIPY_CLIENT_ID"] = self.form["client_id"].strip()
        env["SPOTIPY_CLIENT_SECRET"] = self.form["client_secret"].strip()
        env["SPOTIPY_REDIRECT_URI"] = self.form["redirect_uri"].strip() or DEFAULT_REDIRECT_URI

        self.process = subprocess.Popen(
            [sys.executable, "main.py"],
            cwd=REPO_DIR,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
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
                self.finished = True
                self.exit_code = self.process.returncode
                if self.was_cancelled:
                    self.status = "Run cancelled."
                elif self.exit_code == 0:
                    self.status = "Run finished."
                else:
                    self.status = f"Run exited with code {self.exit_code}."
                self.current_prompt = None
        except Exception as error:
            with self.lock:
                self.finished = True
                self.error = str(error)
                self.status = f"Run failed: {error}"

    def _handle_output_chunk(self, chunk):
        pending_prompt = None
        auto_response = None

        with self.lock:
            self.output += chunk
            self.tail = (self.tail + chunk)[-300:]
            if chunk == "\n":
                self._parse_recent_output_locked()

            for prompt_text, response in AUTO_RESPONSE_PROMPTS.items():
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
                self.output += answer + "\n"
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
        for line in self.output.splitlines()[-20:]:
            if line.startswith("Output file will be: "):
                possible_path = Path(line.split("Output file will be: ", 1)[1].strip())
                if possible_path.exists():
                    self.generated_file = possible_path
            elif "written to '" in line:
                match = re.search(r"written to '([^']+)'", line)
                if match:
                    possible_path = Path(match.group(1))
                    if possible_path.exists():
                        self.generated_file = possible_path

    def artifact_url(self):
        if not self.generated_file or not self.generated_file.exists():
            return None
        return f"/artifact?session_id={urllib.parse.quote(self.id)}"

    def serialize(self):
        with self.lock:
            artifact_path = str(self.generated_file) if self.generated_file else None
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
                "output": self.output,
                "prompt": self.current_prompt,
                "artifact_path": artifact_path,
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

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Spotify Playlist Picker Web</title>
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
      border-radius: 16px;
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
    .row {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
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
    <h1>Spotify Playlist Picker Web</h1>
    <p class="hint">Run the picker in a live web terminal, answer follow-up prompts in place, and preview generated HTML output directly below.</p>
    <p class="hint">{escape(RUNTIME_SETUP_MESSAGE)}</p>

    <form method="post" action="/save" class="card" id="settings-form">
      <h2>Spotify App Settings</h2>
      <p class="section-copy">These are required for personal-playlist features, Spotify link lookup, and playlist creation. They are optional for the public `No Spotify API` discovery mode.</p>
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
          <option{option(form['source'], 'All playlists')}>All playlists</option>
          <option{option(form['source'], 'Filter your playlists by name')}>Filter your playlists by name</option>
          <option{option(form['source'], 'Your own playlists')}>Your own playlists</option>
          <option{option(form['source'], 'Random saved playlists')}>Random saved playlists</option>
          <option{option(form['source'], 'Public discovery')}>Public discovery</option>
          <option{option(form['source'], 'Surprise me')}>Surprise me</option>
        </select>
      </div>

      <div id="visibility-section">
        <h3 class="section-title">Playlist Visibility</h3>
        <p class="section-copy">Use this when you want to limit personal playlist sources to public playlists, private playlists, or either.</p>
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
        <p class="section-copy">These settings control how many public playlists are considered and what size range they must fall into. Use `No Spotify API` when you want a public run that can still finish without Spotify auth.</p>
        <div class="grid">
          <label for="max_playlists">Max playlists</label>
          <input id="max_playlists" name="max_playlists" value="{escape(form['max_playlists'])}">

          <label for="min_playlist_size">Min playlist size</label>
          <input id="min_playlist_size" name="min_playlist_size" value="{escape(form['min_playlist_size'])}">

          <label for="max_playlist_size">Max playlist size</label>
          <input id="max_playlist_size" name="max_playlist_size" value="{escape(form['max_playlist_size'])}">

          <label for="discovery_mode">Discovery mode</label>
          <select id="discovery_mode" name="discovery_mode">
            <option{option(form['discovery_mode'], 'Hybrid')}>Hybrid</option>
            <option{option(form['discovery_mode'], 'Spotify public playlists')}>Spotify public playlists</option>
            <option{option(form['discovery_mode'], 'YouTube playlists')}>YouTube playlists</option>
            <option{option(form['discovery_mode'], 'Track search only')}>Track search only</option>
            <option{option(form['discovery_mode'], 'No Spotify API')}>No Spotify API</option>
          </select>
        </div>
      </div>

      <div id="public-keywords-section">
        <h3 class="section-title">Public Discovery Keywords</h3>
        <p class="section-copy">Enter one or more words or phrases, like <code>happy</code>, <code>yacht rock</code>, or <code>"summer jazz"</code>.</p>
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
          <input id="random_genre" name="random_genre" value="{escape(form['random_genre'])}">

          <label for="random_market">Custom market</label>
          <input id="random_market" name="random_market" value="{escape(form['random_market'])}">

          <label for="random_decade">Custom decade</label>
          <input id="random_decade" name="random_decade" value="{escape(form['random_decade'])}">
        </div>

        <div class="row" style="margin-top: 12px;">
          <label><input type="checkbox" name="random_new"{checked('random_new')}> New releases only</label>
          <label><input type="checkbox" name="random_exclude_singles"{checked('random_exclude_singles')}> Exclude singles</label>
        </div>
      </div>

      <div id="link-section">
        <h3 class="section-title">Link Lookup</h3>
        <p class="section-copy">Choose whether the resulting songs should include Spotify links, YouTube links, both, or no links.</p>
        <div class="grid">
          <label for="link_choice">Link lookup</label>
          <select id="link_choice" name="link_choice">
            <option{option(form['link_choice'], 'No links')}>No links</option>
            <option{option(form['link_choice'], 'Spotify links')}>Spotify links</option>
            <option{option(form['link_choice'], 'YouTube links')}>YouTube links</option>
            <option{option(form['link_choice'], 'Both Spotify and YouTube')}>Both Spotify and YouTube</option>
          </select>
        </div>
      </div>

      <div id="output-format-section">
        <h3 class="section-title">Multi-song Output</h3>
        <p class="section-copy">If you request more than one song, you can keep it in the live terminal or generate a text or HTML file.</p>
        <div class="grid">
          <label for="output_format">Output format</label>
          <select id="output_format" name="output_format">
            <option{option(form['output_format'], 'terminal')}>terminal</option>
            <option{option(form['output_format'], 'html')}>html</option>
            <option{option(form['output_format'], 'txt')}>txt</option>
          </select>
        </div>
      </div>

      <div class="row" style="margin-top: 16px;">
        <button type="submit" id="run-button">Run Picker</button>
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
      <h2>Live Terminal</h2>
      <pre id="terminal-output"></pre>
    </div>

    <div class="card hidden" id="artifact-card">
      <h2>Generated File</h2>
      <div class="row" id="artifact-actions"></div>
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
    const runClientId = document.getElementById("run-client-id");
    const runClientSecret = document.getElementById("run-client-secret");
    const runRedirectUri = document.getElementById("run-redirect-uri");
    const sourceSelect = document.getElementById("source");
    const numSongsInput = document.getElementById("num_songs");
    const visibilitySelect = document.getElementById("visibility");
    const filterModeSelect = document.getElementById("filter_mode");
    const surpriseModeSelect = document.getElementById("surprise_mode");
    const visibilitySection = document.getElementById("visibility-section");
    const playlistFilterSection = document.getElementById("playlist-filter-section");
    const publicDiscoveryOptionsSection = document.getElementById("public-discovery-options-section");
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
    const terminalOutput = document.getElementById("terminal-output");
    const promptCard = document.getElementById("prompt-card");
    const promptText = document.getElementById("prompt-text");
    const promptChoiceRow = document.getElementById("prompt-choice-row");
    const promptForm = document.getElementById("prompt-form");
    const promptInput = document.getElementById("prompt-input");
    const artifactCard = document.getElementById("artifact-card");
    const artifactActions = document.getElementById("artifact-actions");
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
      const filterMode = filterModeSelect.value;
      const surpriseMode = surpriseModeSelect.value;

      const usesVisibility = ["All playlists", "Filter your playlists by name", "Your own playlists", "Random saved playlists"].includes(source);
      setHidden(visibilitySection, !usesVisibility);
      visibilitySelect.disabled = !usesVisibility;

      setHidden(playlistFilterSection, source !== "Filter your playlists by name");
      setHidden(publicDiscoveryOptionsSection, !(source === "Public discovery" || (source === "Surprise me" && surpriseMode === "Random emotions/genres via public discovery")));
      setHidden(publicKeywordsSection, source !== "Public discovery");
      setHidden(surpriseSection, source !== "Surprise me");
      setHidden(randomSongCustomSection, !(source === "Surprise me" && surpriseMode === "random-song.com custom"));
      setHidden(outputFormatSection, !wantsMultipleSongs());

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

    function renderArtifact(data) {{
      const hasArtifact = Boolean(data.artifact_url);
      setHidden(artifactCard, !hasArtifact);
      artifactActions.innerHTML = "";
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
      openButton.className = "secondary";
      openButton.style.display = "inline-block";
      openButton.style.padding = "10px 16px";
      openButton.style.borderRadius = "999px";
      openButton.style.color = "white";
      openButton.style.textDecoration = "none";
      artifactActions.appendChild(openButton);

      if (data.artifact_kind === "html") {{
        artifactFrame.src = data.artifact_url;
        artifactFrame.classList.remove("hidden");
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
      terminalOutput.textContent = data.output || "";
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
    sourceSelect.addEventListener("change", updateFlow);
    numSongsInput.addEventListener("input", updateFlow);
    filterModeSelect.addEventListener("change", updateFlow);
    surpriseModeSelect.addEventListener("change", updateFlow);
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
    print(f"Spotify Playlist Picker Web running at {url}")
    print("Press Ctrl+C to stop.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
