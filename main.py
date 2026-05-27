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
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

import spotipy
import spotipy.exceptions
from spotipy.cache_handler import CacheHandler
from spotipy.oauth2 import SpotifyOAuth
from tqdm import tqdm

logging.getLogger("spotipy").setLevel(logging.CRITICAL)


APP_NAME = "spotify-scripts"
APP_DIR = Path.home() / f".{APP_NAME}"
CONFIG_PATH = APP_DIR / "config.json"
TOKEN_CACHE_PATH = APP_DIR / "token_cache.json"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8080/callback"
SCOPE = "user-read-private playlist-read-private playlist-modify-private playlist-modify-public"
RANDOM_SONG_API_BASE_URL = "https://europe-west1-randommusicgenerator-34646.cloudfunctions.net/appV2"
REQUIRED_SCOPE_SET = set(SCOPE.split())
DEFAULT_PUBLIC_DISCOVERY_MODE = "hybrid"
YOUTUBE_SEARCH_RESULT_LIMIT = 20
SPOTIFY_MATCH_CACHE = {}
SPOTIFY_MIN_INTERVAL_SECONDS = 0.2
SPOTIFY_MAX_RETRIES = 4
SPOTIFY_MAX_AUTO_RETRY_AFTER_SECONDS = 120
CURRENT_USER_PROFILE = None
SPOTIFY_CLIENT = None
SPOTIFY_API_UNAVAILABLE_REASON = None
SPOTIFY_UNAVAILABLE_NOTICES = set()


class RunAborted(RuntimeError):
    """Raised when the current run should stop cleanly."""


class SpotifyApiUnavailableError(RuntimeError):
    """Raised when Spotify API access is unavailable for this run."""


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

    if secret:
        return getpass.getpass(f"{prompt_text}: ").strip()

    return input(f"{prompt_text}: ").strip()


def prompt_for_visibility_filter():
    while True:
        print("Which playlist visibility should be used?")
        print("1. Any visibility")
        print("2. Public playlists only")
        print("3. Private playlists only")
        visibility_choice = input("Enter 1, 2, or 3 [1]: ").strip() or '1'

        if visibility_choice == '1':
            return 'any'
        if visibility_choice == '2':
            return 'public'
        if visibility_choice == '3':
            return 'private'

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


def create_spotify_client(prompt_if_missing=True):
    global CURRENT_USER_PROFILE
    print("Spotify credentials are read from environment variables when available.")
    print("If they are not set, this script will use saved local app settings when available or ask for them.")

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


def spotify_api_is_available(context="this step"):
    return get_spotify_client(required=False, prompt_if_missing=False, context=context) is not None


class LazySpotifyClient:
    def __getattr__(self, name):
        client = get_spotify_client(required=True, prompt_if_missing=True, context="Spotify API access")
        return getattr(client, name)


sp = LazySpotifyClient()


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
        print("Choose how to match playlist names:")
        print("1. Rediscover preset")
        print("2. Playlist name contains text")
        print("3. Custom regex")
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
                search_text = input("Enter text to look for in playlist names: ").strip()
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
                regex_input = input("Enter a regex for playlist names: ").strip()
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


def fetch_text(url, extra_headers=None):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if extra_headers:
        headers.update(extra_headers)

    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", errors="ignore")


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
    print("Random-song.com configuration:")
    print("Press Enter to keep an option random.")

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
            progress_bar.update(1)

    if len(song_list) < num_songs:
        print(f"random-song.com returned {len(song_list)} unique song(s) after {total_attempts} attempt(s).")

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


def find_spotify_url(song):
    if song.get('spotify_url'):
        return song.get('spotify_url'), song.get('spotify_found_exact_track', True)

    return build_spotify_search_url(song), False


def find_spotify_track_for_song(song):
    if song.get('spotify_uri'):
        return song['spotify_uri']

    if song.get('spotify_url'):
        spotify_url = song['spotify_url']
        track_id = spotify_url.rstrip('/').split('/')[-1].split('?')[0]
        if track_id:
            return f"spotify:track:{track_id}"

    spotify_match = resolve_song_on_spotify(song)
    if not spotify_match:
        return None

    return spotify_match.get('spotify_uri')


def attach_platform_links(selected_songs, link_platform):
    if link_platform in {'spotify', 'both'}:
        print("Building Spotify links/search pages for the selected songs...")
        for song in tqdm(selected_songs, desc='Finding Spotify links', unit='song'):
            spotify_url, found_exact_track = find_spotify_url(song)
            song['spotify_url'] = spotify_url
            song['spotify_found_exact_track'] = found_exact_track

    if link_platform in {'youtube', 'both'}:
        print("Looking up YouTube links for the selected songs...")
        for song in tqdm(selected_songs, desc='Finding YouTube links', unit='song'):
            youtube_url, found_exact_video = find_youtube_url(song)
            song['youtube_url'] = youtube_url
            song['youtube_found_exact_video'] = found_exact_video


def maybe_open_platform_links(selected_songs, link_platform):
    if not link_platform:
        return

    platform_label = {
        'spotify': 'Spotify',
        'youtube': 'YouTube',
        'both': 'Spotify/YouTube',
    }[link_platform]
    open_choice = input(f"Do you want to open {platform_label} link(s) now? (yes/no): ").strip().lower()
    if open_choice != 'yes':
        return

    if link_platform == 'both':
        preferred_platform = input("Which platform should be opened first? (Enter 'spotify' or 'youtube'): ").strip().lower()
        if preferred_platform not in {'spotify', 'youtube'}:
            preferred_platform = 'spotify'
        secondary_platform = 'youtube' if preferred_platform == 'spotify' else 'spotify'
    else:
        preferred_platform = link_platform
        secondary_platform = None

    def get_song_url(song):
        if preferred_platform == 'spotify' and song.get('spotify_url'):
            return song.get('spotify_url')
        if preferred_platform == 'youtube' and song.get('youtube_url'):
            return song.get('youtube_url')
        if secondary_platform == 'spotify' and song.get('spotify_url'):
            return song.get('spotify_url')
        if secondary_platform == 'youtube' and song.get('youtube_url'):
            return song.get('youtube_url')
        return None

    if len(selected_songs) == 1:
        urls_to_open = [get_song_url(selected_songs[0])]
    else:
        open_all_choice = input("Open all song links? (yes/no): ").strip().lower()
        if open_all_choice == 'yes':
            urls_to_open = [get_song_url(song) for song in selected_songs]
        else:
            urls_to_open = [get_song_url(selected_songs[0])]

    opened_count = 0
    for url in urls_to_open:
        if url:
            webbrowser.open(url)
            opened_count += 1

    print(f"Opened {opened_count} link(s) in your browser.")


def build_keyword_combinations(keywords):
    keyword_combinations = []
    seen_combinations = set()
    for i in range(len(keywords)):
        for j in range(i, len(keywords)):
            if i != j:
                combined_keywords = f"{keywords[i]} {keywords[j]}"
                if combined_keywords not in seen_combinations:
                    keyword_combinations.append(combined_keywords)
                    seen_combinations.add(combined_keywords)
            if keywords[i] not in seen_combinations:
                keyword_combinations.append(keywords[i])
                seen_combinations.add(keywords[i])
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


def search_youtube_playlist_ids_by_keywords(keywords, max_playlists, candidate_multiplier=5):
    playlist_ids = []
    target_count = max(max_playlists * candidate_multiplier, max_playlists)
    keyword_combinations = build_keyword_combinations(keywords)

    for keyword_combo in keyword_combinations:
        print(f"Searching YouTube playlists with: {keyword_combo}")
        search_url = (
            "https://www.youtube.com/results?search_query="
            f"{urllib.parse.quote_plus(keyword_combo)}&sp=EgIQAw%253D%253D"
        )

        try:
            html_text = fetch_text(search_url)
        except Exception as error:
            print(f"Could not search YouTube playlists for '{keyword_combo}': {error}")
            continue

        for playlist_id in re.findall(r'"playlistId":"([^"]+)"', html_text):
            if playlist_id not in playlist_ids and playlist_id not in {"WL"} and not playlist_id.startswith("RD"):
                playlist_ids.append(playlist_id)
            if len(playlist_ids) >= target_count:
                return playlist_ids[:target_count]

    return playlist_ids[:target_count]


def cleanup_youtube_video_title(raw_title):
    cleaned_title = html.unescape(raw_title or "")
    cleaned_title = cleaned_title.replace("\\", "")
    cleaned_title = re.sub(r"\s+", " ", cleaned_title).strip()
    cleanup_patterns = [
        r"\((official|lyrics?|lyric video|audio|music video|video|visualizer|hd|4k|remaster(?:ed)?|live)[^)]*\)",
        r"\[(official|lyrics?|lyric video|audio|music video|video|visualizer|hd|4k|remaster(?:ed)?|live)[^\]]*\]",
    ]
    for pattern in cleanup_patterns:
        cleaned_title = re.sub(pattern, "", cleaned_title, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned_title).strip(" -|")


def score_youtube_song_likelihood(raw_title, cleaned_title):
    lowered_raw = (raw_title or "").lower()
    lowered_cleaned = (cleaned_title or "").lower()
    score = 0

    if any(separator in cleaned_title for separator in [" - ", " – ", " — ", ": "]):
        score += 5

    likely_song_markers = [
        "official video",
        "official audio",
        "lyrics",
        "lyric video",
        "visualizer",
        "topic",
        "audio",
    ]
    if any(marker in lowered_raw for marker in likely_song_markers):
        score += 2

    blocked_markers = [
        "podcast",
        "episode",
        "interview",
        "reaction",
        "review",
        "sermon",
        "homily",
        "lecture",
        "audiobook",
        "trailer",
        "recap",
        "vlog",
        "livestream",
        "live stream",
        "tutorial",
        "lesson",
        "explained",
        "news",
        "documentary",
        "full movie",
        "movie clip",
    ]
    for marker in blocked_markers:
        if marker in lowered_raw or marker in lowered_cleaned:
            score -= 6

    if re.search(r"\b(part|chapter|ep)\b", lowered_cleaned):
        score -= 4

    if cleaned_title.count(" - ") > 1 or cleaned_title.count(": ") > 1:
        score -= 2

    word_count = len(cleaned_title.split())
    if word_count <= 8:
        score += 1
    elif word_count >= 14:
        score -= 2

    return score


def convert_youtube_video_title_to_song(raw_title):
    cleaned_title = cleanup_youtube_video_title(raw_title)
    song_score = score_youtube_song_likelihood(raw_title, cleaned_title)
    separators = [" - ", " – ", " — ", ": "]
    for separator in separators:
        if separator in cleaned_title:
            artist_name, track_title = cleaned_title.split(separator, 1)
            artist_name = artist_name.strip()
            track_title = track_title.strip()
            if artist_name and track_title:
                return {
                    'title': track_title,
                    'artists': artist_name,
                    'youtube_song_score': song_score,
                }

    return {
        'title': cleaned_title or 'Unknown Title',
        'artists': 'Unknown Artist',
        'youtube_song_score': song_score,
    }


def is_likely_youtube_song(song):
    title = song.get('title', '')
    artists = song.get('artists', '')
    score = song.get('youtube_song_score', 0)

    if not title or title == 'Unknown Title':
        return False

    if artists == 'Unknown Artist':
        return score >= 4 and len(title.split()) <= 8

    if len(title.split()) > 12:
        return score >= 6

    return score >= 2


def fetch_songs_from_youtube_public_playlists(
    playlist_ids,
    max_playlists,
    max_tracks_per_playlist,
    min_playlist_size=None,
    max_playlist_size=None,
):
    song_list = []
    playlists_used = 0
    playlist_pattern = re.compile(
        r'"playlistVideoRenderer":\{.*?"videoId":"([^"]+)".*?"title":\{"runs":\[\{"text":"([^"]+)"',
        re.DOTALL,
    )

    for playlist_id in playlist_ids:
        if playlists_used >= max_playlists:
            break

        playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
        print(f"Scraping tracks from YouTube playlist: {playlist_id}")
        try:
            html_text = fetch_text(playlist_url)
        except Exception as error:
            print(f"Could not load YouTube playlist {playlist_id}: {error}")
            continue

        matches = playlist_pattern.findall(html_text)
        if not matches:
            continue

        seen_video_ids = set()
        unique_entries = []
        for video_id, raw_title in matches:
            if video_id in seen_video_ids:
                continue
            seen_video_ids.add(video_id)
            unique_entries.append((video_id, raw_title))

        if min_playlist_size is not None and len(unique_entries) < min_playlist_size:
            print(f"Skipping YouTube playlist '{playlist_id}' because it has only {len(unique_entries)} songs/videos.")
            continue

        if max_playlist_size is not None and len(unique_entries) > max_playlist_size:
            print(f"Skipping YouTube playlist '{playlist_id}' because it has {len(unique_entries)} songs/videos.")
            continue

        playlist_song_count = 0
        skipped_non_song_count = 0
        for video_id, raw_title in unique_entries:
            if playlist_song_count >= max_tracks_per_playlist:
                break

            song = convert_youtube_video_title_to_song(raw_title)
            if not is_likely_youtube_song(song):
                skipped_non_song_count += 1
                continue

            playlist_song = dict(song)
            playlist_song['youtube_url'] = f"https://www.youtube.com/watch?v={video_id}"
            playlist_song['youtube_found_exact_video'] = True
            song_list.append(playlist_song)
            playlist_song_count += 1

        if playlist_song_count:
            print(
                f"Recovered {playlist_song_count} likely song candidate(s) from YouTube playlist '{playlist_id}'"
                f", skipped {skipped_non_song_count} likely non-song video(s)."
            )
            playlists_used += 1

    return song_list


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


def resolve_song_on_spotify(song, suppress_errors=False):
    spotify_client = get_spotify_client(
        required=False,
        prompt_if_missing=False,
        context="Spotify song matching",
    )
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
            'spotify_found_exact_track': bool(track.get('external_urls', {}).get('spotify')) and is_exact_query,
        }
        SPOTIFY_MATCH_CACHE[song_key] = spotify_match
        return spotify_match

    SPOTIFY_MATCH_CACHE[song_key] = None
    return None


def search_youtube_videos_by_keywords(keywords, max_songs):
    song_list = []
    seen_song_keys = set()
    keyword_combinations = build_keyword_combinations(keywords)
    video_pattern = re.compile(r'"videoRenderer":\{.*?"videoId":"([^"]+)".*?"title":\{"runs":\[\{"text":"([^"]+)"', re.DOTALL)

    for keyword_combo in keyword_combinations:
        print(f"Searching YouTube videos with: {keyword_combo}")
        search_url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(keyword_combo)

        try:
            html_text = fetch_text(search_url)
        except Exception as error:
            print(f"Could not search YouTube videos for '{keyword_combo}': {error}")
            continue

        video_count = 0
        skipped_non_song_count = 0
        skipped_duplicate_count = 0
        for video_id, raw_title in video_pattern.findall(html_text):
            song = convert_youtube_video_title_to_song(raw_title)
            if not is_likely_youtube_song(song):
                skipped_non_song_count += 1
                continue

            accepted_song = dict(song)
            accepted_song['youtube_url'] = f"https://www.youtube.com/watch?v={video_id}"
            accepted_song['youtube_found_exact_video'] = True
            if merge_song_list_with_seen(song_list, seen_song_keys, accepted_song):
                video_count += 1
            else:
                skipped_duplicate_count += 1

            if video_count >= YOUTUBE_SEARCH_RESULT_LIMIT or len(song_list) >= max_songs:
                break

        print(
            f"Recovered {video_count} likely song candidate(s) from YouTube search for '{keyword_combo}'"
            f", skipped {skipped_non_song_count} likely non-song result(s),"
            f" and skipped {skipped_duplicate_count} duplicate result(s)."
        )

        if len(song_list) >= max_songs:
            return song_list

    return song_list


def search_tracks_by_keywords(
    keywords,
    max_songs,
    label="Searching YouTube tracks directly...",
):
    print(label)
    song_map = {}
    youtube_songs = search_youtube_videos_by_keywords(keywords, max_songs)
    merge_song_candidates(song_map, youtube_songs, "youtube-track-search")
    song_list = list(song_map.values())
    random.shuffle(song_list)
    return song_list[:max_songs]



def prompt_for_public_discovery_mode():
    print("Choose how public discovery should work:")
    print("1. Hybrid web-only: YouTube playlists + YouTube track search")
    print("2. YouTube playlists only")
    print("3. Track search only (YouTube web search)")
    discovery_choice = input("Enter 1, 2, or 3 [1]: ").strip() or '1'

    mode_map = {
        '1': 'hybrid',
        '2': 'youtube-playlists-web',
        '3': 'youtube-track-search',
    }
    discovery_mode = mode_map.get(discovery_choice)
    if not discovery_mode:
        print("Invalid choice. Using hybrid discovery.")
        return DEFAULT_PUBLIC_DISCOVERY_MODE
    return discovery_mode


def describe_public_discovery_mode(discovery_mode):
    return {
        'hybrid': 'Hybrid web-only public discovery',
        'youtube-playlists-web': 'YouTube playlists',
        'youtube-track-search': 'YouTube track search',
        'spotify-playlists-web': 'YouTube playlists',
        'spotify-track-search': 'YouTube track search',
        'web-no-spotify-api': 'Hybrid web-only public discovery',
    }.get(discovery_mode, 'Hybrid web-only public discovery')


# Function to fetch songs from public music sources by keywords/phrases without caching
def fetch_songs_from_public_playlists_by_keywords(
    keywords,
    max_playlists=15,
    max_songs=500,
    max_tracks_per_playlist=35,
    min_playlist_size=None,
    max_playlist_size=None,
    discovery_mode=DEFAULT_PUBLIC_DISCOVERY_MODE,
):
    print("Starting to fetch songs from public music sources by keywords/phrases...")
    song_map = {}
    legacy_mode_map = {
        'spotify-playlists-web': 'youtube-playlists-web',
        'spotify-track-search': 'youtube-track-search',
        'web-no-spotify-api': 'hybrid',
    }
    if discovery_mode in legacy_mode_map:
        remapped_mode = legacy_mode_map[discovery_mode]
        print("Legacy Spotify-assisted public discovery has been disabled to avoid Spotify API rate limits.")
        print(f"Switching this run to {describe_public_discovery_mode(remapped_mode)}.")
        discovery_mode = remapped_mode

    if discovery_mode in {'hybrid', 'youtube-playlists-web'}:
        youtube_playlist_ids = search_youtube_playlist_ids_by_keywords(keywords, max_playlists)
        youtube_playlist_songs = fetch_songs_from_youtube_public_playlists(
            youtube_playlist_ids,
            max_playlists=max_playlists,
            max_tracks_per_playlist=max_tracks_per_playlist,
            min_playlist_size=min_playlist_size,
            max_playlist_size=max_playlist_size,
        )
        merge_song_candidates(song_map, youtube_playlist_songs, "youtube-playlists")

    if discovery_mode in {'hybrid', 'youtube-track-search'}:
        direct_track_songs = search_tracks_by_keywords(
            keywords,
            max_songs,
            label="Searching YouTube tracks directly...",
        )
        merge_song_candidates(song_map, direct_track_songs, "youtube-track-search")

    song_list = list(song_map.values())
    random.shuffle(song_list)

    print(
        f"Compiled {len(song_list)} unique song candidate(s) from public discovery for: {', '.join(keywords)}"
    )
    return song_list[:max_songs]


def create_spotify_playlist(selected_songs):
    if not spotify_app_credentials_configured():
        print("Spotify playlist creation is unavailable because no Spotify app credentials are configured for this run.")
        return

    # Prompt for playlist name
    playlist_name = input("Enter a name for your new playlist: ").strip()
    if not playlist_name:
        playlist_name = "New Playlist"
        print(f"No name entered. Using default name: {playlist_name}")

    track_uris = []
    for song in selected_songs:
        track_uri = find_spotify_track_for_song(song)
        if track_uri:
            track_uris.append(track_uri)
        else:
            print(f"Track not found: {song['title']} by {song['artists']}")

    if not track_uris:
        print("No tracks were found to add, so the playlist was not created.")
        return

    spotify_client = get_spotify_client(required=True, prompt_if_missing=True, context="Spotify playlist creation")

    # Create the playlist only after we know there is something to add.
    try:
        playlist = spotify_client.current_user_playlist_create(playlist_name, public=False)
        print(f"Playlist '{playlist_name}' created successfully.")
    except spotipy.exceptions.SpotifyException as e:
        abort_if_unreasonable_rate_limit_error(e)
        print(f"Error creating playlist: {e}")
        if getattr(e, "http_status", None) == 403:
            user_id = (CURRENT_USER_PROFILE or {}).get('id', 'unknown')
            print("Spotify rejected playlist creation with HTTP 403.")
            print("The most common causes are:")
            print("- the cached token was authorized without playlist-write scopes")
            print("- the token belongs to an older Spotify app configuration")
            print("- the request is not being treated as the current authenticated user")
            print("This script requests playlist-modify-private and now creates playlists through the current-user endpoint.")
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
    except spotipy.exceptions.SpotifyException as e:
        abort_if_unreasonable_rate_limit_error(e)
        print(f"Error adding tracks to playlist: {e}")
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
            "Only use public playlists with at least how many songs? (Press Enter for no limit): "
        )
        max_playlist_size = prompt_for_optional_positive_number(
            "Only use public playlists with at most how many songs? (Press Enter for no limit): "
        )

        if (
            min_playlist_size is not None
            and max_playlist_size is not None
            and min_playlist_size > max_playlist_size
        ):
            print("Minimum playlist size cannot be greater than maximum playlist size.")
            continue

        return min_playlist_size, max_playlist_size


def write_song_links_to_console(song, indent=""):
    if song.get('spotify_url'):
        label = "direct track" if song.get('spotify_found_exact_track') else "search page"
        print(f"{indent}Spotify ({label}): {song['spotify_url']}")
    if song.get('youtube_url'):
        label = "direct video" if song.get('youtube_found_exact_video') else "search results"
        print(f"{indent}YouTube ({label}): {song['youtube_url']}")


def write_song_links_to_text_file(song, file_handle, indent=""):
    if song.get('spotify_url'):
        label = "direct track" if song.get('spotify_found_exact_track') else "search page"
        file_handle.write(f"{indent}Spotify ({label}): {song['spotify_url']}\n")
    if song.get('youtube_url'):
        label = "direct video" if song.get('youtube_found_exact_video') else "search results"
        file_handle.write(f"{indent}YouTube ({label}): {song['youtube_url']}\n")


def build_song_links_html(song):
    links = []
    if song.get('spotify_url'):
        label = "Open in Spotify" if song.get('spotify_found_exact_track') else "Search on Spotify"
        links.append(f"""<a href="{song['spotify_url']}" target="_blank" rel="noopener noreferrer">{label}</a>""")
    if song.get('youtube_url'):
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
        num_songs_input = input("Do you want one song or more than one? (Enter 'one' or a number): ").strip()

        if num_songs_input.lower() == 'one' or num_songs_input == '1':
            return 1

        try:
            num_songs = int(num_songs_input)
        except ValueError:
            print("Invalid input. Please enter 'one' or a number.")
            continue

        if num_songs < 1:
            print("Please enter 'one' or a positive number.")
            continue

        return num_songs


def prompt_for_source_choice():
    while True:
        print("Choose the source of songs:")
        print("1. All playlists")
        print("2. Filter your playlists by name")
        print("3. Your own playlists")
        print("4. Random saved playlists")
        print("5. Search public music sources by keywords/phrases")
        print("6. Surprise me")
        source_choice = input("Enter the number of your choice (1, 2, 3, 4, 5, or 6): ").strip()
        if source_choice in {'1', '2', '3', '4', '5', '6'}:
            return source_choice
        print("Invalid choice.")


def prompt_for_max_playlists():
    while True:
        try:
            max_playlists = int(input("Enter the maximum number of playlists to search: ").strip())
        except ValueError:
            print("Invalid input. Please enter a positive number.")
            continue

        if max_playlists > 0:
            return max_playlists

        print("Please enter a positive number.")


def prompt_for_keywords():
    while True:
        keywords_input = input("Enter one or more keywords/phrases (separate by commas or enclose phrases in quotes): ").strip()
        keywords = parse_keywords(keywords_input)
        if keywords:
            return keywords
        print("Please enter at least one keyword or phrase.")


def prompt_for_surprise_mode():
    while True:
        print("Choose your Surprise Me mode:")
        print("1. Random emotions/genres via public discovery")
        print("2. random-song.com with its default random configuration")
        print("3. random-song.com with custom configuration")
        surprise_mode = input("Enter 1, 2, or 3: ").strip()
        if surprise_mode in {'1', '2', '3'}:
            return surprise_mode
        print("Invalid choice.")


def prompt_for_output_format():
    while True:
        file_format = input("Do you want to generate a text file, an HTML file, or display in terminal? (Enter 'txt', 'html', or 'terminal'): ").strip().lower()
        if file_format in {'txt', 'html', 'terminal'}:
            return file_format
        if file_format == 'n/a':
            return file_format
        print("Invalid file format choice. Please enter 'txt', 'html', 'terminal', or 'N/A'.")


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

# List of emotions/genres for the "Surprise Me" option

def generate_random_keywords():
    """Generate a random sequence of keywords from the surprise_keywords list."""
    num_keywords = random.randint(2, 4)  # Choose between 2 to 4 random keywords
    return random.sample(surprise_keywords, num_keywords)

# Function to read the emotions/genres from a file
def load_emotions_genres(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            # Read all lines and strip any extra whitespace
            emotions_genres = [line.strip() for line in f.readlines() if line.strip()]
        return emotions_genres
    except FileNotFoundError:
        print(f"Error: The file '{file_path}' was not found.")
        return []

# Load the emotions/genres list from the file in the same directory
EMOTIONS_GENRES_FILE = 'emotions_genres.txt'
EMOTIONS_GENRES = load_emotions_genres(EMOTIONS_GENRES_FILE)

# Check if the list was loaded successfully
if not EMOTIONS_GENRES:
    print("No emotions or genres found in the file. Exiting...")
else:
    # You can now use the `EMOTIONS_GENRES` list in your existing "Surprise Me" option
    print(f"Loaded {len(EMOTIONS_GENRES)} emotions/genres from file.")


def print_project_summary():
    print("\nWhat this script does:")
    print("- Pulls songs from your Spotify playlists or from public music discovery sources.")
    print("- Randomly picks one or more songs from that pool.")
    print("- Can filter your own playlists by name using a preset, simple text, or regex.")
    print("- Can use random-song.com for truly random Spotify track discovery.")
    print("- Can discover songs from public web sources like YouTube playlists and YouTube track search without leaning on Spotify's API.")
    print("- Automatically adds Spotify search pages and YouTube links for the selected songs.")
    print("- Can save the selection to text or HTML output.")
    print("- Can create a new private Spotify playlist from the selected songs.\n")


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
        print("Switching this run to the web-only public discovery fallback.")
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


def build_no_spotify_surprise_fallback_source_description(keywords):
    return f"Fallback Surprise Me (Web-only public discovery for {', '.join(keywords)})"


def maybe_switch_personal_source_to_no_spotify_fallback(num_songs, failure_reason):
    print(f"Spotify became unavailable for this source: {failure_reason}")
    wants_fallback = prompt_yes_no(
        "Do you want to switch to a web-only Surprise Me fallback instead? (yes/no): "
    )
    if not wants_fallback:
        return None

    max_playlists = 10
    max_songs = 20 * max_playlists
    max_tracks_per_playlist = calculate_max_tracks_per_playlist(max_playlists, max_songs)
    fallback_keywords = random.sample(EMOTIONS_GENRES, 3)
    print("Switching to the web-only fallback with random emotions/genres.")
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
    print_project_summary()

    num_songs = prompt_for_num_songs()
    print(f"Number of songs to fetch: {num_songs}")

    source_choice = prompt_for_source_choice()

    # Default values for max_playlists and max_tracks_per_playlist
    max_playlists = None
    max_tracks_per_playlist = None
    max_songs = None
    min_playlist_size = None
    max_playlist_size = None
    keywords = None
    surprise_song_list = None
    public_discovery_mode = DEFAULT_PUBLIC_DISCOVERY_MODE

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
            public_discovery_mode = prompt_for_public_discovery_mode()
            max_songs = 20 * max_playlists
            max_tracks_per_playlist = calculate_max_tracks_per_playlist(max_playlists, max_songs)

            # For option 5, we won't use cache
            cache_file = None
            source_description = f"Public Discovery by Keywords/Phrases ({describe_public_discovery_mode(public_discovery_mode)})"
            keywords = prompt_for_keywords()
        elif source_choice == '6':
            cache_file = None
            surprise_mode = prompt_for_surprise_mode()

            if surprise_mode == '1':
                max_playlists = prompt_for_max_playlists()
                min_playlist_size, max_playlist_size = prompt_for_playlist_size_range()
                public_discovery_mode = prompt_for_public_discovery_mode()
                max_songs = 20 * max_playlists
                max_tracks_per_playlist = calculate_max_tracks_per_playlist(max_playlists, max_songs)

                print("Surprise Me option selected...")
                random_keywords = random.sample(EMOTIONS_GENRES, 3)
                print(f"Randomly selected emotions/genres: {', '.join(random_keywords)}")
                keywords = random_keywords
                source_description = f"Surprise Me (Random Emotions/Genres via {describe_public_discovery_mode(public_discovery_mode)})"
            elif surprise_mode == '2':
                source_description = 'Surprise Me (random-song.com Default Random Config)'
                surprise_song_list = fetch_songs_from_random_song_generator(num_songs, mode="default-config")
            elif surprise_mode == '3':
                options = fetch_random_song_generator_options()
                custom_config = prompt_for_random_song_generator_config(options)
                source_description = 'Surprise Me (random-song.com Custom Config)'
                surprise_song_list = fetch_songs_from_random_song_generator(num_songs, mode="custom-config", config=custom_config)

    if source_choice == '5' or (source_choice == '6' and keywords is not None):
        # Handle option 5 and 6 without caching
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
            return
        selected_songs = select_random_songs(song_list, num_songs)
        if effective_discovery_mode != public_discovery_mode:
            public_discovery_mode = effective_discovery_mode
            if source_choice == '5':
                source_description = (
                    f"Public Discovery by Keywords/Phrases "
                    f"({describe_public_discovery_mode(public_discovery_mode)})"
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
        selected_playlists = []
    else:
        print(f"Selected source: {source_description}")
        print(f"Cache file to use: {cache_file}")

        # Check if cache file exists
        print(f"Checking if cache file exists: {cache_file}")
        if os.path.exists(cache_file):
            print(f"Cache file '{cache_file}' exists.")
            update_choice = input("Do you want to check for updates? (yes/no): ").strip().lower()
            if update_choice == 'yes':
                # If the user wants to update, delete the old .json file first
                print(f"Deleting old cache file: {cache_file}")
                try:
                    os.remove(cache_file)
                    print(f"Old cache file '{cache_file}' deleted successfully.")
                except Exception as e:
                    print(f"Failed to delete cache file '{cache_file}': {e}")
                    return

                # Fetch songs and update cache
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
                # Load songs from cache
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
            print(f"Cache file '{cache_file}' does not exist.")
            # No cache file exists, fetch songs and create cache
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
                return

            selected_songs = select_random_songs(song_list, num_songs)
            if not selected_songs:
                print("No tracks found.")
                return
            print(f"Selected {len(selected_songs)} songs.")

    num_songs = len(selected_songs)

    link_platform = 'both'
    attach_platform_links(selected_songs, link_platform)

    if num_songs == 1:
        # Output the single song
        random_song = selected_songs[0]
        print(f"\nHere is your song from {source_description}:\n1. {random_song['title']} by {random_song['artists']}")
        write_song_links_to_console(random_song)
        if source_choice == '4':
            print(f"Selected from playlists: {', '.join(selected_playlists)}")
    else:
        # Ask for file format or terminal output
        file_format = prompt_for_output_format()

        if file_format == 'terminal':
            # Display songs directly in the terminal
            print(f"\nYour Selected Songs from {source_description}:\n")
            for idx, song in enumerate(selected_songs, start=1):
                song_title = song['title']
                song_artists = song['artists']
                print(f"{idx}. {song_title} by {song_artists}")
                write_song_links_to_console(song, indent="   ")
            if source_choice == '4':
                print(f"\nSelected from playlists: {', '.join(selected_playlists)}")

        elif file_format == 'n/a':
            print("Skipping file generation.")
        elif file_format in ['txt', 'html']:
            # Get current date and time
            now = datetime.now()
            date_str = now.strftime("%Y.%m.%d at %Hhr%M")

            # Sanitize the source description for filenames
            safe_source_description = sanitize_filename(source_description)

            # Create filename with date and source description
            filename = f"{date_str} - {safe_source_description}.{file_format}"

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
                    with open(filepath, 'w', encoding='utf-8') as f:
                        f.write(f"Your Selected Songs from {source_description} (Generated on {date_str}):\n\n")
                        if source_choice == '4':
                            f.write(f"Selected from playlists:\n")
                            for plist in selected_playlists:
                                f.write(f"- {plist}\n")
                            f.write("\n")
                        for idx, song in enumerate(selected_songs, start=1):
                            song_title = song['title']
                            song_artists = song['artists']
                            f.write(f"{idx}. {song_title} by {song_artists}\n")
                            write_song_links_to_text_file(song, f, indent="   ")
                    print(f"\n{num_songs} songs have been written to '{abs_filepath}'.\n")
                except Exception as e:
                    print(f"Failed to write to file '{abs_filepath}': {e}")

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
    <title>Your Selected Songs - {readable_date}</title>
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
    <h1>Your Selected Songs</h1>
    <p class="source">Source: {source_description}</p>
"""
                    if source_choice == '4':
                        html_content += f"""    <p class="playlists">Selected from playlists: {', '.join(selected_playlists)}</p>
"""
                    html_content += f"""    <p class="date">Generated on {readable_date}</p>
    <ol>
"""

                    for song in selected_songs:
                        song_title = song['title']
                        song_artists = song['artists']
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
                except Exception as e:
                    print(f"Failed to write to HTML file '{abs_filepath}': {e}")

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
            print(f"Skipping Spotify playlist creation because Spotify is unavailable right now: {error}")

    if any(song.get('spotify_url') or song.get('youtube_url') for song in selected_songs):
        maybe_open_platform_links(selected_songs, link_platform)


if __name__ == "__main__":
    try:
        main()
    except RunAborted as error:
        print(error)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nRun cancelled.")
        sys.exit(130)
