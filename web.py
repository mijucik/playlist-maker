#!/usr/bin/env python3

import html
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


APP_NAME = "spotify-scripts"
APP_DIR = Path.home() / f".{APP_NAME}"
CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8080/callback"
REPO_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8765


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


def get_saved_spotify_config():
    return load_local_config().get("spotify_app", {})


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
    }


def merge_with_defaults(form_data):
    defaults = build_default_form_data()
    defaults.update({key: value for key, value in form_data.items() if value is not None})
    return defaults


def build_cli_answers(form):
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

    if source_choice == "4":
        lines.append("no")

    if source_choice == "5":
        lines.append(form["max_playlists"].strip() or "10")
        lines.append(form["min_playlist_size"].strip())
        lines.append(form["max_playlist_size"].strip())
        lines.append({
            "Hybrid": "1",
            "Spotify public playlists": "2",
            "YouTube playlists": "3",
            "Track search only": "4",
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
                lines.append("terminal")
        except ValueError:
            pass

    lines.append("no")

    if form["link_choice"] != "No links":
        lines.append("no")

    return "\n".join(lines) + "\n"


def validate_form(form):
    if not form["client_id"].strip() or not form["client_secret"].strip():
        return "Client ID and Client Secret are required."

    try:
        num_songs_value = form["num_songs"].strip().lower()
        if num_songs_value != "one":
            if int(num_songs_value or "1") < 1:
                return "Number of songs must be at least 1."
    except ValueError:
        return "Number of songs must be 'one' or a positive integer."

    if form["source"] == "Filter your playlists by name" and form["filter_mode"] in {"Contains text", "Custom regex"}:
        if not form["filter_value"].strip():
            return "Enter playlist filter text or regex."

    if form["source"] == "Public discovery" and not form["keywords"].strip():
        return "Enter at least one keyword for public discovery."

    if form["source"] == "Surprise me" and form["surprise_mode"] == "Random emotions/genres via public discovery":
        if not form["max_playlists"].strip():
            return "Enter a maximum number of playlists for public surprise mode."

    if form["source"] == "Public discovery":
        if not form["max_playlists"].strip():
            return "Enter a maximum number of playlists."

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

    return None


def save_credentials_from_form(form):
    config_data = load_local_config()
    config_data["spotify_app"] = {
        "client_id": form["client_id"].strip(),
        "client_secret": form["client_secret"].strip(),
        "redirect_uri": form["redirect_uri"].strip() or DEFAULT_REDIRECT_URI,
    }
    save_local_config(config_data)


def run_cli_from_form(form):
    answer_blob = build_cli_answers(form)
    env = os.environ.copy()
    env["SPOTIPY_CLIENT_ID"] = form["client_id"].strip()
    env["SPOTIPY_CLIENT_SECRET"] = form["client_secret"].strip()
    env["SPOTIPY_REDIRECT_URI"] = form["redirect_uri"].strip() or DEFAULT_REDIRECT_URI

    result = subprocess.run(
        [sys.executable, "main.py"],
        cwd=REPO_DIR,
        env=env,
        input=answer_blob,
        text=True,
        capture_output=True,
        timeout=600,
    )
    return (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")


def render_page(form, status="", output=""):
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
      max-width: 980px;
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
    }}
    .hint {{
      color: #536257;
      font-size: 0.95rem;
      margin-top: 0;
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
    <p class="hint">Enter your Spotify app settings once, then follow the same decision flow as the terminal app without seeing unrelated options.</p>

    <form method="post" action="/save" class="card" id="settings-form">
      <h2>Spotify App Settings</h2>
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

    <form method="post" action="/run" class="card" id="run-form">
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
      <p class="section-copy">These settings control how many public playlists are considered and what size range they must fall into.</p>
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

      <div class="row" style="margin-top: 16px;">
        <button type="submit">Run Picker</button>
      </div>
    </form>

    <div class="card">
      <div class="status">{escape(status)}</div>
      <pre>{escape(output)}</pre>
    </div>
  </div>
  <script>
    const settingsClientId = document.getElementById("client_id");
    const settingsClientSecret = document.getElementById("client_secret");
    const settingsRedirectUri = document.getElementById("redirect_uri");
    const runClientId = document.getElementById("run-client-id");
    const runClientSecret = document.getElementById("run-client-secret");
    const runRedirectUri = document.getElementById("run-redirect-uri");
    const sourceSelect = document.getElementById("source");
    const visibilitySelect = document.getElementById("visibility");
    const filterModeSelect = document.getElementById("filter_mode");
    const surpriseModeSelect = document.getElementById("surprise_mode");
    const visibilitySection = document.getElementById("visibility-section");
    const playlistFilterSection = document.getElementById("playlist-filter-section");
    const publicDiscoveryOptionsSection = document.getElementById("public-discovery-options-section");
    const publicKeywordsSection = document.getElementById("public-keywords-section");
    const surpriseSection = document.getElementById("surprise-section");
    const randomSongCustomSection = document.getElementById("random-song-custom-section");
    const filterValueInput = document.getElementById("filter_value");
    const filterValueLabel = document.getElementById("filter-value-label");

    function setHidden(element, shouldHide) {{
      element.classList.toggle("hidden", shouldHide);
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

      const needsFilterValue = filterMode !== "Rediscover preset";
      filterValueInput.disabled = !needsFilterValue;
      filterValueLabel.textContent = filterMode === "Custom regex" ? "Regex" : "Filter text";
    }}

    function syncSettingsIntoRunForm() {{
      runClientId.value = settingsClientId.value;
      runClientSecret.value = settingsClientSecret.value;
      runRedirectUri.value = settingsRedirectUri.value;
    }}

    settingsClientId.addEventListener("input", syncSettingsIntoRunForm);
    settingsClientSecret.addEventListener("input", syncSettingsIntoRunForm);
    settingsRedirectUri.addEventListener("input", syncSettingsIntoRunForm);
    sourceSelect.addEventListener("change", updateFlow);
    filterModeSelect.addEventListener("change", updateFlow);
    surpriseModeSelect.addEventListener("change", updateFlow);
    syncSettingsIntoRunForm();
    updateFlow();
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

    def do_GET(self):
        self._send_html(render_page(build_default_form_data(), status="Ready.", output=""))

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        form = merge_with_defaults(parse_form_data(self.rfile.read(content_length)))

        if self.path == "/save":
            validation_error = validate_form({**form, "num_songs": "1", "source": "All playlists"})
            if validation_error and "Client ID" in validation_error:
                self._send_html(render_page(form, status=validation_error, output=""))
                return

            save_credentials_from_form(form)
            self._send_html(render_page(form, status=f"Saved settings to {CONFIG_PATH}.", output=""))
            return

        if self.path == "/run":
            validation_error = validate_form(form)
            if validation_error:
                self._send_html(render_page(form, status=validation_error, output=""))
                return

            save_credentials_from_form(form)
            try:
                output = run_cli_from_form(form)
                status = "Run finished."
            except Exception as error:
                output = ""
                status = f"Run failed: {error}"

            self._send_html(render_page(form, status=status, output=output))
            return

        self._send_html(render_page(form, status="Unknown action.", output=""), status_code=404)

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
