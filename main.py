#!/usr/bin/env python3

import getpass
import json
import os
import random
import re
import string
import sys
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


APP_NAME = "spotify-scripts"
APP_DIR = Path.home() / f".{APP_NAME}"
TOKEN_CACHE_PATH = APP_DIR / "token_cache.json"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8080/callback"
SCOPE = "user-read-private playlist-read-private playlist-modify-private playlist-modify-public"


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
        temp_path = self.cache_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as cache_file:
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


def create_spotify_client():
    print("Spotify credentials are read from environment variables when available.")
    print("If they are not set, this script will ask for them and keep the auth token in a private local cache.")

    client_id = prompt_for_env_var("SPOTIPY_CLIENT_ID", "Enter your Spotify Client ID")
    client_secret = prompt_for_env_var("SPOTIPY_CLIENT_SECRET", "Enter your Spotify Client Secret", secret=True)
    redirect_uri = prompt_for_env_var(
        "SPOTIPY_REDIRECT_URI",
        "Enter your Spotify Redirect URI",
        default=DEFAULT_REDIRECT_URI,
    )

    if not client_id or not client_secret:
        print("Error: Spotify client ID and client secret are required.")
        sys.exit(1)

    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=SCOPE,
        open_browser=True,
        cache_handler=SecureTokenCacheHandler(TOKEN_CACHE_PATH),
    )

    spotify_client = spotipy.Spotify(auth_manager=auth_manager)

    try:
        user = spotify_client.current_user()
        display_name = user.get("display_name") or user.get("id") or "unknown user"
        print(f"Successfully authenticated as {display_name}")
    except spotipy.exceptions.SpotifyException as error:
        print(f"Authentication failed: {error}")
        sys.exit(1)

    return spotify_client


sp = create_spotify_client()


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
        print(f"Error fetching tracks from playlist '{playlist_name}': {e}")
    except Exception as e:
        print(f"Unexpected error fetching tracks from playlist '{playlist_name}': {e}")
    return tracks


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
        track = item.get('track')
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


def fetch_and_cache_filtered_playlist_songs(cache_file, playlist_filter, filter_description, progress_label='Fetching playlists'):
    print(f"Starting to fetch and cache songs for playlist filter: {filter_description}...")
    try:
        playlists = fetch_current_user_playlists(progress_label)
        filtered_playlists = [playlist for playlist in playlists if playlist_filter(playlist)]
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
def fetch_and_cache_all_songs(cache_file):
    return fetch_and_cache_filtered_playlist_songs(
        cache_file,
        playlist_filter=lambda playlist: True,
        filter_description='All playlists',
        progress_label='Fetching all playlists',
    )


# Function to fetch songs from Rediscover playlists and update the cache
def fetch_and_cache_rediscover_songs(cache_file):
    date_pattern = re.compile(r'^Rediscover\s-\s[A-Za-z]{3}\s\d{1,2}(st|nd|rd|th)$', re.IGNORECASE)
    return fetch_and_cache_filtered_playlist_songs(
        cache_file,
        playlist_filter=lambda playlist: bool(date_pattern.match(playlist['name'])),
        filter_description='Rediscover preset',
    )


# Function to fetch songs from user-created playlists and update the cache
def fetch_and_cache_user_playlists_songs(cache_file):
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
        user_playlists = [plist for plist in playlists if plist['owner']['id'] == user_id]
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
            track = item.get('track')
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


def prompt_for_playlist_name_filter():
    print("Choose how to match playlist names:")
    print("1. Rediscover preset")
    print("2. Playlist name contains text")
    print("3. Custom regex")
    filter_choice = input("Enter 1, 2, or 3: ").strip()

    if filter_choice == '1':
        return {
            'cache_file': 'song_cache_rediscover.json',
            'fetcher': fetch_and_cache_rediscover_songs,
            'description': 'Rediscover Playlists',
        }

    if filter_choice == '2':
        search_text = input("Enter text to look for in playlist names: ").strip()
        if not search_text:
            print("No text entered.")
            return None

        lowered_search_text = search_text.lower()
        description = f"Playlists containing '{search_text}'"
        cache_file = build_filtered_playlist_cache_file(f"contains_{search_text}")
        return {
            'cache_file': cache_file,
            'fetcher': lambda selected_cache_file: fetch_and_cache_filtered_playlist_songs(
                selected_cache_file,
                playlist_filter=lambda playlist: lowered_search_text in playlist['name'].lower(),
                filter_description=description,
            ),
            'description': description,
        }

    if filter_choice == '3':
        regex_input = input("Enter a regex for playlist names: ").strip()
        if not regex_input:
            print("No regex entered.")
            return None

        try:
            compiled_pattern = re.compile(regex_input, re.IGNORECASE)
        except re.error as error:
            print(f"Invalid regex: {error}")
            return None

        description = f"Playlists matching regex '{regex_input}'"
        cache_file = build_filtered_playlist_cache_file(f"regex_{regex_input}")
        return {
            'cache_file': cache_file,
            'fetcher': lambda selected_cache_file: fetch_and_cache_filtered_playlist_songs(
                selected_cache_file,
                playlist_filter=lambda playlist: bool(compiled_pattern.search(playlist['name'])),
                filter_description=description,
            ),
            'description': description,
        }

    print("Invalid choice.")
    return None


# Function to fetch songs from random playlists
def fetch_and_cache_random_playlists_songs(cache_file, num_songs):
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

        if not playlists:
            print("No playlists found.")
            return [], []

        # Shuffle the list of playlists
        random.shuffle(playlists)

        selected_playlists = []
        total_songs_collected = 0
        all_tracks = []

        for playlist in playlists:
            print(f"Fetching tracks from random playlist: {playlist['name']}")
            tracks = get_playlist_tracks(playlist['id'], playlist['name'])
            if tracks:
                all_tracks.extend(tracks)
                selected_playlists.append(playlist)
                total_songs_collected += len(tracks)

                if total_songs_collected >= num_songs:
                    break

        if not all_tracks:
            print("No tracks found in the selected playlists.")
            return [], []

        print(f"Total tracks fetched from random playlists: {len(all_tracks)}")

        # Collect song names and artists
        song_list = []
        for item in all_tracks:
            track = item.get('track')
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


def build_youtube_search_url(song):
    query = f"{song['title']} {song['artists']}"
    encoded_query = urllib.parse.quote_plus(query)
    return f"https://www.youtube.com/results?search_query={encoded_query}"


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
    query = f"track:{song['title']} artist:{song['artists']}"
    try:
        result = sp.search(q=query, type='track', limit=1)
    except spotipy.exceptions.SpotifyException as error:
        print(f"Could not search Spotify for {song['title']} by {song['artists']}: {error}")
        return None, False

    tracks = result.get('tracks', {}).get('items', [])
    if not tracks:
        return None, False

    spotify_url = tracks[0].get('external_urls', {}).get('spotify')
    return spotify_url, bool(spotify_url)


def attach_platform_links(selected_songs, link_platform):
    if link_platform in {'spotify', 'both'}:
        print("Looking up Spotify links for the selected songs...")
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


def prompt_for_link_platform():
    print("Do you want to look up links for the selected songs?")
    print("1. No links")
    print("2. Spotify links")
    print("3. YouTube links")
    print("4. Both Spotify and YouTube")
    link_choice = input("Enter 1, 2, 3, or 4: ").strip()

    if link_choice == '1':
        return None
    if link_choice == '2':
        return 'spotify'
    if link_choice == '3':
        return 'youtube'
    if link_choice == '4':
        return 'both'

    print("Invalid choice. Skipping link lookup.")
    return None


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


# Function to search public playlists by multiple keywords/phrases in combinations
def search_public_playlists_by_keywords(keywords, max_playlists, max_playlist_size=None):
    playlists = []

    # Generate unique single-keyword and pairwise search combinations.
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

    # Now rotate through combinations of keywords
    for keyword_combo in keyword_combinations:
        print(f"Searching for playlists with the combination: {keyword_combo}")
        results = sp.search(q=keyword_combo, type='playlist', limit=min(max_playlists, 50))  # Use min to ensure we don't exceed Spotify's 50 limit
        playlist_results = results.get('playlists', {})

        if 'items' in playlist_results:
            playlists.extend(
                playlist
                for playlist in playlist_results['items']
                if isinstance(playlist, dict) and playlist.get('id')
            )

        # Handle pagination for more playlists
        while playlist_results.get('next') and len(playlists) < max_playlists:
            results = sp.next(playlist_results)
            playlist_results = results
            if 'items' in results:
                playlists.extend(
                    playlist
                    for playlist in results['items']
                    if isinstance(playlist, dict) and playlist.get('id')
                )
            if len(playlists) >= max_playlists:
                break

    # Remove duplicates based on playlist ID
    unique_playlists = list({p['id']: p for p in playlists}.values())

    if max_playlist_size is not None:
        filtered_playlists = []
        skipped_playlists = 0
        for playlist in unique_playlists:
            total_tracks = playlist.get('tracks', {}).get('total')
            if isinstance(total_tracks, int) and total_tracks <= max_playlist_size:
                filtered_playlists.append(playlist)
            else:
                skipped_playlists += 1
        unique_playlists = filtered_playlists
        print(f"Skipped {skipped_playlists} public playlists with more than {max_playlist_size} songs.")

    print(f"Found {len(unique_playlists)} public playlists for keyword combinations: {', '.join(keywords)}")
    return unique_playlists



# Function to fetch songs from public playlists by multiple keywords/phrases without caching
def fetch_songs_from_public_playlists_by_keywords(
    keywords,
    max_playlists=15,
    max_songs=500,
    max_tracks_per_playlist=35,
    max_playlist_size=None,
):
    print("Starting to fetch songs from public playlists by keywords/phrases...")
    playlists = search_public_playlists_by_keywords(keywords, max_playlists, max_playlist_size=max_playlist_size)

    all_tracks = []
    playlist_count = 0

    # Shuffle the order of the playlists to ensure randomness
    random.shuffle(playlists)

    for playlist in playlists:
        if len(all_tracks) >= max_songs:
            break

        print(f"Fetching tracks from playlist: {playlist['name']}")
        tracks = get_playlist_tracks(playlist['id'], playlist['name'])

        # Limit the number of tracks fetched per playlist to avoid filling up from a single playlist
        limited_tracks = tracks[:max_tracks_per_playlist]
        all_tracks.extend(limited_tracks)

        print(f"Fetched {len(limited_tracks)} tracks from playlist: {playlist['name']}")
        playlist_count += 1

        if len(all_tracks) >= max_songs:
            print(f"Reached the limit of {max_songs} tracks.")
            break

    print(f"Fetched {len(all_tracks)} tracks from {playlist_count} playlists matching the keywords/phrases {', '.join(keywords)}")

    # Collect song names and artists
    song_list = []
    for item in all_tracks[:max_songs]:
        track = item.get('track')
        if track:
            track_name = track.get('name') or 'Unknown Title'
            track_artists = track.get('artists', [])
            artist_names = [artist.get('name', 'Unknown Artist') for artist in track_artists]
            artists = ', '.join(artist_names)
            song_entry = {'title': track_name, 'artists': artists}
            song_list.append(song_entry)

    return song_list


def create_spotify_playlist(selected_songs):
    try:
        # Get current user's ID
        user_id = sp.current_user()['id']
    except spotipy.exceptions.SpotifyException as e:
        print(f"Error fetching user ID: {e}")
        return

    # Prompt for playlist name
    playlist_name = input("Enter a name for your new playlist: ").strip()
    if not playlist_name:
        playlist_name = "New Playlist"
        print(f"No name entered. Using default name: {playlist_name}")

    # Create the playlist
    try:
        playlist = sp.user_playlist_create(user_id, playlist_name, public=False)
        print(f"Playlist '{playlist_name}' created successfully.")
    except spotipy.exceptions.SpotifyException as e:
        print(f"Error creating playlist: {e}")
        return

    # Collect track URIs
    track_uris = []
    for song in selected_songs:
        # Search for the track
        query = f"track:{song['title']} artist:{song['artists']}"
        try:
            result = sp.search(q=query, type='track', limit=1)
            tracks = result.get('tracks', {}).get('items', [])
            if tracks:
                track_uri = tracks[0]['uri']
                track_uris.append(track_uri)
            else:
                print(f"Track not found: {song['title']} by {song['artists']}")
        except spotipy.exceptions.SpotifyException as e:
            print(f"Error searching for track: {e}")

    if not track_uris:
        print("No tracks were found to add to the playlist.")
        return

    # Add tracks to the playlist in batches of up to 100
    try:
        for i in range(0, len(track_uris), 100):
            batch = track_uris[i:i+100]
            sp.playlist_add_items(playlist['id'], batch)
        print(f"Added {len(track_uris)} tracks to '{playlist_name}'.")
    except spotipy.exceptions.SpotifyException as e:
        print(f"Error adding tracks to playlist: {e}")

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


def write_song_links_to_console(song, indent=""):
    if song.get('spotify_url'):
        label = "direct track" if song.get('spotify_found_exact_track') else "search result"
        print(f"{indent}Spotify ({label}): {song['spotify_url']}")
    if song.get('youtube_url'):
        label = "direct video" if song.get('youtube_found_exact_video') else "search results"
        print(f"{indent}YouTube ({label}): {song['youtube_url']}")


def write_song_links_to_text_file(song, file_handle, indent=""):
    if song.get('spotify_url'):
        label = "direct track" if song.get('spotify_found_exact_track') else "search result"
        file_handle.write(f"{indent}Spotify ({label}): {song['spotify_url']}\n")
    if song.get('youtube_url'):
        label = "direct video" if song.get('youtube_found_exact_video') else "search results"
        file_handle.write(f"{indent}YouTube ({label}): {song['youtube_url']}\n")


def build_song_links_html(song):
    links = []
    if song.get('spotify_url'):
        label = "Open in Spotify" if song.get('spotify_found_exact_track') else "Spotify result"
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

# List of emotions/genres for the "Surprise Me" option

def generate_random_keywords():
    """Generate a random sequence of keywords from the surprise_keywords list."""
    num_keywords = random.randint(2, 4)  # Choose between 2 to 4 random keywords
    return random.sample(surprise_keywords, num_keywords)

import random  # Ensure this is imported for random keyword generation

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
    print("- Pulls songs from your Spotify playlists or from public playlists.")
    print("- Randomly picks one or more songs from that pool.")
    print("- Can filter your own playlists by name using a preset, simple text, or regex.")
    print("- Can look up selected songs on Spotify, YouTube, or both.")
    print("- Can save the selection to text or HTML output.")
    print("- Can create a new private Spotify playlist from the selected songs.\n")


def main():
    print_project_summary()

    # Ask the user for input
    num_songs_input = input("Do you want one song or more than one? (Enter 'one' or a number): ").strip()

    if num_songs_input.lower() == 'one' or num_songs_input == '1':
        num_songs = 1
    else:
        try:
            num_songs = int(num_songs_input)
            if num_songs < 1:
                print("Please enter 'one' or a positive number.")
                return
        except ValueError:
            print("Invalid input. Please enter 'one' or a number.")
            return

    print(f"Number of songs to fetch: {num_songs}")

    # Ask the user which songs to use
    print("Choose the source of songs:")
    print("1. All playlists")
    print("2. Filter your playlists by name")
    print("3. Your own playlists")
    print("4. Random saved playlists")
    print("5. Search public playlists by keywords/phrases")
    print("6. Surprise me")
    source_choice = input("Enter the number of your choice (1, 2, 3, 4, 5, or 6): ").strip()

    # Default values for max_playlists and max_tracks_per_playlist
    max_playlists = None
    max_tracks_per_playlist = None
    max_songs = None
    max_playlist_size = None

    if source_choice == '1':
        cache_file = 'song_cache_all.json'
        fetch_and_cache_songs = fetch_and_cache_all_songs
        source_description = 'All Playlists'
    elif source_choice == '2':
        filter_config = prompt_for_playlist_name_filter()
        if not filter_config:
            return
        cache_file = filter_config['cache_file']
        fetch_and_cache_songs = filter_config['fetcher']
        source_description = filter_config['description']
    elif source_choice == '3':
        cache_file = 'song_cache_user_playlists.json'
        fetch_and_cache_songs = fetch_and_cache_user_playlists_songs
        source_description = 'Your Own Playlists'
    elif source_choice == '4':
        cache_file = 'song_cache_random_playlists.json'
        fetch_and_cache_songs = lambda cache_file: fetch_and_cache_random_playlists_songs(cache_file, num_songs)
        source_description = 'Random Playlists'
    elif source_choice == '5' or source_choice == '6':
        # Only prompt for max playlists when option 5 or 6 is selected
        while True:
            try:
                max_playlists = int(input("Enter the maximum number of playlists to search: ").strip())
                if max_playlists > 0:
                    break
                else:
                    print("Please enter a positive number.")
            except ValueError:
                print("Invalid input. Please enter a positive number.")

        max_playlist_size = prompt_for_optional_positive_number(
            "Only use public playlists with at most how many songs? (Press Enter for no limit): "
        )
        max_songs = 20 * max_playlists  # You can keep this logic to calculate the total max songs
        max_tracks_per_playlist = calculate_max_tracks_per_playlist(max_playlists, max_songs)

        if source_choice == '5':
            # For option 5, we won't use cache
            cache_file = None
            source_description = 'Public Playlists by Keywords/Phrases'
            keywords_input = input("Enter one or more keywords/phrases (separate by commas or enclose phrases in quotes): ").strip()
            keywords = parse_keywords(keywords_input)
        elif source_choice == '6':
            # For the 'Surprise Me' option, generate random emotions/genres
            print("Surprise Me option selected...")
            random_keywords = random.sample(EMOTIONS_GENRES, 3)  # Select 3 random emotions/genres
            print(f"Randomly selected emotions/genres: {', '.join(random_keywords)}")
            keywords = random_keywords
            cache_file = None
            source_description = 'Surprise Me (Random Emotions/Genres)'
    else:
        print("Invalid choice. Please run the script again and select a valid option.")
        return

    if source_choice == '5' or source_choice == '6':
        # Handle option 5 and 6 without caching
        song_list = fetch_songs_from_public_playlists_by_keywords(
            keywords,
            max_playlists=max_playlists,
            max_songs=max_songs,
            max_tracks_per_playlist=max_tracks_per_playlist,
            max_playlist_size=max_playlist_size,
        )
        if not song_list:
            print("No tracks found.")
            return
        selected_songs = random.sample(song_list, min(num_songs, len(song_list)))
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
                if source_choice == '4':
                    song_list, selected_playlists = fetch_and_cache_songs(cache_file)
                else:
                    song_list = fetch_and_cache_songs(cache_file)
                    selected_playlists = []
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
            if source_choice == '4':
                song_list, selected_playlists = fetch_and_cache_songs(cache_file)
            else:
                song_list = fetch_and_cache_songs(cache_file)
                selected_playlists = []

        if not song_list:
            print("No tracks found.")
            return

        if num_songs > len(song_list):
            print(f"Only found {len(song_list)} songs. Returning all of them.")
            num_songs = len(song_list)

        selected_songs = random.sample(song_list, num_songs)
        print(f"Selected {num_songs} songs.")

    link_platform = prompt_for_link_platform()
    if link_platform:
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
        file_format = input("Do you want to generate a text file, an HTML file, or display in terminal? (Enter 'txt', 'html', or 'terminal'): ").strip().lower()

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

        else:
            print("Invalid file format choice. Please enter 'txt', 'html', 'terminal', or 'N/A'.")
            return

    # Ask the user if they want to create a Spotify playlist
    create_playlist_choice = input("Do you want to create a Spotify playlist with these songs? (yes/no): ").strip().lower()
    if create_playlist_choice == 'yes':
        create_spotify_playlist(selected_songs)

    if any(song.get('spotify_url') or song.get('youtube_url') for song in selected_songs):
        maybe_open_platform_links(selected_songs, link_platform)


if __name__ == "__main__":
    main()
