#!/usr/bin/env python3

import getpass
import html
import json
import logging
import os
import random
import re
import string
import sys
import tempfile
import threading
import time
import textwrap
import unicodedata
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

import spotipy
import spotipy.exceptions
from spotipy.cache_handler import CacheHandler
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth
from tqdm import tqdm
from ytmusicapi import YTMusic

logging.getLogger("spotipy").setLevel(logging.CRITICAL)

# Windows consoles default to cp1252 which can't encode emoji in playlist/song names.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


APP_NAME = "spotify-scripts"
APP_DIR = Path.home() / f".{APP_NAME}"
CONFIG_PATH = APP_DIR / "config.json"
TOKEN_CACHE_PATH = APP_DIR / "token_cache.json"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8080/callback"
SCOPE = "user-read-private playlist-read-private playlist-modify-private playlist-modify-public"
RANDOM_SONG_API_BASE_URL = "https://europe-west1-randommusicgenerator-34646.cloudfunctions.net/appV2"
REQUIRED_SCOPE_SET = set(SCOPE.split())
DEFAULT_PUBLIC_DISCOVERY_MODE = "youtube-music"
SURPRISE_PUBLIC_DISCOVERY_MAX_ATTEMPTS = 5
YOUTUBE_MUSIC_SEARCH_LIMIT = 20
YOUTUBE_MUSIC_MAX_CONSECUTIVE_NET_ERRORS = 2
YOUTUBE_MUSIC_VIDEO_TYPES = {
    "MUSIC_VIDEO_TYPE_ATV",
    "MUSIC_VIDEO_TYPE_OMV",
    "MUSIC_VIDEO_TYPE_UGC",
}
SPOTIFY_MATCH_CACHE = {}
SPOTIFY_MIN_INTERVAL_SECONDS = 0.2
SPOTIFY_MAX_RETRIES = 4
SPOTIFY_MAX_AUTO_RETRY_AFTER_SECONDS = 120
CURRENT_USER_PROFILE = None
SPOTIFY_CLIENT = None
SPOTIFY_SEARCH_CLIENT = None
SPOTIFY_API_UNAVAILABLE_REASON = None
SPOTIFY_SEARCH_UNAVAILABLE_REASON = None
SPOTIFY_UNAVAILABLE_NOTICES = set()
YOUTUBE_MUSIC_CLIENT = None
RUN_REPORT = {}
SPOTIFY_LINK_MATCHING_DISABLED_REASON = None
AVOID_OPTIONAL_SPOTIFY_API = False


class RunAborted(RuntimeError):
    """Raised when the current run should stop cleanly."""


class SpotifyApiUnavailableError(RuntimeError):
    """Raised when Spotify API access is unavailable for this run."""


def emit_web_status(phase, message, detail=None, level="info", reset=False, done=False, report=None):
    if os.getenv("SPOTIFY_PICKER_WEB_STATUS") != "1":
        return

    payload = {
        "phase": phase,
        "message": message,
        "level": level,
        "reset": reset,
        "done": done,
    }
    if detail is not None:
        payload["detail"] = detail
    if report is not None:
        payload["report"] = report
    print(f"WEB_STATUS_JSON:{json.dumps(payload, ensure_ascii=False)}", flush=True)


def reset_run_report():
    global RUN_REPORT, SPOTIFY_LINK_MATCHING_DISABLED_REASON, AVOID_OPTIONAL_SPOTIFY_API
    RUN_REPORT = {
        "warnings": [],
        "errors": [],
        "spotify_playlist_status": "not asked",
        "links_added": False,
        "spotify_exact_links": 0,
        "spotify_search_links": 0,
        "youtube_links": 0,
        "surprise_attempts": [],
    }
    SPOTIFY_LINK_MATCHING_DISABLED_REASON = None
    AVOID_OPTIONAL_SPOTIFY_API = False


def add_report_list_item(key, value):
    if value and value not in RUN_REPORT.setdefault(key, []):
        RUN_REPORT[key].append(value)


def update_run_report(**updates):
    for key, value in updates.items():
        if key in {"warnings", "errors", "surprise_attempts"}:
            existing = RUN_REPORT.setdefault(key, [])
            for item in value if isinstance(value, list) else [value]:
                if item and item not in existing:
                    existing.append(item)
        else:
            RUN_REPORT[key] = value
    emit_web_status("report", None, report=RUN_REPORT)


class SecureTokenCacheHandler(CacheHandler):
    """Store Spotify tokens in a private file with owner-only permissions."""

    def __init__(self, cache_path):
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.cache_path.parent, 0o700)
        except OSError:
            pass

    def get_cached_token(self):
        if not self.cache_path.exists():
            return None

        try:
            with self.cache_path.open("r", encoding="utf-8") as cache_file:
                return json.load(cache_file)
        except (OSError, json.JSONDecodeError):
            return None

    def save_token_to_cache(self, token_info):
        temp_fd, temp_name = tempfile.mkstemp(
            prefix=f"{self.cache_path.stem}_",
            suffix=".tmp",
            dir=str(self.cache_path.parent),
        )
        temp_path = Path(temp_name)
        with os.fdopen(temp_fd, "w", encoding="utf-8") as cache_file:
            json.dump(token_info, cache_file)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, self.cache_path)
        try:
            os.chmod(self.cache_path, 0o600)
        except OSError:
            pass

    def delete_cache(self):
        try:
            self.cache_path.unlink()
        except FileNotFoundError:
            pass


class RateLimitedSpotifyClient:
    """Throttle Spotify API calls and honor Retry-After on 429 responses."""

    def __init__(self, client):
        self._client = client
        self._lock = threading.Lock()
        self._next_request_time = 0.0

    def _reserve_request_slot(self):
        with self._lock:
            now = time.monotonic()
            wait_seconds = max(0.0, self._next_request_time - now)
            if wait_seconds:
                time.sleep(wait_seconds)
            self._next_request_time = time.monotonic() + SPOTIFY_MIN_INTERVAL_SECONDS

    def _extract_retry_after_seconds(self, error):
        return extract_retry_after_seconds(error)

    def _call_with_rate_limit(self, method, *args, **kwargs):
        attempt = 0
        while True:
            self._reserve_request_slot()
            try:
                return method(*args, **kwargs)
            except spotipy.exceptions.SpotifyException as error:
                attempt += 1
                retry_after_seconds = self._extract_retry_after_seconds(error)
                http_status = getattr(error, "http_status", None)

                if http_status == 429 and attempt <= SPOTIFY_MAX_RETRIES:
                    if retry_after_seconds is None:
                        retry_after_seconds = min(2 ** attempt, 10)

                    if retry_after_seconds > SPOTIFY_MAX_AUTO_RETRY_AFTER_SECONDS:
                        raise RunAborted(
                            "Spotify rate-limited this run for too long to auto-retry "
                            f"({retry_after_seconds} seconds, over the 2 minute limit). Exiting early to avoid hammering the API."
                        )

                    print(
                        "Spotify rate limit reached. Waiting "
                        f"{retry_after_seconds} second(s) before retrying..."
                    )
                    time.sleep(retry_after_seconds)
                    continue

                if http_status == 429:
                    retry_summary = (
                        f"Spotify kept returning HTTP 429 after {attempt} attempt(s). "
                        "Exiting early to avoid hammering the API."
                    )
                    raise RunAborted(retry_summary)

                if http_status in {500, 502, 503} and attempt <= SPOTIFY_MAX_RETRIES:
                    backoff_seconds = min(2 ** attempt, 10)
                    print(
                        f"Spotify returned HTTP {http_status}. Retrying in "
                        f"{backoff_seconds} second(s)..."
                    )
                    time.sleep(backoff_seconds)
                    continue

                raise

    def __getattr__(self, name):
        attribute = getattr(self._client, name)
        if not callable(attribute):
            return attribute

        def wrapped(*args, **kwargs):
            return self._call_with_rate_limit(attribute, *args, **kwargs)

        return wrapped


def extract_retry_after_seconds(error):
    headers = getattr(error, "headers", {}) or {}
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after:
        try:
            return int(float(retry_after))
        except ValueError:
            pass

    retry_match = re.search(r"Retry will occur after:\s*(\d+)\s*s", str(error))
    if retry_match:
        return int(retry_match.group(1))

    return None


def abort_if_unreasonable_rate_limit_error(error):
    retry_after_seconds = extract_retry_after_seconds(error)
    if retry_after_seconds is not None and retry_after_seconds > SPOTIFY_MAX_AUTO_RETRY_AFTER_SECONDS:
        raise RunAborted(
            "Spotify asked this run to wait too long before retrying "
            f"({retry_after_seconds} seconds, over the 2 minute limit). Exiting early."
        )


def print_spotify_unavailable_notice(context, message):
    notice_key = (context, message)
    if notice_key in SPOTIFY_UNAVAILABLE_NOTICES:
        return
    SPOTIFY_UNAVAILABLE_NOTICES.add(notice_key)
    print(f"Spotify API unavailable for {context}: {message}")


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


def save_spotify_app_config(client_id, client_secret, redirect_uri):
    config_data = load_local_config()
    spotify_config = config_data.get("spotify_app", {})
    spotify_config.update(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }
    )
    config_data["spotify_app"] = spotify_config
    save_local_config(config_data)


def prompt_for_env_var(name, prompt_text, secret=False, default=None):
    value = os.getenv(name)
    if value:
        return value

    if default is not None:
        entered = input(f"{prompt_text} [{default}]: ").strip()
        return entered or default

    if secret and os.getenv("SPOTIFY_PICKER_WEB_STATUS") != "1":
        return getpass.getpass(f"{prompt_text}: ").strip()

    return input(f"{prompt_text}: ").strip()


def prompt_for_visibility_filter():
    while True:
        print_cli_menu(
            "Playlist visibility",
            [
                ("1", "Any"),
                ("2", "Public only"),
                ("3", "Private only"),
            ],
        )
        visibility_choice = input("Enter 1, 2, or 3 [1]: ").strip() or '1'

        if visibility_choice == '1':
            return 'any'
        if visibility_choice == '2':
            return 'public'
        if visibility_choice == '3':
            return 'private'

        print("Invalid choice.")

def prompt_for_link_platform():
    while True:
        print_cli_menu(
            "Links to include",
            [
                ("1", "No links"),
                ("2", "YouTube only"),
                ("3", "Spotify only (requires Spotify API access)"),
                ("4", "Spotify and YouTube (Spotify requires API access)"),
            ],
        )
        print("Spotify links require Spotify app credentials and optional Spotify API calls enabled.")
        choice = input("Enter 1, 2, 3, or 4 [2]: ").strip() or "2"

        if choice == "1":
            return None
        if choice == "2":
            return "youtube"
        if choice == "3":
            return "spotify"
        if choice == "4":
            return "both"

        print("Invalid choice.")


def playlist_matches_visibility(playlist, visibility_filter):
    if visibility_filter == 'any':
        return True

    playlist_public = playlist.get('public')
    if visibility_filter == 'public':
        return playlist_public is True

    if visibility_filter == 'private':
        return playlist_public is False

    return True


def add_visibility_to_cache_file(cache_file, visibility_filter):
    if visibility_filter == 'any':
        return cache_file

    cache_path = Path(cache_file)
    return str(cache_path.with_name(f"{cache_path.stem}_{visibility_filter}{cache_path.suffix}"))


def describe_visibility_filter(visibility_filter):
    return {
        'any': 'Any Visibility',
        'public': 'Public Only',
        'private': 'Private Only',
    }.get(visibility_filter, 'Any Visibility')


def decorate_source_for_visibility(cache_file, fetcher, description, visibility_filter):
    if visibility_filter == 'any':
        return cache_file, fetcher, description

    visibility_label = describe_visibility_filter(visibility_filter)
    return (
        add_visibility_to_cache_file(cache_file, visibility_filter),
        lambda selected_cache_file: fetcher(selected_cache_file, visibility_filter=visibility_filter),
        f"{description} ({visibility_label})",
    )


def resolve_spotify_app_credentials(prompt_if_missing=True):
    saved_spotify_config = load_local_config().get("spotify_app", {})
    client_id = os.getenv("SPOTIPY_CLIENT_ID") or saved_spotify_config.get("client_id")
    client_secret = os.getenv("SPOTIPY_CLIENT_SECRET") or saved_spotify_config.get("client_secret")
    redirect_uri = os.getenv("SPOTIPY_REDIRECT_URI") or saved_spotify_config.get("redirect_uri")

    if not client_id and prompt_if_missing:
        client_id = prompt_for_env_var("SPOTIPY_CLIENT_ID", "Enter your Spotify Client ID")
    if not client_secret and prompt_if_missing:
        client_secret = prompt_for_env_var("SPOTIPY_CLIENT_SECRET", "Enter your Spotify Client Secret", secret=True)
    if not redirect_uri and prompt_if_missing:
        redirect_uri = prompt_for_env_var(
            "SPOTIPY_REDIRECT_URI",
            "Enter your Spotify Redirect URI",
            default=DEFAULT_REDIRECT_URI,
        )

    if not client_id or not client_secret:
        raise SpotifyApiUnavailableError(
            "Spotify client ID and client secret are not configured for this run."
        )

    return client_id, client_secret, redirect_uri or DEFAULT_REDIRECT_URI


def spotify_app_credentials_configured():
    saved_spotify_config = load_local_config().get("spotify_app", {})
    client_id = os.getenv("SPOTIPY_CLIENT_ID") or saved_spotify_config.get("client_id")
    client_secret = os.getenv("SPOTIPY_CLIENT_SECRET") or saved_spotify_config.get("client_secret")
    return bool(client_id and client_secret)


def optional_spotify_api_allowed():
    return not AVOID_OPTIONAL_SPOTIFY_API


def create_spotify_client(prompt_if_missing=True):
    global CURRENT_USER_PROFILE
    client_id, client_secret, redirect_uri = resolve_spotify_app_credentials(prompt_if_missing=prompt_if_missing)

    if not os.getenv("SPOTIPY_CLIENT_ID") and not os.getenv("SPOTIPY_CLIENT_SECRET"):
        save_spotify_app_config(client_id, client_secret, redirect_uri)

    cache_handler = SecureTokenCacheHandler(TOKEN_CACHE_PATH)

    def get_cached_scope_set():
        token_info = cache_handler.get_cached_token() or {}
        return set((token_info.get("scope") or "").split())

    def build_client():
        auth_manager = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope=SCOPE,
            open_browser=True,
            cache_handler=cache_handler,
            requests_session=False,
        )
        return RateLimitedSpotifyClient(
            spotipy.Spotify(
                auth_manager=auth_manager,
                requests_session=False,
            )
        )

    spotify_client = build_client()

    try:
        user = spotify_client.current_user()
        granted_scope_set = get_cached_scope_set()
        missing_scopes = REQUIRED_SCOPE_SET - granted_scope_set
        if missing_scopes:
            print(f"Spotify auth is missing required scopes: {', '.join(sorted(missing_scopes))}")
            print("Clearing the local token cache and retrying authentication with the full scope set...")
            cache_handler.delete_cache()
            spotify_client = build_client()
            user = spotify_client.current_user()
    except spotipy.exceptions.SpotifyOauthError as error:
        print(f"Cached Spotify token could not be refreshed: {error}")
        print("Clearing the local token cache and retrying authentication...")
        cache_handler.delete_cache()
        spotify_client = build_client()
        try:
            user = spotify_client.current_user()
        except (spotipy.exceptions.SpotifyException, spotipy.exceptions.SpotifyOauthError) as retry_error:
            if isinstance(retry_error, spotipy.exceptions.SpotifyException):
                abort_if_unreasonable_rate_limit_error(retry_error)
            raise SpotifyApiUnavailableError(
                f"Authentication failed after clearing the cache: {retry_error}"
            ) from retry_error
    except spotipy.exceptions.SpotifyException as error:
        abort_if_unreasonable_rate_limit_error(error)
        raise SpotifyApiUnavailableError(f"Authentication failed: {error}") from error

    display_name = user.get("display_name") or user.get("id") or "unknown user"
    CURRENT_USER_PROFILE = user
    print(f"Successfully authenticated as {display_name}")
    return spotify_client


def get_spotify_client(required=True, prompt_if_missing=None, context="this step"):
    global SPOTIFY_CLIENT, SPOTIFY_API_UNAVAILABLE_REASON

    if SPOTIFY_CLIENT is not None:
        return SPOTIFY_CLIENT

    if prompt_if_missing is None:
        prompt_if_missing = required

    if SPOTIFY_API_UNAVAILABLE_REASON and not required:
        return None

    try:
        SPOTIFY_CLIENT = create_spotify_client(prompt_if_missing=prompt_if_missing)
        SPOTIFY_API_UNAVAILABLE_REASON = None
        return SPOTIFY_CLIENT
    except (RunAborted, SpotifyApiUnavailableError) as error:
        SPOTIFY_API_UNAVAILABLE_REASON = str(error)
        if required:
            raise
        print_spotify_unavailable_notice(context, SPOTIFY_API_UNAVAILABLE_REASON)
        return None


def offer_spotify_credentials_in_terminal(purpose="Spotify API access"):
    print("\nSpotify API credentials are not configured.")
    print(f"They are needed for {purpose}.")
    choice = input("Enter Spotify credentials now? (yes/no) [no]: ").strip().lower()
    if choice not in {"yes", "y"}:
        return

    try:
        client_id = prompt_for_env_var("SPOTIPY_CLIENT_ID", "Spotify Client ID")
        client_secret = prompt_for_env_var("SPOTIPY_CLIENT_SECRET", "Spotify Client Secret", secret=True)
        save_spotify_app_config(client_id, client_secret, DEFAULT_REDIRECT_URI)
        os.environ["SPOTIPY_CLIENT_ID"] = client_id
        os.environ["SPOTIPY_CLIENT_SECRET"] = client_secret
        print("Credentials saved.")
    except Exception as err:
        print(f"Could not save credentials: {err}")


def get_spotify_search_client(context="Spotify song matching"):
    global SPOTIFY_SEARCH_CLIENT, SPOTIFY_SEARCH_UNAVAILABLE_REASON

    if SPOTIFY_SEARCH_CLIENT is not None:
        return SPOTIFY_SEARCH_CLIENT

    if SPOTIFY_SEARCH_UNAVAILABLE_REASON:
        return None

    if not optional_spotify_api_allowed():
        SPOTIFY_SEARCH_UNAVAILABLE_REASON = "Optional Spotify API calls are disabled for this run."
        return None

    if not spotify_app_credentials_configured():
        offer_spotify_credentials_in_terminal()

    try:
        client_id, client_secret, _redirect_uri = resolve_spotify_app_credentials(prompt_if_missing=False)
        auth_manager = SpotifyClientCredentials(
            client_id=client_id,
            client_secret=client_secret,
        )
        SPOTIFY_SEARCH_CLIENT = RateLimitedSpotifyClient(
            spotipy.Spotify(
                auth_manager=auth_manager,
                requests_session=False,
            )
        )
        return SPOTIFY_SEARCH_CLIENT
    except (RunAborted, SpotifyApiUnavailableError) as error:
        SPOTIFY_SEARCH_UNAVAILABLE_REASON = str(error)
        print_spotify_unavailable_notice(context, SPOTIFY_SEARCH_UNAVAILABLE_REASON)
        return None
    except Exception as error:
        SPOTIFY_SEARCH_UNAVAILABLE_REASON = str(error)
        print_spotify_unavailable_notice(context, SPOTIFY_SEARCH_UNAVAILABLE_REASON)
        return None


def spotify_api_is_available(context="this step"):
    return get_spotify_client(required=False, prompt_if_missing=False, context=context) is not None


class LazySpotifyClient:
    def __getattr__(self, name):
        client = get_spotify_client(required=True, prompt_if_missing=True, context="Spotify API access")
        return getattr(client, name)


sp = LazySpotifyClient()


CLI_WIDTH = 78
BOX_CONTENT_WIDTH = CLI_WIDTH - 4
ANSI_BOLD = "\033[1m"
ANSI_CYAN = "\033[36m"
ANSI_RESET = "\033[0m"


def print_cli_rule(char="-"):
    print(char * CLI_WIDTH)


def terminal_supports_color():
    return (
        sys.stdout.isatty()
        and os.getenv("NO_COLOR") is None
        and os.getenv("TERM", "").lower() != "dumb"
    )


def color_text(text, color):
    if not terminal_supports_color():
        return text
    return f"{color}{text}{ANSI_RESET}"


def visible_len(text):
    width = 0
    for char in str(text):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def pad_visible(text, width):
    return str(text) + (" " * max(0, width - visible_len(text)))


def print_cli_section(title):
    print()
    print_cli_rule("=")
    print(title)
    print_cli_rule("=")


def print_cli_menu(title, options):
    print()
    print(f"{title}:")
    print_cli_rule("-")
    for number, label in options:
        print(f"{number}. {label}")


# Function to sanitize strings for filenames
def sanitize_filename(name):
    valid_chars = "-_.() %s%s" % (string.ascii_letters, string.digits)
    sanitized = ''.join(c for c in name if c in valid_chars)
    sanitized = sanitized.replace(' ', '_')
    return sanitized


# Function to get all tracks from a playlist with a loading bar
def get_playlist_tracks(playlist_id, playlist_name):
    tracks = []
    try:
        results = sp.playlist_items(playlist_id)
        tracks.extend(results['items'])
        total_tracks = results['total']

        with tqdm(total=total_tracks, desc=f"Fetching tracks from '{playlist_name}'", unit='track') as pbar:
            pbar.update(len(results['items']))
            while results['next']:
                results = sp.next(results)
                tracks.extend(results['items'])
                pbar.update(len(results['items']))
    except spotipy.exceptions.SpotifyException as e:
        abort_if_unreasonable_rate_limit_error(e)
        print(f"Error fetching tracks from playlist '{playlist_name}': {e}")
    except Exception as e:
        print(f"Unexpected error fetching tracks from playlist '{playlist_name}': {e}")
    return tracks


def extract_track_from_playlist_item(item):
    track = item.get('track') or item.get('item')
    if not isinstance(track, dict):
        return None
    if track.get('type') and track.get('type') != 'track':
        return None
    return track


def fetch_current_user_playlists(progress_label):
    playlists = []
    results = sp.current_user_playlists(limit=50)
    playlists.extend(results['items'])
    total_playlists = results['total']
    print(f"Total playlists found: {total_playlists}")

    with tqdm(total=total_playlists, desc=progress_label, unit='playlist') as pbar:
        pbar.update(len(results['items']))
        while results['next']:
            results = sp.next(results)
            playlists.extend(results['items'])
            pbar.update(len(results['items']))

    return playlists


def build_song_list_from_tracks(all_tracks):
    song_list = []
    for item in tqdm(all_tracks, desc='Processing tracks', unit='track'):
        track = extract_track_from_playlist_item(item)
        if track:
            track_name = track.get('name') or 'Unknown Title'
            track_artists = track.get('artists', [])
            artist_names = []
            for artist in track_artists:
                if artist:
                    name = artist.get('name')
                    if isinstance(name, str) and name.strip():
                        artist_names.append(name)
                    else:
                        artist_names.append('Unknown Artist')
            artists = ', '.join(artist_names)
            song_entry = {'title': track_name, 'artists': artists}
            song_list.append(song_entry)
    return song_list


def write_song_cache(cache_file, song_list):
    abs_cache_path = os.path.abspath(cache_file)
    print(f"Writing {len(song_list)} songs to cache file: {abs_cache_path}")
    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump(song_list, f, ensure_ascii=False, indent=2)
    print(f"Cache file '{abs_cache_path}' written successfully.")


def fetch_and_cache_filtered_playlist_songs(
    cache_file,
    playlist_filter,
    filter_description,
    progress_label='Fetching playlists',
    visibility_filter='any',
):
    print(f"Starting to fetch and cache songs for playlist filter: {filter_description}...")
    try:
        playlists = fetch_current_user_playlists(progress_label)
        filtered_playlists = [
            playlist
            for playlist in playlists
            if playlist_filter(playlist) and playlist_matches_visibility(playlist, visibility_filter)
        ]
        print(f"Playlists matching '{filter_description}': {len(filtered_playlists)}")

        all_tracks = []
        for playlist in filtered_playlists:
            print(f"Fetching tracks from playlist: {playlist['name']}")
            tracks = get_playlist_tracks(playlist['id'], playlist['name'])
            all_tracks.extend(tracks)

        print(f"Total tracks fetched: {len(all_tracks)}")
        song_list = build_song_list_from_tracks(all_tracks)
        write_song_cache(cache_file, song_list)
        return song_list
    except Exception as e:
        print(f"An error occurred while fetching songs for playlist filter '{filter_description}': {e}")
        return []


# Function to fetch songs from all playlists and update the cache
def fetch_and_cache_all_songs(cache_file, visibility_filter='any'):
    return fetch_and_cache_filtered_playlist_songs(
        cache_file,
        playlist_filter=lambda playlist: True,
        filter_description='All playlists',
        progress_label='Fetching all playlists',
        visibility_filter=visibility_filter,
    )


# Function to fetch songs from Rediscover playlists and update the cache
def fetch_and_cache_rediscover_songs(cache_file, visibility_filter='any'):
    date_pattern = re.compile(r'^Rediscover\s-\s[A-Za-z]{3}\s\d{1,2}(st|nd|rd|th)$', re.IGNORECASE)
    return fetch_and_cache_filtered_playlist_songs(
        cache_file,
        playlist_filter=lambda playlist: bool(date_pattern.match(playlist['name'])),
        filter_description='Rediscover preset',
        visibility_filter=visibility_filter,
    )


# Function to fetch songs from user-created playlists and update the cache
def fetch_and_cache_user_playlists_songs(cache_file, visibility_filter='any'):
    print("Starting to fetch and cache user-created playlist songs...")
    try:
        # Fetch current user's playlists
        playlists = []
        results = sp.current_user_playlists(limit=50)
        playlists.extend(results['items'])
        total_playlists = results['total']
        print(f"Total playlists found: {total_playlists}")

        # Handle pagination if more playlists exist
        with tqdm(total=total_playlists, desc='Fetching your playlists', unit='playlist') as pbar:
            pbar.update(len(results['items']))
            while results['next']:
                results = sp.next(results)
                playlists.extend(results['items'])
                pbar.update(len(results['items']))

        # Filter playlists created by the user
        user_id = sp.current_user()['id']
        user_playlists = [
            plist
            for plist in playlists
            if plist['owner']['id'] == user_id and playlist_matches_visibility(plist, visibility_filter)
        ]
        print(f"User-created playlists found: {len(user_playlists)}")

        # Retrieve tracks from the user playlists
        all_tracks = []
        for playlist in user_playlists:
            print(f"Fetching tracks from your playlist: {playlist['name']}")
            tracks = get_playlist_tracks(playlist['id'], playlist['name'])
            all_tracks.extend(tracks)

        print(f"Total tracks fetched from your playlists: {len(all_tracks)}")

        # Collect song names and artists
        song_list = []
        for item in tqdm(all_tracks, desc='Processing tracks', unit='track'):
            track = extract_track_from_playlist_item(item)
            if track:
                track_name = track.get('name') or 'Unknown Title'
                track_artists = track.get('artists', [])
                artist_names = []
                for artist in track_artists:
                    if artist:
                        name = artist.get('name')
                        if isinstance(name, str) and name.strip():
                            artist_names.append(name)
                        else:
                            artist_names.append('Unknown Artist')
                artists = ', '.join(artist_names)
                song_entry = {'title': track_name, 'artists': artists}
                song_list.append(song_entry)

        # Save song list to cache file
        abs_cache_path = os.path.abspath(cache_file)
        print(f"Writing {len(song_list)} songs to cache file: {abs_cache_path}")
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(song_list, f, ensure_ascii=False, indent=2)
        print(f"Cache file '{abs_cache_path}' written successfully.")

        return song_list
    except Exception as e:
        print(f"An error occurred in fetch_and_cache_user_playlists_songs: {e}")
        return []


def build_filtered_playlist_cache_file(filter_label):
    safe_label = sanitize_filename(filter_label) or "custom_filter"
    return f"song_cache_filtered_{safe_label}.json"


def prompt_for_playlist_name_filter(visibility_filter='any'):
    while True:
        print_cli_menu(
            "Filter playlists by name",
            [
                ("1", "Rediscover preset"),
                ("2", "Contains text"),
                ("3", "Custom regex"),
            ],
        )
        filter_choice = input("Enter 1, 2, or 3: ").strip()

        if filter_choice == '1':
            return {
                'cache_file': 'song_cache_rediscover.json',
                'fetcher': lambda selected_cache_file: fetch_and_cache_rediscover_songs(
                    selected_cache_file,
                    visibility_filter=visibility_filter,
                ),
                'description': 'Rediscover Playlists',
            }

        if filter_choice == '2':
            while True:
                search_text = input("Text to match in playlist names: ").strip()
                if search_text:
                    break
                print("Please enter some text.")

            lowered_search_text = search_text.lower()
            description = f"Playlists containing '{search_text}'"
            cache_file = build_filtered_playlist_cache_file(f"contains_{search_text}")
            return {
                'cache_file': cache_file,
                'fetcher': lambda selected_cache_file: fetch_and_cache_filtered_playlist_songs(
                    selected_cache_file,
                    playlist_filter=lambda playlist: lowered_search_text in playlist['name'].lower(),
                    filter_description=description,
                    visibility_filter=visibility_filter,
                ),
                'description': description,
            }

        if filter_choice == '3':
            while True:
                regex_input = input("Regex for playlist names: ").strip()
                if not regex_input:
                    print("Please enter a regex.")
                    continue

                try:
                    compiled_pattern = re.compile(regex_input, re.IGNORECASE)
                except re.error as error:
                    print(f"Invalid regex: {error}")
                    continue
                break

            description = f"Playlists matching regex '{regex_input}'"
            cache_file = build_filtered_playlist_cache_file(f"regex_{regex_input}")
            return {
                'cache_file': cache_file,
                'fetcher': lambda selected_cache_file: fetch_and_cache_filtered_playlist_songs(
                    selected_cache_file,
                    playlist_filter=lambda playlist: bool(compiled_pattern.search(playlist['name'])),
                    filter_description=description,
                    visibility_filter=visibility_filter,
                ),
                'description': description,
            }

        print("Invalid choice.")


# Function to fetch songs from random playlists
def fetch_and_cache_random_playlists_songs(cache_file, num_songs, visibility_filter='any'):
    print("Starting to fetch and cache random playlist songs...")
    try:
        # Fetch current user's playlists
        playlists = []
        results = sp.current_user_playlists(limit=50)
        playlists.extend(results['items'])
        total_playlists = results['total']
        print(f"Total playlists found: {total_playlists}")

        # Handle pagination if more playlists exist
        while results['next']:
            results = sp.next(results)
            playlists.extend(results['items'])

        playlists = [playlist for playlist in playlists if playlist_matches_visibility(playlist, visibility_filter)]

        if not playlists:
            print("No playlists found.")
            return [], []

        # Shuffle the list of playlists
        random.shuffle(playlists)

        selected_playlists = []
        song_list = []
        seen_song_keys = set()

        for playlist in playlists:
            if len(song_list) >= num_songs:
                break

            print(f"Fetching tracks from random playlist: {playlist['name']}")
            tracks = get_playlist_tracks(playlist['id'], playlist['name'])
            if not tracks:
                continue

            playlist_song_list = build_song_list_from_tracks(tracks)
            if not playlist_song_list:
                print(f"No usable songs found in playlist: {playlist['name']}")
                continue

            selected_playlists.append(playlist)

            for song in playlist_song_list:
                song_key = (song['title'], song['artists'])
                if song_key in seen_song_keys:
                    continue
                seen_song_keys.add(song_key)
                song_list.append(song)
                if len(song_list) >= num_songs:
                    break

        if not song_list:
            print("No tracks found in the selected playlists.")
            return [], []

        print(f"Total usable songs fetched from random playlists: {len(song_list)}")

        # Randomize the song list
        random.shuffle(song_list)

        # Save song list and selected playlists to cache file
        cache_data = {
            'songs': song_list,
            'playlists': [plist['name'] for plist in selected_playlists]
        }
        abs_cache_path = os.path.abspath(cache_file)
        print(f"Writing {len(song_list)} songs to cache file: {abs_cache_path}")
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        print(f"Cache file '{abs_cache_path}' written successfully.")

        return song_list, cache_data['playlists']
    except Exception as e:
        print(f"An error occurred in fetch_and_cache_random_playlists_songs: {e}")
        return [], []


# Function to parse user input with support for comma-separated keywords and quoted phrases
def parse_keywords(input_string):
    # Regular expression to match quoted phrases or separate by commas
    pattern = r'(?:"([^"]+)"|([^,]+))'
    matches = re.findall(pattern, input_string)

    # Flatten results and strip extra whitespace
    keywords = [kw[0] or kw[1] for kw in matches]  # Each match is a tuple; pick the non-empty group
    return [kw.strip() for kw in keywords]


def fetch_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def random_song_api_get(path, params=None):
    query = urllib.parse.urlencode(params or {})
    url = f"{RANDOM_SONG_API_BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"
    return fetch_json(url)


def fetch_random_song_generator_options():
    markets_response = random_song_api_get("/getMarkets")
    genres_response = random_song_api_get("/getGenres")
    decades_response = random_song_api_get("/getDecades")
    return {
        "markets": markets_response.get("data", []),
        "genres": genres_response.get("data", []),
        "decades": decades_response.get("data", []),
    }


def prompt_for_random_song_generator_config(options):
    print("Configure random-song.com (press Enter to keep any option random):")

    available_genres = [genre.get("name") for genre in options["genres"] if genre.get("name")]
    available_markets = [market.get("name") for market in options["markets"] if market.get("name")]
    available_decades = [decade.get("name") for decade in options["decades"] if decade.get("name")]

    while True:
        genre = input("Genre (example: ambient, rock, jazz) [random]: ").strip() or "random"
        if genre == "random" or genre == "none" or genre in available_genres:
            break
        print("That genre is not in random-song.com's published list.")

    while True:
        market = input("Market/country name (example: Germany, Japan) [random]: ").strip() or "random"
        if market == "random" or market in available_markets:
            break
        print("That market is not in random-song.com's published list.")

    while True:
        decade = input("Decade (example: 1990s, 2000s) [random]: ").strip() or "random"
        if decade == "random" or decade == "all" or decade in available_decades:
            break
        print("That decade is not in random-song.com's published list.")

    tag_new = input("New releases only? (yes/no) [no]: ").strip().lower() == "yes"
    exclude_singles = input("Exclude singles? (yes/no) [no]: ").strip().lower() == "yes"

    return {
        "market": market,
        "genre": genre,
        "decade": decade,
        "tag_new": tag_new,
        "exclude_singles": exclude_singles,
    }


def build_random_song_generator_default_config():
    return {
        "market": "random",
        "genre": "random",
        "decade": "all",
        "tag_new": False,
        "exclude_singles": False,
    }


def fetch_random_song_generator_track(config, retries=8):
    last_response = None
    for _ in range(retries):
        params = {
            "market": config["market"],
            "genre": config["genre"],
            "decade": config["decade"],
            "tag_new": str(config["tag_new"]).lower(),
            "exclude_singles": str(config["exclude_singles"]).lower(),
        }

        try:
            response = random_song_api_get("/getRandomTrack", params)
        except Exception as error:
            last_response = {"status": 503, "message": str(error)}
            continue

        last_response = response
        if response.get("status") == 200:
            track = response.get("data", {}).get("track")
            if track:
                return track, response.get("meta_data", {})

    return None, last_response


def convert_random_song_generator_track(track, meta_data=None):
    artists = ", ".join(artist.get("name", "Unknown Artist") for artist in track.get("artists", []))
    song = {
        "title": track.get("name") or "Unknown Title",
        "artists": artists or "Unknown Artist",
        "spotify_url": track.get("link"),
        "spotify_found_exact_track": bool(track.get("link")),
        "source": "random-song.com",
    }
    if meta_data:
        song["random_song_meta"] = meta_data
    return song


def fetch_songs_from_random_song_generator(num_songs, mode, config=None):
    print("Fetching songs from random-song.com...")
    emit_web_status("random-song", "Asking random-song.com for tracks...", reset=True)
    song_list = []
    seen_song_keys = set()
    max_total_attempts = max(num_songs * 6, 10)
    total_attempts = 0

    with tqdm(total=num_songs, desc='Getting random-song.com tracks', unit='song') as progress_bar:
        while len(song_list) < num_songs and total_attempts < max_total_attempts:
            total_attempts += 1
            active_config = build_random_song_generator_default_config() if mode == "default-config" else config
            track, meta_data = fetch_random_song_generator_track(active_config)
            if not track:
                print(f"random-song.com did not return a track for config: {active_config}")
                continue

            song = convert_random_song_generator_track(track, meta_data)
            song_key = (song["title"], song["artists"])
            if song_key in seen_song_keys:
                continue

            seen_song_keys.add(song_key)
            song_list.append(song)
            emit_web_status("random-song", f"Collected {len(song_list)} random song(s).")
            progress_bar.update(1)

    if len(song_list) < num_songs:
        print(f"random-song.com returned {len(song_list)} unique song(s) after {total_attempts} attempt(s).")

    emit_web_status("random-song", f"random-song.com returned {len(song_list)} song(s).", done=True)
    return song_list


def build_youtube_search_url(song):
    query = f"{song['title']} {song['artists']}"
    encoded_query = urllib.parse.quote_plus(query)
    return f"https://www.youtube.com/results?search_query={encoded_query}"


def build_spotify_search_url(song):
    query = f"{song['title']} {song['artists']}".strip()
    encoded_query = urllib.parse.quote(query)
    return f"https://open.spotify.com/search/{encoded_query}"


def find_youtube_url(song):
    if song.get('youtube_url'):
        return song.get('youtube_url'), song.get('youtube_found_exact_video', True)

    search_url = build_youtube_search_url(song)
    request = urllib.request.Request(
        search_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            html = response.read().decode("utf-8", errors="ignore")
    except Exception as error:
        print(f"Could not search YouTube for {song['title']} by {song['artists']}: {error}")
        return search_url, False

    match = re.search(r'"videoRenderer"\s*:\s*\{\s*"videoId"\s*:\s*"([^"]+)"', html)
    if match:
        return f"https://www.youtube.com/watch?v={match.group(1)}", True

    return search_url, False


def find_spotify_url(song, allow_search_fallback=False):
    global SPOTIFY_LINK_MATCHING_DISABLED_REASON

    if song.get('spotify_url') and song.get('spotify_found_exact_track'):
        return song.get('spotify_url'), True

    if SPOTIFY_LINK_MATCHING_DISABLED_REASON:
        return (build_spotify_search_url(song), False) if allow_search_fallback else (None, False)

    try:
        spotify_match = resolve_song_on_spotify(song, suppress_errors=True)
    except RunAborted as error:
        SPOTIFY_LINK_MATCHING_DISABLED_REASON = str(error)
        add_report_list_item("warnings", f"Spotify exact-link matching stopped early: {error}")
        return (build_spotify_search_url(song), False) if allow_search_fallback else (None, False)

    if spotify_match and spotify_match.get('spotify_url'):
        if spotify_match.get('spotify_uri'):
            song['spotify_uri'] = spotify_match['spotify_uri']
        return spotify_match['spotify_url'], True

    return (build_spotify_search_url(song), False) if allow_search_fallback else (None, False)


def find_spotify_track_for_song(song, spotify_client=None):
    if song.get('spotify_uri'):
        return song['spotify_uri']

    if song.get('spotify_url'):
        spotify_url = song['spotify_url']
        if "/track/" in spotify_url:
            track_id = spotify_url.rstrip('/').split('/')[-1].split('?')[0]
            return f"spotify:track:{track_id}"

    spotify_match = resolve_song_on_spotify(song, spotify_client=spotify_client)
    if not spotify_match:
        return None

    return spotify_match.get('spotify_uri')


def get_spotify_client_for_playlist_creation():
    try:
        if not spotify_app_credentials_configured():
            offer_spotify_credentials_in_terminal(purpose="Spotify playlist creation")
        if os.getenv("SPOTIFY_PICKER_WEB_STATUS") == "1":
            print(
                "Spotify may ask you to authorize in a browser. "
                "If asked for the redirected URL, copy the full browser address after approving and paste it here."
            )
        return get_spotify_client(
            required=True,
            prompt_if_missing=True,
            context="Spotify playlist creation",
        )
    except (RunAborted, SpotifyApiUnavailableError) as error:
        message = f"Spotify playlist creation needs Spotify API access: {error}"
        print(message)
        update_run_report(
            spotify_playlist_status="failed: Spotify unavailable",
            errors=message,
        )
        emit_web_status("playlist", message, level="error", reset=True, done=True)
        return None


def attach_platform_links(selected_songs, link_platform):
    global SPOTIFY_LINK_MATCHING_DISABLED_REASON

    for song in selected_songs:
        song['spotify_requested'] = link_platform in {'spotify', 'both'}
        song['youtube_requested'] = link_platform in {'youtube', 'both'}

    if link_platform in {'spotify', 'both'}:
        emit_web_status("links", "Finding Spotify links...", reset=True)
        print("Finding Spotify links for the selected songs...")
        exact_count = 0
        missing_count = 0

        for song in tqdm(selected_songs, desc='Finding Spotify links', unit='song'):
            try:
                spotify_url, found_exact_track = find_spotify_url(
                    song,
                    allow_search_fallback=False,
                )
            except Exception as error:
                SPOTIFY_LINK_MATCHING_DISABLED_REASON = str(error)
                add_report_list_item("warnings", f"Spotify link lookup stopped: {error}")
                spotify_url, found_exact_track = None, False

            song['spotify_url'] = spotify_url
            song['spotify_found_exact_track'] = found_exact_track

            if found_exact_track:
                exact_count += 1
            else:
                missing_count += 1

        update_run_report(
            links_added=True,
            spotify_exact_links=exact_count,
            spotify_search_links=0,
            spotify_missing_links=missing_count,
        )

    if link_platform in {'youtube', 'both'}:
        emit_web_status("links", "Preparing YouTube Music links...")
        print("Looking up YouTube links for the selected songs...")
        youtube_count = 0

        for song in tqdm(selected_songs, desc='Finding YouTube links', unit='song'):
            youtube_url, found_exact_video = find_youtube_url(song)
            song['youtube_url'] = youtube_url
            song['youtube_found_exact_video'] = found_exact_video

            if youtube_url:
                youtube_count += 1

        update_run_report(youtube_links=youtube_count)

    emit_web_status("links", "Links are ready.", done=True)


def build_keyword_combinations(keywords):
    """Return search queries to try, ordered from most specific to broadest.

    Individual keywords are tried first so that a focused genre term like
    "space disco" or "morning coffee" gets its own search pass before we
    attempt broader pair combinations that are often too specific for YouTube
    Music's index and return zero results.
    """
    keyword_combinations = []
    seen_combinations = set()

    # Pass 1: individual keywords (most likely to return results)
    for keyword in keywords:
        if keyword not in seen_combinations:
            keyword_combinations.append(keyword)
            seen_combinations.add(keyword)

    # Pass 2: pairs of keywords (supplementary, more specific)
    for i in range(len(keywords)):
        for j in range(i + 1, len(keywords)):
            combined = f"{keywords[i]} {keywords[j]}"
            if combined not in seen_combinations:
                keyword_combinations.append(combined)
                seen_combinations.add(combined)

    return keyword_combinations


def normalize_song_key(title, artists):
    normalized_title = re.sub(r"\s+", " ", (title or "").strip().lower())
    normalized_artists = re.sub(r"\s+", " ", (artists or "").strip().lower())
    return normalized_title, normalized_artists


def tokenize_for_match(text):
    cleaned_text = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    return {token for token in cleaned_text.split() if token}


def spotify_match_is_plausible(song, track):
    source_title_tokens = tokenize_for_match(song.get('title'))
    matched_title_tokens = tokenize_for_match(track.get('name'))
    if not source_title_tokens or not matched_title_tokens:
        return False

    title_overlap = source_title_tokens & matched_title_tokens
    if len(title_overlap) < max(1, min(len(source_title_tokens), 2)):
        return False

    source_artists = (song.get('artists') or '').strip()
    if source_artists and source_artists != 'Unknown Artist':
        source_artist_tokens = tokenize_for_match(source_artists)
        matched_artist_tokens = tokenize_for_match(' '.join(artist.get('name', '') for artist in track.get('artists', [])))
        if source_artist_tokens and matched_artist_tokens and not (source_artist_tokens & matched_artist_tokens):
            return False

    if source_artists == 'Unknown Artist' and len(source_title_tokens) > 8 and len(title_overlap) < 3:
        return False

    return True


def merge_song_candidates(song_map, songs, source_label):
    for song in songs:
        title = (song.get('title') or '').strip()
        artists = (song.get('artists') or '').strip()
        if not title or not artists:
            continue

        song_key = normalize_song_key(title, artists)
        if song_key not in song_map:
            song_copy = dict(song)
            song_copy['discovery_sources'] = [source_label]
            song_map[song_key] = song_copy
        else:
            existing = song_map[song_key]
            if source_label not in existing.setdefault('discovery_sources', []):
                existing['discovery_sources'].append(source_label)


def get_youtube_music_client():
    global YOUTUBE_MUSIC_CLIENT
    if YOUTUBE_MUSIC_CLIENT is None:
        YOUTUBE_MUSIC_CLIENT = YTMusic()
    return YOUTUBE_MUSIC_CLIENT


def parse_youtube_music_playlist_id(browse_id):
    if not browse_id:
        return None
    return browse_id[2:] if browse_id.startswith("VL") else browse_id


def parse_youtube_music_item_count(raw_count):
    if isinstance(raw_count, int):
        return raw_count
    if not isinstance(raw_count, str):
        return None

    normalized = raw_count.strip().replace(",", "")
    multiplier = 1
    if normalized.lower().endswith("k"):
        multiplier = 1_000
        normalized = normalized[:-1]
    elif normalized.lower().endswith("m"):
        multiplier = 1_000_000
        normalized = normalized[:-1]

    try:
        return int(float(normalized) * multiplier)
    except ValueError:
        return None


def youtube_music_artist_names(item):
    artists = item.get("artists") or []
    names = []
    for artist in artists:
        if isinstance(artist, dict) and artist.get("name"):
            names.append(artist["name"])
    return ", ".join(names) or "Unknown Artist"


def convert_youtube_music_track_to_song(track, source, playlist_id=None, playlist_title=None):
    if not isinstance(track, dict):
        return None

    title = track.get("title")
    video_id = track.get("videoId")
    video_type = track.get("videoType")
    if not title or not video_id:
        return None

    if video_type and video_type not in YOUTUBE_MUSIC_VIDEO_TYPES:
        return None

    duration_seconds = track.get("duration_seconds")
    if isinstance(duration_seconds, int) and duration_seconds > 900:
        return None

    song = {
        "title": title,
        "artists": youtube_music_artist_names(track),
        "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
        "youtube_found_exact_video": True,
        "youtube_music_video_id": video_id,
        "youtube_music_video_type": video_type,
        "youtube_music_verified": True,
        "source": source,
    }
    if playlist_id:
        song["youtube_music_playlist_id"] = playlist_id
    if playlist_title:
        song["youtube_music_playlist_title"] = playlist_title
    album = track.get("album")
    if isinstance(album, dict) and album.get("name"):
        song["album"] = album["name"]
    return song


def search_youtube_music_playlists_by_keywords(keywords, max_playlists, candidate_multiplier=3):
    emit_web_status("search", "Searching YouTube Music playlists...", reset=True)
    youtube_music = get_youtube_music_client()
    playlists = []
    seen_playlist_ids = set()
    target_count = max(max_playlists * candidate_multiplier, max_playlists)

    for keyword_combo in build_keyword_combinations(keywords):
        print(f"Searching YouTube Music playlists with: {keyword_combo}")
        for playlist_filter in ("featured_playlists", "community_playlists"):
            try:
                results = youtube_music.search(
                    keyword_combo,
                    filter=playlist_filter,
                    limit=min(YOUTUBE_MUSIC_SEARCH_LIMIT, target_count),
                )
            except Exception as error:
                print(f"Could not search YouTube Music {playlist_filter} for '{keyword_combo}': {summarise_network_error(error)}")
                if is_network_down_error(error):
                    emit_web_status("search", f"Network down — found {len(playlists)} playlist candidate(s).")
                    return playlists
                continue

            for result in results:
                playlist_id = parse_youtube_music_playlist_id(result.get("browseId"))
                if not playlist_id or playlist_id in seen_playlist_ids:
                    continue

                seen_playlist_ids.add(playlist_id)
                playlists.append({
                    "id": playlist_id,
                    "title": result.get("title") or playlist_id,
                    "source_filter": playlist_filter,
                    "item_count": parse_youtube_music_item_count(result.get("itemCount")),
                })
                if len(playlists) >= target_count:
                    emit_web_status("search", f"Found {len(playlists)} playlist candidates.")
                    return playlists

    emit_web_status("search", f"Found {len(playlists)} playlist candidates.")
    return playlists


def is_network_down_error(error):
    msg = str(error)
    return any(s in msg for s in (
        "NewConnectionError",
        "Failed to establish a new connection",
        "Network is unreachable",
        "Errno -3",
        "ConnectionRefusedError",
        "No route to host",
    ))


def summarise_network_error(error):
    msg = str(error)
    if "NewConnectionError" in msg or "Failed to establish a new connection" in msg:
        return "network unreachable"
    if "Read timed out" in msg or "ReadTimeoutError" in msg:
        return "read timed out"
    if "ConnectTimeoutError" in msg:
        return "connection timed out"
    return msg.split("\n")[0][:120]


def fetch_songs_from_youtube_music_playlists(
    playlists,
    max_playlists,
    max_tracks_per_playlist,
    min_playlist_size=None,
    max_playlist_size=None,
):
    emit_web_status("verify", "Reading YouTube Music playlist tracks...", reset=True)
    youtube_music = get_youtube_music_client()
    song_list = []
    playlists_used = 0
    consecutive_net_errors = 0

    for playlist in playlists:
        if playlists_used >= max_playlists:
            break

        playlist_id = playlist["id"]
        playlist_title = playlist.get("title") or playlist_id
        print(f"Reading YouTube Music playlist: {playlist_title}")
        try:
            playlist_data = youtube_music.get_playlist(
                playlist_id,
                limit=max(max_tracks_per_playlist, 1),
            )
            consecutive_net_errors = 0
        except Exception as error:
            short_err = summarise_network_error(error)
            print(f"Could not load YouTube Music playlist '{playlist_title}': {short_err}")
            if is_network_down_error(error):
                consecutive_net_errors += 1
                if consecutive_net_errors >= YOUTUBE_MUSIC_MAX_CONSECUTIVE_NET_ERRORS:
                    print(f"Network appears to be down — stopping search with {len(song_list)} song(s) collected so far.")
                    break
            continue

        if not isinstance(playlist_data, dict):
            print(f"Could not load YouTube Music playlist '{playlist_title}': unexpected response type.")
            continue

        track_count = playlist_data.get("trackCount")
        if not isinstance(track_count, int):
            track_count = len(playlist_data.get("tracks") or [])

        if min_playlist_size is not None and track_count < min_playlist_size:
            print(f"Skipping YouTube Music playlist '{playlist_title}' because it has only {track_count} songs.")
            continue

        if max_playlist_size is not None and track_count > max_playlist_size:
            print(f"Skipping YouTube Music playlist '{playlist_title}' because it has {track_count} songs.")
            continue

        playlist_songs = []
        for track in (playlist_data.get("tracks") or [])[:max_tracks_per_playlist]:
            song = convert_youtube_music_track_to_song(
                track,
                source="youtube-music-playlist",
                playlist_id=playlist_id,
                playlist_title=playlist_title,
            )
            if song:
                playlist_songs.append(song)

        if playlist_songs:
            song_list.extend(playlist_songs)
            playlists_used += 1
            print(f"Recovered {len(playlist_songs)} YouTube Music song(s) from '{playlist_title}'.")
            emit_web_status(
                "verify",
                f"Recovered {len(song_list)} verified songs from {playlists_used} playlist(s).",
            )

    return song_list


def search_youtube_music_songs_by_keywords(keywords, max_songs):
    emit_web_status("fallback", "Searching YouTube Music songs...", reset=True)
    youtube_music = get_youtube_music_client()
    song_list = []
    seen_song_keys = set()

    for keyword_combo in build_keyword_combinations(keywords):
        print(f"Searching YouTube Music songs with: {keyword_combo}")
        for search_filter in ("songs", "videos"):
            try:
                results = youtube_music.search(
                    keyword_combo,
                    filter=search_filter,
                    limit=min(YOUTUBE_MUSIC_SEARCH_LIMIT, max_songs),
                )
            except Exception as error:
                print(f"Could not search YouTube Music {search_filter} for '{keyword_combo}': {summarise_network_error(error)}")
                if is_network_down_error(error):
                    emit_web_status("fallback", f"Network down — found {len(song_list)} song(s) so far.")
                    return song_list[:max_songs]
                continue

            recovered_count = 0
            for result in results:
                song = convert_youtube_music_track_to_song(
                    result,
                    source=f"youtube-music-{search_filter}",
                )
                if song and merge_song_list_with_seen(song_list, seen_song_keys, song):
                    recovered_count += 1
                if len(song_list) >= max_songs:
                    emit_web_status("fallback", f"Found {len(song_list)} verified YouTube Music songs.")
                    return song_list[:max_songs]

            print(f"Recovered {recovered_count} YouTube Music {search_filter} result(s) for '{keyword_combo}'.")

    emit_web_status("fallback", f"Found {len(song_list)} verified YouTube Music songs.")
    return song_list[:max_songs]


def fetch_songs_from_spotify_genre_recommendations(keywords, max_songs=100):
    """Discover songs using Spotify's recommendations API seeded by genre.

    Spotify maintains a curated list of genre seeds (e.g. "jazz", "disco",
    "ambient").  This function fuzzy-matches each keyword against that list,
    picks the best genre seeds, and calls spotify.recommendations() which
    returns actual tracks — far more reliable than keyword-searching YouTube
    Music playlist titles for mood/genre terms like "morning coffee" or
    "space disco".

    Returns a list of song dicts in the same format used by the rest of the
    pipeline, or an empty list if Spotify is unavailable.
    """
    spotify_client = get_spotify_search_client(context="Spotify genre recommendations")
    if spotify_client is None:
        return []

    # Fetch available genre seeds once and cache on the function object.
    if not hasattr(fetch_songs_from_spotify_genre_recommendations, "_genre_cache"):
        try:
            result = spotify_client.recommendation_genre_seeds()
            fetch_songs_from_spotify_genre_recommendations._genre_cache = (
                result.get("genres") or []
            )
        except Exception as error:
            print(f"Could not fetch Spotify genre seeds: {error}")
            fetch_songs_from_spotify_genre_recommendations._genre_cache = []

    available_genres = fetch_songs_from_spotify_genre_recommendations._genre_cache
    if not available_genres:
        return []

    def best_genre_match(keyword):
        """Return the closest Spotify genre seed for a free-text keyword."""
        keyword_lower = keyword.lower()
        # Exact match first.
        if keyword_lower in available_genres:
            return keyword_lower
        # Substring containment: genre is part of the keyword or vice versa.
        for genre in available_genres:
            if genre in keyword_lower or keyword_lower in genre:
                return genre
        # Word-overlap: share at least one word token.
        keyword_tokens = set(re.split(r"[\s\-_]+", keyword_lower))
        best, best_score = None, 0
        for genre in available_genres:
            genre_tokens = set(re.split(r"[\s\-_]+", genre))
            score = len(keyword_tokens & genre_tokens)
            if score > best_score:
                best, best_score = genre, score
        return best if best_score > 0 else None

    matched_genres = []
    seen_genres = set()
    for keyword in keywords:
        genre = best_genre_match(keyword)
        if genre and genre not in seen_genres:
            matched_genres.append(genre)
            seen_genres.add(genre)

    if not matched_genres:
        print(f"No Spotify genre seeds matched for keywords: {', '.join(keywords)}")
        return []

    # Spotify allows at most 5 seed genres per recommendations call.
    seed_genres = matched_genres[:5]
    print(f"Fetching Spotify genre recommendations for seeds: {', '.join(seed_genres)}")
    emit_web_status("search", f"Querying Spotify genre recommendations: {', '.join(seed_genres)}")

    try:
        result = spotify_client.recommendations(
            seed_genres=seed_genres,
            limit=min(max_songs, 100),
        )
    except Exception as error:
        print(f"Could not fetch Spotify genre recommendations: {error}")
        return []

    song_list = []
    for track in result.get("tracks") or []:
        title = track.get("name")
        artist_names = ", ".join(
            a.get("name", "") for a in (track.get("artists") or []) if a.get("name")
        )
        spotify_url = track.get("external_urls", {}).get("spotify")
        spotify_uri = track.get("uri")
        if not title or not artist_names:
            continue
        song_list.append({
            "title": title,
            "artists": artist_names,
            "spotify_url": spotify_url,
            "spotify_uri": spotify_uri,
            "spotify_found_exact_track": bool(spotify_url),
            "source": "spotify-genre-recommendations",
        })

    print(f"Recovered {len(song_list)} song(s) from Spotify genre recommendations.")
    return song_list


def fetch_youtube_music_public_discovery_songs(
    keywords,
    max_playlists=10,
    max_songs=500,
    max_tracks_per_playlist=35,
    min_playlist_size=None,
    max_playlist_size=None,
):
    """Discover songs via YouTube Music playlist search and direct song search.

    This function is pure YouTube Music — no Spotify calls.  For genre-based
    Surprise Me discovery, see fetch_surprise_discovery_tiered() which tries
    Spotify genre recommendations first and only calls this as a fallback.
    """
    emit_web_status("search", "Searching YouTube Music playlists...", reset=True)

    song_map = {}
    playlists = search_youtube_music_playlists_by_keywords(keywords, max_playlists)
    playlist_songs = fetch_songs_from_youtube_music_playlists(
        playlists,
        max_playlists=max_playlists,
        max_tracks_per_playlist=max_tracks_per_playlist,
        min_playlist_size=min_playlist_size,
        max_playlist_size=max_playlist_size,
    )
    merge_song_candidates(song_map, playlist_songs, "youtube-music-playlists")

    if len(song_map) < max_songs:
        direct_limit = max(max_songs - len(song_map), max_tracks_per_playlist)
        direct_songs = search_youtube_music_songs_by_keywords(keywords, direct_limit)
        merge_song_candidates(song_map, direct_songs, "youtube-music-search")

    song_list = list(song_map.values())
    random.shuffle(song_list)
    print(
        f"Compiled {len(song_list)} verified YouTube Music song candidate(s) for: {', '.join(keywords)}"
    )
    emit_web_status(
        "complete",
        f"Found {len(song_list)} verified YouTube Music song(s).",
        reset=True,
        done=True,
    )
    return song_list[:max_songs]


# ---------------------------------------------------------------------------
# Tiered discovery for "Surprise Me"
# ---------------------------------------------------------------------------

# Minimum number of Spotify recommendation tracks required to skip the
# YouTube Music fallback entirely.  If fewer arrive (e.g. a genre seed maps
# to a very small catalogue), we still return what we have — the retry loop
# in fetch_surprise_public_discovery_with_retries will pick new keywords if
# the total is too low.
SPOTIFY_GENRE_RECOMMENDATIONS_MIN_SONGS = 10


def fetch_surprise_discovery_tiered(
    keywords,
    max_songs,
    max_playlists,
    max_tracks_per_playlist,
    min_playlist_size,
    max_playlist_size,
):
    """Genre-based song discovery with ordered fallback tiers.

    Tier 1 — Spotify genre recommendations (when credentials are configured):
        Maps the random keywords to the closest Spotify genre seeds and calls
        spotify.recommendations().  This is a maximum of 2 API calls (genre
        seed list is cached after the first call) and directly returns real
        tracks, making it far more reliable than text-searching YouTube Music
        playlist titles for mood/vibe terms like "morning coffee" or
        "space disco".  If this produces songs we return immediately without
        touching YouTube Music.

    Tier 2 — YouTube Music (always available, no credentials needed):
        Falls back to the YouTube Music playlist + direct song search path
        when Spotify is not configured, returns zero results, or fails.

    Returns (song_list, effective_mode_label).
    """
    # ---- Tier 1: Spotify genre recommendations ----
    if optional_spotify_api_allowed() and spotify_app_credentials_configured():
        print("Trying Spotify genre recommendations (Tier 1)...")
        emit_web_status(
            "search",
            f"Querying Spotify genre recommendations for: {', '.join(keywords)}",
        )
        try:
            spotify_songs = fetch_songs_from_spotify_genre_recommendations(keywords, max_songs)
        except Exception as error:
            print(f"Spotify genre recommendations failed unexpectedly: {error}")
            spotify_songs = []

        if spotify_songs:
            print(
                f"Tier 1 (Spotify): found {len(spotify_songs)} song(s) "
                f"via genre recommendations — skipping YouTube Music."
            )
            emit_web_status(
                "complete",
                f"Found {len(spotify_songs)} song(s) via Spotify genre recommendations.",
                reset=True,
                done=True,
            )
            return spotify_songs, "spotify-genre-recommendations"

        print(
            "Tier 1 (Spotify): no recommendations returned. "
            "Falling back to YouTube Music (Tier 2)..."
        )
    elif not optional_spotify_api_allowed():
        print("Optional Spotify API calls are disabled — using YouTube Music discovery.")
    else:
        print("Spotify credentials not configured — starting at YouTube Music (Tier 2).")

    # ---- Tier 2: YouTube Music ----
    song_list = fetch_youtube_music_public_discovery_songs(
        keywords,
        max_playlists=max_playlists,
        max_songs=max_songs,
        max_tracks_per_playlist=max_tracks_per_playlist,
        min_playlist_size=min_playlist_size,
        max_playlist_size=max_playlist_size,
    )
    return song_list, DEFAULT_PUBLIC_DISCOVERY_MODE


def merge_song_list_with_seen(song_list, seen_song_keys, song):
    song_key = normalize_song_key(song.get('title', ''), song.get('artists', ''))
    if song_key in seen_song_keys:
        return False

    seen_song_keys.add(song_key)
    song_list.append(song)
    return True


def build_spotify_search_queries(song):
    title = (song.get('title') or '').strip()
    artists = (song.get('artists') or '').strip()
    queries = []

    if title and artists and artists != 'Unknown Artist':
        queries.append((f"track:{title} artist:{artists}", True))
        queries.append((f"{title} {artists}", False))

    if title:
        queries.append((f"track:{title}", False))
        queries.append((title, False))

    return queries


def resolve_song_on_spotify(song, suppress_errors=False, spotify_client=None):
    global SPOTIFY_LINK_MATCHING_DISABLED_REASON
    spotify_client = spotify_client or get_spotify_search_client(context="Spotify song matching")
    if spotify_client is None:
        return None

    song_key = normalize_song_key(song.get('title', ''), song.get('artists', ''))
    if song_key in SPOTIFY_MATCH_CACHE:
        return SPOTIFY_MATCH_CACHE[song_key]

    for query, is_exact_query in build_spotify_search_queries(song):
        try:
            result = spotify_client.search(q=query, type='track', limit=5)
        except spotipy.exceptions.SpotifyException as error:
            abort_if_unreasonable_rate_limit_error(error)
            if not suppress_errors:
                print(f"Could not search Spotify for {song.get('title', 'Unknown Title')} by {song.get('artists', 'Unknown Artist')}: {error}")
            continue
        except Exception as error:
            SPOTIFY_LINK_MATCHING_DISABLED_REASON = str(error)
            if not suppress_errors:
                print(f"Spotify search failed ({error}); switching to search-page links.")
            break

        tracks = result.get('tracks', {}).get('items', [])
        if not tracks:
            continue

        track = None
        for candidate_track in tracks:
            if spotify_match_is_plausible(song, candidate_track):
                track = candidate_track
                break

        if track is None:
            continue

        spotify_match = {
            'spotify_url': track.get('external_urls', {}).get('spotify'),
            'spotify_uri': track.get('uri'),
            'spotify_found_exact_track': bool(track.get('external_urls', {}).get('spotify')),
        }
        SPOTIFY_MATCH_CACHE[song_key] = spotify_match
        return spotify_match

    SPOTIFY_MATCH_CACHE[song_key] = None
    return None


def describe_public_discovery_mode(discovery_mode):
    return {
        'youtube-music': 'YouTube Music discovery',
        'spotify-genre-recommendations': 'Spotify genre recommendations',
    }.get(discovery_mode, 'YouTube Music discovery')


# Function to fetch songs from public music sources by keywords/phrases without caching
def fetch_songs_from_public_playlists_by_keywords(
    keywords,
    max_playlists=10,
    max_songs=500,
    max_tracks_per_playlist=35,
    min_playlist_size=None,
    max_playlist_size=None,
    discovery_mode=DEFAULT_PUBLIC_DISCOVERY_MODE,
):
    if discovery_mode != DEFAULT_PUBLIC_DISCOVERY_MODE:
        print(
            "Classic public YouTube discovery modes have been retired. "
            "Using YouTube Music discovery instead."
        )

    return fetch_youtube_music_public_discovery_songs(
        keywords,
        max_playlists=max_playlists,
        max_songs=max_songs,
        max_tracks_per_playlist=max_tracks_per_playlist,
        min_playlist_size=min_playlist_size,
        max_playlist_size=max_playlist_size,
    )


def create_spotify_playlist(selected_songs):
    spotify_client = get_spotify_client_for_playlist_creation()
    if spotify_client is None:
        return

    # Prompt for playlist name
    playlist_name = input("Enter a name for your new playlist: ").strip()
    if not playlist_name:
        playlist_name = "New Playlist"
        print(f"No name entered. Using default name: {playlist_name}")

    track_uris = []
    for song in selected_songs:
        track_uri = find_spotify_track_for_song(song, spotify_client=spotify_client)
        if track_uri:
            track_uris.append(track_uri)
        else:
            print(f"Track not found: {song['title']} by {song['artists']}")

    if not track_uris:
        print("No tracks were found to add, so the playlist was not created.")
        update_run_report(spotify_playlist_status="failed: no Spotify tracks found", spotify_playlist_name=playlist_name)
        return

    # Create the playlist only after we know there is something to add.
    try:
        playlist = spotify_client.current_user_playlist_create(playlist_name, public=False)
        print(f"Playlist '{playlist_name}' created successfully.")
        update_run_report(spotify_playlist_status="created", spotify_playlist_name=playlist_name)
    except spotipy.exceptions.SpotifyException as e:
        abort_if_unreasonable_rate_limit_error(e)
        print(f"Error creating playlist: {e}")
        update_run_report(
            spotify_playlist_status="failed: could not create playlist",
            spotify_playlist_name=playlist_name,
            errors=f"Spotify playlist creation failed: {e}",
        )
        if getattr(e, "http_status", None) == 403:
            user_id = (CURRENT_USER_PROFILE or {}).get('id', 'unknown')
            print("Spotify rejected playlist creation with HTTP 403.")
            print("The most common causes are:")
            print("- the cached token was authorized without playlist-write scopes")
            print("- the token belongs to an older Spotify app configuration")
            print("- the request is not being treated as the current authenticated user")
            print("This script requests playlist-modify-private and creates playlists through the current-user endpoint.")
            print("Delete ~/.spotify-scripts/token_cache.json and run the script again to force a fresh Spotify consent flow.")
            print(f"Authenticated user during this run: {user_id}")
        if getattr(e, "http_status", None) == 429:
            print(
                "Spotify is still rate-limiting playlist creation after the built-in backoff. "
                "Wait a bit before trying again, or reduce the number of Spotify-heavy features "
                "you run back-to-back."
            )
        return

    # Add tracks to the playlist in batches of up to 100
    try:
        for i in range(0, len(track_uris), 100):
            batch = track_uris[i:i+100]
            spotify_client.playlist_add_items(playlist['id'], batch)
        print(f"Added {len(track_uris)} tracks to '{playlist_name}'.")
        update_run_report(spotify_playlist_status="created", spotify_playlist_name=playlist_name)
    except spotipy.exceptions.SpotifyException as e:
        abort_if_unreasonable_rate_limit_error(e)
        print(f"Error adding tracks to playlist: {e}")
        update_run_report(
            spotify_playlist_status="failed: could not add tracks",
            spotify_playlist_name=playlist_name,
            errors=f"Spotify playlist add failed: {e}",
        )
        if getattr(e, "http_status", None) == 429:
            print(
                "Spotify rate-limited the playlist add step even after retries. "
                "Try again after a short pause."
            )

def calculate_max_tracks_per_playlist(max_playlists, max_songs):
    """
    Dynamically calculate the max number of tracks per playlist depending on the number of playlists.
    As max_playlists increases, max_tracks_per_playlist decreases, but a minimum is enforced.
    """
    # Set a lower bound for the minimum number of tracks per playlist
    min_tracks_per_playlist = 5
    
    # Dynamically adjust the number of tracks per playlist
    max_tracks_per_playlist = max(max_songs // max_playlists, min_tracks_per_playlist)
    
    return max_tracks_per_playlist


def prompt_for_optional_positive_number(prompt_text):
    while True:
        raw_value = input(prompt_text).strip().lower()
        if raw_value in {"", "none", "no", "n"}:
            return None

        try:
            parsed_value = int(raw_value)
        except ValueError:
            print("Invalid input. Enter a positive number or press Enter for no limit.")
            continue

        if parsed_value > 0:
            return parsed_value

        print("Please enter a positive number or press Enter for no limit.")


def prompt_for_playlist_size_range():
    while True:
        min_playlist_size = prompt_for_optional_positive_number(
            "Min songs per playlist? (Enter to skip): "
        )
        max_playlist_size = prompt_for_optional_positive_number(
            "Max songs per playlist? (Enter to skip): "
        )

        if (
            min_playlist_size is not None
            and max_playlist_size is not None
            and min_playlist_size > max_playlist_size
        ):
            print("Minimum playlist size cannot be greater than maximum playlist size.")
            continue

        return min_playlist_size, max_playlist_size


def collect_song_link_rows(song):
    link_rows = []
    if song.get('spotify_requested'):
        if song.get('spotify_url'):
            label = "direct track" if song.get('spotify_found_exact_track') else "search page"
            link_rows.append((f"Spotify ({label})", song['spotify_url']))

    if song.get('youtube_requested') and song.get('youtube_url'):
        label = "direct video" if song.get('youtube_found_exact_video') else "search results"
        link_rows.append((f"YouTube ({label})", song['youtube_url']))

    if len(link_rows) == 1:
        return [("URL", link_rows[0][1])]
    return link_rows


def wrap_box_row(label, value):
    prefix = f"{label + ':':<9} "
    value_width = max(20, BOX_CONTENT_WIDTH - visible_len(prefix))
    wrapped_values = textwrap.wrap(
        str(value) if value else "",
        width=value_width,
        break_long_words=True,
        break_on_hyphens=False,
    ) or [""]
    rows = [f"{prefix}{wrapped_values[0]}"]
    rows.extend(f"{' ' * len(prefix)}{line}" for line in wrapped_values[1:])
    return rows


def build_song_card_lines(index, song):
    rows = [
        ("Title", song.get('title') or "Unknown Title"),
        ("Artist", song.get('artists') or "Unknown Artist"),
    ]
    rows.extend(collect_song_link_rows(song))

    header = f"SONG {index} "
    first_label, first_value = rows[0]
    first_lines = wrap_box_row(first_label, first_value)
    lines = [header + "-" * max(3, CLI_WIDTH - visible_len(header))]
    lines.append(f"  {first_lines[0]}")
    for continuation_line in first_lines[1:]:
        lines.append(f"  {continuation_line}")

    for label, value in rows[1:]:
        for wrapped_line in wrap_box_row(label, value):
            lines.append(f"  {wrapped_line}")

    return lines


def print_song_card(index, song):
    print()
    for line_index, line in enumerate(build_song_card_lines(index, song)):
        if line_index == 0:
            print(color_text(line, ANSI_CYAN))
        elif any(label in line for label in ("Title:", "Artist:", "URL:", "Spotify", "YouTube")):
            print(color_text(line, ANSI_BOLD))
        else:
            print(line)


def write_song_card_to_text_file(file_handle, index, song):
    file_handle.write("\n")
    file_handle.write("\n".join(build_song_card_lines(index, song)))
    file_handle.write("\n")


def build_song_links_html(song):
    links = []

    if song.get('spotify_requested'):
        if song.get('spotify_url'):
            label = "Open in Spotify" if song.get('spotify_found_exact_track') else "Search on Spotify"
            links.append(f"""<a href="{song['spotify_url']}" target="_blank" rel="noopener noreferrer">{label}</a>""")

    if song.get('youtube_requested') and song.get('youtube_url'):
        label = "Watch on YouTube" if song.get('youtube_found_exact_video') else "Search on YouTube"
        links.append(f"""<a href="{song['youtube_url']}" target="_blank" rel="noopener noreferrer">{label}</a>""")

    return " | ".join(links)


def prompt_for_optional_positive_int(prompt_text, default_value):
    while True:
        raw_value = input(f"{prompt_text} [{default_value}]: ").strip()
        if not raw_value:
            return default_value

        try:
            parsed_value = int(raw_value)
        except ValueError:
            print("Invalid input. Please enter a positive number or press Enter for the default.")
            continue

        if parsed_value > 0:
            return parsed_value

        print("Please enter a positive number.")


def prompt_for_num_songs():
    while True:
        raw = input("How many songs? [1]: ").strip()
        if not raw or raw.lower() == 'one':
            return 1
        try:
            n = int(raw)
        except ValueError:
            print("Enter a number.")
            continue
        if n < 1:
            print("Enter a positive number.")
            continue
        return n


def prompt_for_source_choice():
    while True:
        print_cli_menu(
            "Source",
            [
                ("1", "My Spotify playlists"),
                ("2", "Public discovery"),
                ("3", "Surprise me"),
            ],
        )
        source_choice = input("Enter 1, 2, or 3: ").strip()
        if source_choice in {'1', '2', '3'}:
            return source_choice
        print("Invalid choice.")


def prompt_for_avoid_optional_spotify_api():
    return prompt_yes_no(
        "Avoid optional Spotify API calls for this run? (yes/no) [yes]: ",
        default="yes",
    )


def ensure_spotify_link_access(link_platform):
    global AVOID_OPTIONAL_SPOTIFY_API

    if link_platform not in {'spotify', 'both'}:
        return link_platform

    if not optional_spotify_api_allowed():
        print("Spotify links need optional Spotify API calls.")
        allow_spotify_api = prompt_yes_no(
            "Enable optional Spotify API calls for Spotify links? (yes/no) [yes]: ",
            default="yes",
        )
        if allow_spotify_api:
            AVOID_OPTIONAL_SPOTIFY_API = False
            update_run_report(avoid_optional_spotify_api=False)
        elif link_platform == 'both':
            print("Continuing with YouTube links only.")
            return 'youtube'
        else:
            print("Skipping Spotify link lookup.")
            return None

    if not spotify_app_credentials_configured():
        print("Spotify links need Spotify app credentials.")
        offer_spotify_credentials_in_terminal(purpose="exact Spotify links")
        if not spotify_app_credentials_configured():
            if link_platform == 'both':
                print("Continuing with YouTube links only.")
                return 'youtube'
            print("Skipping Spotify link lookup.")
            return None

    return link_platform


def prompt_for_playlist_scope():
    while True:
        print_cli_menu(
            "Playlist selection",
            [
                ("1", "All playlists"),
                ("2", "Filter by name"),
                ("3", "Own playlists only"),
                ("4", "Random selection"),
            ],
        )
        scope = input("Enter 1, 2, 3, or 4: ").strip()
        if scope in {'1', '2', '3', '4'}:
            return scope
        print("Invalid choice.")


def prompt_for_max_playlists():
    while True:
        raw = input("Max playlists to search [10]: ").strip()
        if not raw:
            return 10
        try:
            n = int(raw)
        except ValueError:
            print("Enter a positive number.")
            continue
        if n > 0:
            return n
        print("Enter a positive number.")


def prompt_for_max_songs_pool():
    return prompt_for_optional_positive_number(
        "Max songs to collect before selecting? (Enter to skip): "
    )


def prompt_for_keywords():
    while True:
        keywords_input = input('Enter one or more words or phrases, like "happy", "yacht rock", or "summer jazz": ').strip()
        keywords = parse_keywords(keywords_input)
        if keywords:
            return keywords
        print("Please enter at least one keyword or phrase.")


def prompt_for_surprise_mode():
    while True:
        print_cli_menu(
            "Surprise Me mode",
            [
                ("1", "Random emotions/genres via public discovery"),
                ("2", "random-song.com with its default random configuration"),
                ("3", "random-song.com with custom configuration"),
            ],
        )
        surprise_mode = input("Enter 1, 2, or 3: ").strip()
        if surprise_mode in {'1', '2', '3'}:
            return surprise_mode
        print("Invalid choice.")


def prompt_for_output_format():
    while True:
        file_format = input("Output format (txt / html / terminal): ").strip().lower()
        if file_format in {'txt', 'html', 'terminal'}:
            return file_format
        if file_format == 'n/a':
            return file_format
        print("Enter 'txt', 'html', or 'terminal'.")


def prompt_yes_no(prompt_text, default=None):
    while True:
        raw_value = input(prompt_text).strip().lower()
        if not raw_value and default in {'yes', 'no'}:
            return default == 'yes'
        if raw_value in {'yes', 'y'}:
            return True
        if raw_value in {'no', 'n'}:
            return False
        print("Please answer yes or no.")

def load_emotions_genres(file_path):
    """Load the emotions/genres list used by Surprise Me from a plain-text file."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"Warning: emotions/genres file '{file_path}' not found. Surprise Me will have no keyword pool.")
        return []


EMOTIONS_GENRES_FILE = 'emotions_genres.txt'
EMOTIONS_GENRES = load_emotions_genres(EMOTIONS_GENRES_FILE)
if EMOTIONS_GENRES:
    print(f"Loaded {len(EMOTIONS_GENRES)} emotions/genres from file.")


def print_project_summary():
    print_cli_section("Playlist Maker")
    print("Randomly selects songs from your playlists or public discovery.")


def select_random_songs(song_list, requested_count):
    if not song_list:
        return []

    if requested_count > len(song_list):
        print(f"Only found {len(song_list)} songs. Returning all of them.")
        requested_count = len(song_list)

    return random.sample(song_list, requested_count)


def fetch_public_discovery_with_auto_fallback(
    keywords,
    max_playlists,
    max_songs,
    max_tracks_per_playlist,
    min_playlist_size,
    max_playlist_size,
    discovery_mode,
    ):
    effective_mode = discovery_mode
    try:
        song_list = fetch_songs_from_public_playlists_by_keywords(
            keywords,
            max_playlists=max_playlists,
            max_songs=max_songs,
            max_tracks_per_playlist=max_tracks_per_playlist,
            min_playlist_size=min_playlist_size,
            max_playlist_size=max_playlist_size,
            discovery_mode=discovery_mode,
        )
        return song_list, effective_mode
    except (RunAborted, SpotifyApiUnavailableError) as error:
        if discovery_mode == DEFAULT_PUBLIC_DISCOVERY_MODE:
            raise

        print(f"Spotify became unavailable during {describe_public_discovery_mode(discovery_mode)}: {error}")
        print("Switching this run to YouTube Music discovery.")
        effective_mode = DEFAULT_PUBLIC_DISCOVERY_MODE
        song_list = fetch_songs_from_public_playlists_by_keywords(
            keywords,
            max_playlists=max_playlists,
            max_songs=max_songs,
            max_tracks_per_playlist=max_tracks_per_playlist,
            min_playlist_size=min_playlist_size,
            max_playlist_size=max_playlist_size,
            discovery_mode=effective_mode,
        )
        return song_list, effective_mode


def random_surprise_keywords(keyword_count=3):
    if not EMOTIONS_GENRES:
        return []
    return random.sample(EMOTIONS_GENRES, min(keyword_count, len(EMOTIONS_GENRES)))


def fetch_surprise_public_discovery_with_retries(
    max_playlists,
    max_songs,
    max_tracks_per_playlist,
    min_playlist_size,
    max_playlist_size,
    discovery_mode,
):
    """Repeatedly draw random keyword sets and run tiered discovery until songs are found.

    Each attempt uses fetch_surprise_discovery_tiered(), which tries Spotify
    genre recommendations first (Tier 1) before falling back to YouTube Music
    (Tier 2).  If an attempt returns no songs the loop picks a fresh set of
    random keywords and retries, up to SURPRISE_PUBLIC_DISCOVERY_MAX_ATTEMPTS.
    """
    attempted_keywords = []
    effective_mode = discovery_mode

    for attempt_number in range(1, SURPRISE_PUBLIC_DISCOVERY_MAX_ATTEMPTS + 1):
        keywords = random_surprise_keywords(3)
        if not keywords:
            print("No emotions/genres available for Surprise Me.")
            break

        attempted_keywords.append(keywords)
        update_run_report(surprise_attempts=[" + ".join(keywords)])
        print(f"Randomly selected emotions/genres (attempt {attempt_number}): {', '.join(keywords)}")
        emit_web_status(
            "search",
            f"Trying Surprise Me keywords {attempt_number}/{SURPRISE_PUBLIC_DISCOVERY_MAX_ATTEMPTS}: {', '.join(keywords)}",
            reset=attempt_number == 1,
            report={"keywords": keywords, "surprise_attempts": [" + ".join(keywords)]},
        )

        song_list, effective_mode = fetch_surprise_discovery_tiered(
            keywords,
            max_songs=max_songs,
            max_playlists=max_playlists,
            max_tracks_per_playlist=max_tracks_per_playlist,
            min_playlist_size=min_playlist_size,
            max_playlist_size=max_playlist_size,
        )
        if song_list:
            return song_list, effective_mode, keywords, attempted_keywords

        print("No songs found for that Surprise Me keyword set. Trying another set...")

    attempted_summary = [" + ".join(kw) for kw in attempted_keywords]
    update_run_report(
        errors=(
            "No tracks found after trying these Surprise Me keyword sets: "
            + "; ".join(attempted_summary)
        ),
        surprise_attempts=attempted_summary,
    )
    return [], effective_mode, attempted_keywords[-1] if attempted_keywords else [], attempted_keywords


def build_no_spotify_surprise_fallback_source_description(keywords):
    return f"Fallback Surprise Me (YouTube Music discovery for {', '.join(keywords)})"


def maybe_switch_personal_source_to_no_spotify_fallback(num_songs, failure_reason):
    print(f"Spotify became unavailable for this source: {failure_reason}")
    wants_fallback = prompt_yes_no(
        "Do you want to switch to a no-Spotify-API Surprise Me fallback instead? (yes/no): "
    )
    if not wants_fallback:
        return None

    max_playlists = 10
    max_songs = 20 * max_playlists
    max_tracks_per_playlist = calculate_max_tracks_per_playlist(max_playlists, max_songs)
    fallback_keywords = random.sample(EMOTIONS_GENRES, 3)
    print("Switching to the YouTube Music fallback with random emotions/genres.")
    print(f"Random fallback emotions/genres: {', '.join(fallback_keywords)}")

    song_list = fetch_songs_from_public_playlists_by_keywords(
        fallback_keywords,
        max_playlists=max_playlists,
        max_songs=max_songs,
        max_tracks_per_playlist=max_tracks_per_playlist,
        min_playlist_size=None,
        max_playlist_size=None,
        discovery_mode=DEFAULT_PUBLIC_DISCOVERY_MODE,
    )
    if not song_list:
        print("The no-Spotify-API fallback did not find any songs.")
        return None

    selected_songs = select_random_songs(song_list, num_songs)
    if not selected_songs:
        print("The no-Spotify-API fallback did not find any songs.")
        return None

    return {
        'selected_songs': selected_songs,
        'selected_playlists': [],
        'source_description': build_no_spotify_surprise_fallback_source_description(fallback_keywords),
        'source_choice': '6',
    }


def main():
    global AVOID_OPTIONAL_SPOTIFY_API

    reset_run_report()
    print_project_summary()

    num_songs = prompt_for_num_songs()
    update_run_report(requested_count=num_songs)

    top_choice = prompt_for_source_choice()
    if top_choice in {'2', '3'}:
        AVOID_OPTIONAL_SPOTIFY_API = prompt_for_avoid_optional_spotify_api()
        update_run_report(avoid_optional_spotify_api=AVOID_OPTIONAL_SPOTIFY_API)

    if top_choice == '1':
        source_choice = prompt_for_playlist_scope()
    elif top_choice == '2':
        source_choice = '5'
    else:  # top_choice == '3'
        source_choice = '6'

    # Default values for max_playlists and max_tracks_per_playlist
    max_playlists = None
    max_tracks_per_playlist = None
    max_songs = None
    min_playlist_size = None
    max_playlist_size = None
    keywords = None
    surprise_song_list = None
    public_discovery_mode = DEFAULT_PUBLIC_DISCOVERY_MODE
    surprise_public_discovery = False

    if source_choice == '1':
        playlist_visibility_filter = prompt_for_visibility_filter()
        cache_file, fetch_and_cache_songs, source_description = decorate_source_for_visibility(
            'song_cache_all.json',
            fetch_and_cache_all_songs,
            'All Playlists',
            playlist_visibility_filter,
        )
    elif source_choice == '2':
        playlist_visibility_filter = prompt_for_visibility_filter()
        filter_config = prompt_for_playlist_name_filter(playlist_visibility_filter)
        if not filter_config:
            return
        cache_file = add_visibility_to_cache_file(filter_config['cache_file'], playlist_visibility_filter)
        fetch_and_cache_songs = filter_config['fetcher']
        source_description = (
            f"{filter_config['description']} ({describe_visibility_filter(playlist_visibility_filter)})"
            if playlist_visibility_filter != 'any'
            else filter_config['description']
        )
    elif source_choice == '3':
        playlist_visibility_filter = prompt_for_visibility_filter()
        cache_file, fetch_and_cache_songs, source_description = decorate_source_for_visibility(
            'song_cache_user_playlists.json',
            fetch_and_cache_user_playlists_songs,
            'Your Own Playlists',
            playlist_visibility_filter,
        )
    elif source_choice == '4':
        playlist_visibility_filter = prompt_for_visibility_filter()
        cache_file = add_visibility_to_cache_file('song_cache_random_playlists.json', playlist_visibility_filter)
        fetch_and_cache_songs = lambda selected_cache_file: fetch_and_cache_random_playlists_songs(
            selected_cache_file,
            num_songs,
            visibility_filter=playlist_visibility_filter,
        )
        source_description = (
            f"Random Playlists ({describe_visibility_filter(playlist_visibility_filter)})"
            if playlist_visibility_filter != 'any'
            else 'Random Playlists'
        )
    elif source_choice == '5' or source_choice == '6':
        if source_choice == '5':
            max_playlists = prompt_for_max_playlists()
            min_playlist_size, max_playlist_size = prompt_for_playlist_size_range()
            user_max_songs = prompt_for_max_songs_pool()
            max_songs = user_max_songs if user_max_songs is not None else 20 * max_playlists
            max_tracks_per_playlist = calculate_max_tracks_per_playlist(max_playlists, max_songs)

            # For option 5, we won't use cache
            cache_file = None
            source_description = "Public Discovery"
            keywords = prompt_for_keywords()
            update_run_report(
                source=source_description,
                keywords=keywords,
                max_playlists=max_playlists,
                min_playlist_size=min_playlist_size,
                max_playlist_size=max_playlist_size,
            )
        elif source_choice == '6':
            cache_file = None
            surprise_mode = prompt_for_surprise_mode()

            if surprise_mode == '1':
                max_playlists = prompt_for_max_playlists()
                min_playlist_size, max_playlist_size = prompt_for_playlist_size_range()
                user_max_songs = prompt_for_max_songs_pool()
                max_songs = user_max_songs if user_max_songs is not None else 20 * max_playlists
                max_tracks_per_playlist = calculate_max_tracks_per_playlist(max_playlists, max_songs)

                surprise_public_discovery = True
                source_description = f"Surprise Me (Random Emotions/Genres via {describe_public_discovery_mode(public_discovery_mode)})"
                update_run_report(
                    source=source_description,
                    max_playlists=max_playlists,
                    min_playlist_size=min_playlist_size,
                    max_playlist_size=max_playlist_size,
                )
            elif surprise_mode == '2':
                source_description = 'Surprise Me (random-song.com Default Random Config)'
                update_run_report(source=source_description)
                surprise_song_list = fetch_songs_from_random_song_generator(num_songs, mode="default-config")
            elif surprise_mode == '3':
                options = fetch_random_song_generator_options()
                custom_config = prompt_for_random_song_generator_config(options)
                source_description = 'Surprise Me (random-song.com Custom Config)'
                update_run_report(source=source_description, random_song_config=custom_config)
                surprise_song_list = fetch_songs_from_random_song_generator(num_songs, mode="custom-config", config=custom_config)

    if source_choice == '5' or surprise_public_discovery:
        # Handle option 5 and 6 without caching
        if surprise_public_discovery:
            song_list, effective_discovery_mode, keywords, attempted_keywords = fetch_surprise_public_discovery_with_retries(
                max_playlists=max_playlists,
                max_songs=max_songs,
                max_tracks_per_playlist=max_tracks_per_playlist,
                min_playlist_size=min_playlist_size,
                max_playlist_size=max_playlist_size,
                discovery_mode=public_discovery_mode,
            )
            update_run_report(keywords=keywords)
        else:
            song_list, effective_discovery_mode = fetch_public_discovery_with_auto_fallback(
                keywords,
                max_playlists=max_playlists,
                max_songs=max_songs,
                max_tracks_per_playlist=max_tracks_per_playlist,
                min_playlist_size=min_playlist_size,
                max_playlist_size=max_playlist_size,
                discovery_mode=public_discovery_mode,
            )
        if not song_list:
            print("No tracks found.")
            update_run_report(errors="No tracks found.")
            return
        selected_songs = select_random_songs(song_list, num_songs)
        update_run_report(selected_count=len(selected_songs), source=source_description, keywords=keywords)
        emit_web_status("select", f"Selected {len(selected_songs)} song(s).", reset=True, done=True)
        if effective_discovery_mode != public_discovery_mode:
            public_discovery_mode = effective_discovery_mode
            if source_choice == '5':
                source_description = (
                    "Public Discovery"
                )
            else:
                source_description = (
                    f"Surprise Me (Random Emotions/Genres via "
                    f"{describe_public_discovery_mode(public_discovery_mode)})"
                )
        selected_playlists = []
    elif source_choice == '6' and surprise_song_list is not None:
        if not surprise_song_list:
            print("No tracks found.")
            return
        selected_songs = select_random_songs(surprise_song_list, num_songs)
        update_run_report(selected_count=len(selected_songs), source=source_description)
        emit_web_status("select", f"Selected {len(selected_songs)} song(s).", reset=True, done=True)
        selected_playlists = []
    else:
        # Check if cache file exists
        if os.path.exists(cache_file):
            update_choice = input("Do you want to check for updates? (yes/no): ").strip().lower()
            if update_choice == 'yes':
                try:
                    os.remove(cache_file)
                except Exception as e:
                    print(f"Failed to delete cache file '{cache_file}': {e}")
                    return

                print("Fetching songs and updating cache...")
                try:
                    if source_choice == '4':
                        song_list, selected_playlists = fetch_and_cache_songs(cache_file)
                    else:
                        song_list = fetch_and_cache_songs(cache_file)
                        selected_playlists = []
                except (RunAborted, SpotifyApiUnavailableError) as error:
                    fallback_result = maybe_switch_personal_source_to_no_spotify_fallback(num_songs, error)
                    if not fallback_result:
                        print("Ending this run.")
                        return
                    selected_songs = fallback_result['selected_songs']
                    selected_playlists = fallback_result['selected_playlists']
                    source_description = fallback_result['source_description']
                    source_choice = fallback_result['source_choice']
                    song_list = None
            else:
                print("Loading songs from cache...")
                try:
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        cache_data = json.load(f)
                        if source_choice == '4':
                            song_list = cache_data.get('songs', [])
                            selected_playlists = cache_data.get('playlists', [])
                        else:
                            song_list = cache_data
                            selected_playlists = []
                    print(f"Loaded {len(song_list)} songs from cache.")
                except Exception as e:
                    print(f"Failed to load cache file '{cache_file}': {e}")
                    return
        else:
            print("Fetching songs and creating cache...")
            try:
                if source_choice == '4':
                    song_list, selected_playlists = fetch_and_cache_songs(cache_file)
                else:
                    song_list = fetch_and_cache_songs(cache_file)
                    selected_playlists = []
            except (RunAborted, SpotifyApiUnavailableError) as error:
                fallback_result = maybe_switch_personal_source_to_no_spotify_fallback(num_songs, error)
                if not fallback_result:
                    print("Ending this run.")
                    return
                selected_songs = fallback_result['selected_songs']
                selected_playlists = fallback_result['selected_playlists']
                source_description = fallback_result['source_description']
                source_choice = fallback_result['source_choice']
                song_list = None

        if song_list is not None:
            if not song_list:
                print("No tracks found.")
                update_run_report(errors="No tracks found.")
                return

            selected_songs = select_random_songs(song_list, num_songs)
            if not selected_songs:
                print("No tracks found.")
                update_run_report(errors="No tracks found.")
                return
            print(f"Selected {len(selected_songs)} songs.")
            update_run_report(selected_count=len(selected_songs), source=source_description)
            emit_web_status("select", f"Selected {len(selected_songs)} song(s).", reset=True, done=True)

    num_songs = len(selected_songs)
    link_platform = prompt_for_link_platform()
    link_platform = ensure_spotify_link_access(link_platform)
    if link_platform:
        attach_platform_links(selected_songs, link_platform)
    else:
        update_run_report(links_added=False)

    # Ask for file format or terminal output
    file_format = prompt_for_output_format()
    update_run_report(output_format=file_format)

    if file_format == 'terminal':
        emit_web_status("output", "Printing songs in the browser summary...", reset=True)
        # Display songs directly in the terminal
        print_cli_section("Selected Songs")
        print(f"Source: {source_description}")
        for idx, song in enumerate(selected_songs, start=1):
            print_song_card(idx, song)
        print_cli_rule("-")
        if source_choice == '4':
            print(f"\nSelected from playlists: {', '.join(selected_playlists)}")

    elif file_format == 'n/a':
        print("Skipping file generation.")
    elif file_format in ['txt', 'html']:
        emit_web_status("output", f"Writing {file_format.upper()} file...", reset=True)
        now = datetime.now()
        date_str = now.strftime("%Y.%m.%d at %Hhr%M")
        filename_date = now.strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"playlist-maker-{filename_date}.{file_format}"

        # Create 'generated' folder if it doesn't exist
        output_folder = 'generated'
        if not os.path.exists(output_folder):
            try:
                os.makedirs(output_folder)
                print(f"Created folder '{output_folder}'.")
            except Exception as e:
                print(f"Failed to create folder '{output_folder}': {e}")
                return

        # Full path to the output file
        filepath = os.path.join(output_folder, filename)
        abs_filepath = os.path.abspath(filepath)
        print(f"Output file will be: {abs_filepath}")

        if file_format == 'txt':
            # Generate text file
            try:
                with open(filepath, 'w', encoding='utf-8-sig') as f:
                    f.write(f"Your Selected Songs from {source_description} (Generated on {date_str}):\n\n")
                    if source_choice == '4':
                        f.write(f"Selected from playlists:\n")
                        for plist in selected_playlists:
                            f.write(f"- {plist}\n")
                        f.write("\n")
                    for idx, song in enumerate(selected_songs, start=1):
                        write_song_card_to_text_file(f, idx, song)
                    f.write("\n")
                print(f"\n{num_songs} songs have been written to '{abs_filepath}'.\n")
                update_run_report(
                    artifact_path=abs_filepath,
                    artifact_name=filename,
                    artifact_kind="txt",
                )
                emit_web_status("output", f'TXT file "{filename}" was generated.', done=True)
            except Exception as e:
                print(f"Failed to write to file '{abs_filepath}': {e}")
                update_run_report(errors=f"TXT file generation failed: {e}")
                emit_web_status("output", f"Could not write text file: {e}", level="error")

            # Ask if user wants to open the file after .txt generation
            open_choice = input("Do you want to open the file now? (yes/no): ").strip().lower()
            if open_choice == 'yes':
                try:
                    if os.name == 'nt':  # For Windows
                        os.startfile(filepath)
                    elif os.name == 'posix':  # For macOS/Linux
                        if sys.platform == 'darwin':  # macOS
                            os.system(f'open "{filepath}"')
                        else:  # Linux
                            os.system(f'xdg-open "{filepath}"')
                    else:
                        print("Unable to open the file automatically on this operating system.")
                except Exception as e:
                    print(f"Failed to open the file: {e}")

        else:
            # Generate HTML file
            try:
                readable_date = now.strftime("%B %d, %Y at %I:%M %p")
                html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Playlist Maker - {readable_date}</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            background-color: #f7f7f7;
            color: #333;
            padding: 20px;
        }}
        h1 {{
            text-align: center;
            color: #4CAF50;
        }}
        p.date, p.source, p.playlists {{
            text-align: center;
            color: #555;
        }}
        ol {{
            font-size: 18px;
            line-height: 1.6;
            margin-top: 20px;
        }}
        li {{
            margin-bottom: 10px;
        }}
        li::marker {{
            font-weight: bold;
        }}
    </style>
</head>
<body>
    <h1>Playlist Maker</h1>
    <p class="source">Source: {html.escape(source_description)}</p>
"""
                if source_choice == '4':
                    playlists_escaped = ', '.join(html.escape(p) for p in selected_playlists)
                    html_content += f'    <p class="playlists">Selected from playlists: {playlists_escaped}</p>\n'
                html_content += f"""    <p class="date">Generated on {readable_date}</p>
    <ol>
"""

                for song in selected_songs:
                    song_title = html.escape(song['title'])
                    song_artists = html.escape(song['artists'])
                    html_content += f"        <li>{song_title} by {song_artists}"
                    html_links = build_song_links_html(song)
                    if html_links:
                        html_content += f"<br>{html_links}"
                    html_content += "</li>\n"

                html_content += """    </ol>
</body>
</html>"""

                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(html_content)
                print(f"\n{num_songs} songs have been written to '{abs_filepath}'.\n")
                update_run_report(
                    artifact_path=abs_filepath,
                    artifact_name=filename,
                    artifact_kind="html",
                )
                emit_web_status("output", f'HTML file "{filename}" was generated.', done=True)
            except Exception as e:
                print(f"Failed to write to HTML file '{abs_filepath}': {e}")
                update_run_report(errors=f"HTML file generation failed: {e}")
                emit_web_status("output", f"Could not write HTML file: {e}", level="error")

            # Ask if user wants to open the file after .html generation
            open_choice = input("Do you want to open the file now? (yes/no): ").strip().lower()
            if open_choice == 'yes':
                try:
                    if os.name == 'nt':  # For Windows
                        os.startfile(filepath)
                    elif os.name == 'posix':  # For macOS/Linux
                        if sys.platform == 'darwin':  # macOS
                            os.system(f'open "{filepath}"')
                        else:  # Linux
                            os.system(f'xdg-open "{filepath}"')
                    else:
                        print("Unable to open the file automatically on this operating system.")
                except Exception as e:
                    print(f"Failed to open the file: {e}")

    # Ask the user if they want to create a Spotify playlist
    create_playlist_choice = input("Do you want to create a Spotify playlist with these songs? (yes/no): ").strip().lower()
    if create_playlist_choice == 'yes':
        try:
            create_spotify_playlist(selected_songs)
        except (RunAborted, SpotifyApiUnavailableError) as error:
            print(f"Skipping Spotify playlist creation because Spotify is unavailable: {error}")
            update_run_report(
                spotify_playlist_status="failed: Spotify unavailable",
                errors=f"Spotify playlist creation skipped: {error}",
            )
    else:
        update_run_report(spotify_playlist_status="skipped by user")


if __name__ == "__main__":
    try:
        main()
    except RunAborted as error:
        print(error)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nRun cancelled.")
        sys.exit(130)
