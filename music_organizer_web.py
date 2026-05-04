#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Music Organizer — Web UI (Flask + Tinder-style swipe interface)."""

import json
import os
import random
import re
import shutil
import threading
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, jsonify, request, send_file, abort

from music_organizer import (
    AUDIO_EXTENSIONS,
    get_existing_tags,
    get_fingerprint,
    lookup_acoustid,
    organize_file,
    sanitize,
    write_tags,
    write_cover_art,
    fetch_cover_art_bytes,
    ACOUSTID_API_KEY,
)

app = Flask(__name__, static_folder="web_static", static_url_path="/static")

# ─── Config ────────────────────────────────────────────────────────────────────

SOURCE_FOLDER = os.environ.get("SOURCE_FOLDER", "")
OUTPUT_FOLDER = os.environ.get("OUTPUT_FOLDER", SOURCE_FOLDER)
API_KEY       = ACOUSTID_API_KEY

# State file: tracks which files have been accepted/skipped
STATE_FILE = Path(SOURCE_FOLDER) / ".music_organizer_state.json" if SOURCE_FOLDER else Path(".music_organizer_state.json")


def _playlist_file() -> Path:
    base = Path(OUTPUT_FOLDER) if OUTPUT_FOLDER else (Path(SOURCE_FOLDER) if SOURCE_FOLDER else Path("."))
    return base / "playlist.json"

# In-memory fingerprint cache to avoid re-fingerprinting already looked-up files
_fingerprint_cache: dict = {}  # path -> {"existing": ..., "candidates": [...]}
_cache_lock = threading.Lock()

# ─── State helpers ─────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {"accepted": [], "skipped": [], "skipped_offsets": {}}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), "utf-8")


def _load_playlist() -> list:
    pf = _playlist_file()
    if pf.exists():
        try:
            return json.loads(pf.read_text("utf-8"))
        except Exception:
            pass
    return []


def _save_playlist(playlist: list) -> None:
    pf = _playlist_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps(playlist, indent=2, ensure_ascii=False), "utf-8")


def _all_audio_files() -> list[str]:
    src = Path(SOURCE_FOLDER)
    if not src.is_dir():
        return []
    return sorted(
        str(p) for p in src.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )

# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file("web_static/index.html")


@app.route("/api/status")
def api_status():
    """Return overall progress."""
    state   = _load_state()
    all_f   = _all_audio_files()
    accepted = set(state.get("accepted", []))
    skipped  = set(state.get("skipped", []))
    pending  = [f for f in all_f if f not in accepted and f not in skipped]
    return jsonify({
        "total":    len(all_f),
        "accepted": len(accepted),
        "skipped":  len(skipped),
        "pending":  len(pending),
        "playlist": len(_load_playlist()),
        "source":   SOURCE_FOLDER,
        "output":   OUTPUT_FOLDER,
    })


@app.route("/api/next")
def api_next():
    """Return the next file to review (pending first, then skipped)."""
    state    = _load_state()
    all_f    = _all_audio_files()
    accepted = set(state.get("accepted", []))
    skipped  = list(state.get("skipped", []))

    pending = [f for f in all_f if f not in accepted and f not in skipped]

    # Pick next: random from pending first, fallback to random skipped
    if pending:
        candidate = random.choice(pending)
    elif skipped:
        candidate = random.choice(skipped)
    else:
        return jsonify({"done": True})

    path = Path(candidate)
    existing = get_existing_tags(path)

    # Fingerprint + AcoustID lookup (with cache)
    with _cache_lock:
        cached = _fingerprint_cache.get(candidate)

    if cached is None:
        duration, fp = get_fingerprint(path)
        acoustid_candidates = []
        if duration and fp and API_KEY:
            acoustid_candidates = lookup_acoustid(duration, fp, API_KEY)
        cached = {"existing": existing, "candidates": acoustid_candidates}
        with _cache_lock:
            _fingerprint_cache[candidate] = cached

    # Best suggestion: pick candidate based on skip offset, else existing tags
    candidates = cached["candidates"]
    skipped_offsets = state.get("skipped_offsets", {})
    candidate_offset = skipped_offsets.get(candidate, 0)
    if candidates:
        idx = candidate_offset % len(candidates)
        best = candidates[idx]
    else:
        best = existing if any(existing.values()) else {}
    num_candidates = len(candidates)

    # Destination preview
    ext = path.suffix.lower()
    dest_preview = str(
        Path(sanitize(best.get("artist") or "Unknown Artist"))
        / sanitize(best.get("album") or "Unknown Album")
        / f"{sanitize(best.get('title') or path.stem)}{ext}"
    )
    dest_full = str(Path(OUTPUT_FOLDER) / dest_preview) if OUTPUT_FOLDER else dest_preview

    # Pre-fetch the *next* pending file in the background while the user decides.
    _trigger_prefetch(exclude=candidate)

    return jsonify({
        "done":             False,
        "path":             candidate,
        "filename":         path.name,
        "existing":         existing,
        "candidates":       candidates,
        "best":             best,
        "dest_preview":     dest_preview,
        "dest_full":        dest_full,
        "is_skipped":       candidate in skipped,
        "candidate_offset": candidate_offset,
        "num_candidates":   num_candidates,
    })


@app.route("/api/accept", methods=["POST"])
def api_accept():
    """Accept the best suggestion (or a custom one) and copy the file."""
    data = request.get_json(force=True)
    file_path = data.get("path", "")
    choice    = data.get("choice", {})  # {title, artist, album}

    if not file_path:
        abort(400, "Missing path")

    path = Path(file_path)
    if not path.is_file():
        abort(404, "File not found")

    out_dir = Path(OUTPUT_FOLDER) if OUTPUT_FOLDER else path.parent

    dest = organize_file(
        path,
        choice.get("artist", ""),
        choice.get("album",  ""),
        choice.get("title",  ""),
        out_dir,
        copy=True,  # always copy in web mode
    )
    write_tags(dest, choice.get("title", ""), choice.get("artist", ""), choice.get("album", ""))
    img_data, img_mime = fetch_cover_art_bytes(
        mbid=choice.get("release_mbid", ""),
        artist=choice.get("artist", ""),
        album=choice.get("album", ""),
    )
    if img_data:
        write_cover_art(dest, img_data, img_mime or "image/jpeg")

    state = _load_state()
    accepted        = state.setdefault("accepted",        [])
    skipped         = state.setdefault("skipped",         [])
    skipped_offsets = state.setdefault("skipped_offsets", {})

    if file_path not in accepted:
        accepted.append(file_path)
    if file_path in skipped:
        skipped.remove(file_path)
    skipped_offsets.pop(file_path, None)

    _save_state(state)

    # Evict cache so the slot is freed, then pre-fetch the next song.
    with _cache_lock:
        _fingerprint_cache.pop(file_path, None)
    _trigger_prefetch()

    return jsonify({"ok": True, "dest": str(dest)})


@app.route("/api/star", methods=["POST"])
def api_star():
    """Accept the track AND add it to the playlist JSON."""
    data = request.get_json(force=True)
    file_path = data.get("path", "")
    choice    = data.get("choice", {})

    if not file_path:
        abort(400, "Missing path")

    path = Path(file_path)
    if not path.is_file():
        abort(404, "File not found")

    out_dir = Path(OUTPUT_FOLDER) if OUTPUT_FOLDER else path.parent

    dest = organize_file(
        path,
        choice.get("artist", ""),
        choice.get("album",  ""),
        choice.get("title",  ""),
        out_dir,
        copy=True,
    )
    write_tags(dest, choice.get("title", ""), choice.get("artist", ""), choice.get("album", ""))
    img_data, img_mime = fetch_cover_art_bytes(
        mbid=choice.get("release_mbid", ""),
        artist=choice.get("artist", ""),
        album=choice.get("album", ""),
    )
    if img_data:
        write_cover_art(dest, img_data, img_mime or "image/jpeg")

    # Mark as accepted (same logic as api_accept)
    state = _load_state()
    accepted        = state.setdefault("accepted",        [])
    skipped         = state.setdefault("skipped",         [])
    skipped_offsets = state.setdefault("skipped_offsets", {})

    if file_path not in accepted:
        accepted.append(file_path)
    if file_path in skipped:
        skipped.remove(file_path)
    skipped_offsets.pop(file_path, None)

    _save_state(state)

    # Append to playlist
    playlist = _load_playlist()
    playlist.append({
        "title":  choice.get("title",  "") or path.stem,
        "artist": choice.get("artist", ""),
        "album":  choice.get("album",  ""),
        "source": file_path,
        "dest":   str(dest),
    })
    _save_playlist(playlist)

    with _cache_lock:
        _fingerprint_cache.pop(file_path, None)
    _trigger_prefetch()

    return jsonify({"ok": True, "dest": str(dest), "playlist_size": len(playlist)})


@app.route("/api/skip", methods=["POST"])
def api_skip():
    """Skip the current file (will be re-proposed later)."""
    data = request.get_json(force=True)
    file_path = data.get("path", "")

    if not file_path:
        abort(400, "Missing path")

    state = _load_state()
    skipped         = state.setdefault("skipped",         [])
    accepted        = state.setdefault("accepted",        [])
    skipped_offsets = state.setdefault("skipped_offsets", {})

    if file_path not in accepted:
        if file_path not in skipped:
            skipped.append(file_path)
        # Increment candidate offset so next proposal shows the next suggestion
        skipped_offsets[file_path] = skipped_offsets.get(file_path, 0) + 1

    _save_state(state)
    _trigger_prefetch(exclude=file_path)
    return jsonify({"ok": True})


@app.route("/api/audio")
def api_audio():
    """Stream an audio file by its absolute path."""
    file_path = request.args.get("path", "")
    if not file_path:
        abort(400)
    path = Path(file_path)
    # Security: only serve files inside SOURCE_FOLDER
    try:
        path.resolve().relative_to(Path(SOURCE_FOLDER).resolve())
    except ValueError:
        abort(403)
    if not path.is_file():
        abort(404)
    return send_file(str(path))


@app.route("/api/reset_skipped", methods=["POST"])
def api_reset_skipped():
    """Clear all skipped entries so they become pending again."""
    state = _load_state()
    state["skipped"] = []
    _save_state(state)
    return jsonify({"ok": True})


@app.route("/api/search_by_text", methods=["POST"])
def api_search_by_text():
    """Search MusicBrainz recordings by title and/or artist."""
    data   = request.get_json(force=True)
    title  = str(data.get("title",  "")).strip()
    artist = str(data.get("artist", "")).strip()

    if not title and not artist:
        abort(400, "Provide at least a title or artist")

    parts = []
    if title:
        parts.append(f'recording:"{title}"')
    if artist:
        parts.append(f'artist:"{artist}"')
    query = " AND ".join(parts)

    url = "https://musicbrainz.org/ws/2/recording/?" + urllib.parse.urlencode({
        "query": query,
        "fmt":   "json",
        "limit": "10",
    })

    req = urllib.request.Request(url, headers={
        "User-Agent": "MusicOrganizer/1.0 (music-organizer)",
    })

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        abort(502, f"MusicBrainz error: {exc}")

    candidates = []
    seen: set = set()
    for recording in result.get("recordings", []):
        title_r  = recording.get("title", "")
        ac       = recording.get("artist-credit", [])
        artist_r = ac[0].get("name", "") if ac else ""
        releases = recording.get("releases", [])
        album_r  = releases[0].get("title", "") if releases else ""
        score    = int(recording.get("score", 0))

        key = (title_r.lower(), artist_r.lower(), album_r.lower())
        if key in seen:
            continue
        seen.add(key)

        candidates.append({
            "title":  title_r,
            "artist": artist_r,
            "album":  album_r,
            "score":  round(score / 100.0, 2),
            "release_mbid": releases[0].get("id", "") if releases else "",
        })

    return jsonify({"candidates": candidates})


_COMPILATION_RE = re.compile(
    r'greatest\s+hits?|best\s+of|collect(?:ion|ed)|essential|anthology|'
    r'the\s+very\s+best|platinum\s+(?:hits?|edition)|diamond|box\s+set|rarities|'
    r'definitive\s+collection|ultimate\s+collection',
    re.IGNORECASE,
)


@app.route("/api/search_artists", methods=["POST"])
def api_search_artists():
    """Given a song title, return a ranked list of artists that performed it."""
    data  = request.get_json(force=True)
    title = str(data.get("title", "")).strip()
    if not title:
        abort(400, "Provide a title")

    url = "https://musicbrainz.org/ws/2/recording/?" + urllib.parse.urlencode({
        "query": f'recording:"{title}"',
        "fmt":   "json",
        "limit": "25",
    })
    req = urllib.request.Request(url, headers={
        "User-Agent": "MusicOrganizer/1.0 (music-organizer)",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        abort(502, f"MusicBrainz error: {exc}")

    artist_map: dict = {}
    for recording in result.get("recordings", []):
        score = int(recording.get("score", 0))
        ac    = recording.get("artist-credit", [])
        if not ac:
            continue
        name = ac[0].get("artist", {}).get("name", "") or ac[0].get("name", "")
        mbid = ac[0].get("artist", {}).get("id", "")
        if not name:
            continue
        key = name.lower()
        if key not in artist_map:
            artist_map[key] = {"name": name, "mbid": mbid, "score_max": score, "count": 1}
        else:
            artist_map[key]["count"] += 1
            if score > artist_map[key]["score_max"]:
                artist_map[key]["score_max"] = score

    artists = sorted(artist_map.values(), key=lambda a: (-a["score_max"], -a["count"]))
    return jsonify({"artists": [{"name": a["name"], "mbid": a["mbid"]} for a in artists[:12]]})


@app.route("/api/search_albums", methods=["POST"])
def api_search_albums():
    """Given title + artist, return albums ranked with the original release first."""
    data   = request.get_json(force=True)
    title  = str(data.get("title",  "")).strip()
    artist = str(data.get("artist", "")).strip()
    if not title or not artist:
        abort(400, "Provide title and artist")

    url = "https://musicbrainz.org/ws/2/recording/?" + urllib.parse.urlencode({
        "query": f'recording:"{title}" AND artist:"{artist}"',
        "fmt":   "json",
        "limit": "25",
    })
    req = urllib.request.Request(url, headers={
        "User-Agent": "MusicOrganizer/1.0 (music-organizer)",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        abort(502, f"MusicBrainz error: {exc}")

    seen_ids: set     = set()
    releases_raw: list = []
    for recording in result.get("recordings", []):
        rec_title  = recording.get("title", "")
        ac         = recording.get("artist-credit", [])
        rec_artist = (ac[0].get("artist", {}).get("name", "") or ac[0].get("name", "")) if ac else ""
        for rel in recording.get("releases", []):
            rel_id = rel.get("id", "")
            if not rel_id or rel_id in seen_ids:
                continue
            seen_ids.add(rel_id)

            rg              = rel.get("release-group", {})
            primary_type    = rg.get("primary-type", "")
            secondary_types = rg.get("secondary-types", [])
            album_title     = rel.get("title", "")
            date            = rel.get("date", "") or ""
            year            = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else 9999

            is_compilation = (
                "Compilation" in secondary_types
                or bool(_COMPILATION_RE.search(album_title))
            )
            if is_compilation:
                orig_score = 20
            elif primary_type != "Album":
                orig_score = 10   # Singles, EPs, etc.
            else:
                orig_score = 0    # Pure studio album

            releases_raw.append({
                "title":        rec_title,
                "artist":       rec_artist,
                "album":        album_title,
                "release_mbid": rel_id,
                "year":         year,
                "primary_type": primary_type,
                "orig_score":   orig_score,
            })

    # Sort: original albums first, then by year ascending (oldest = most likely original)
    releases_raw.sort(key=lambda r: (r["orig_score"], r["year"]))

    seen_titles: set = set()
    albums: list = []
    for r in releases_raw:
        key = r["album"].lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        albums.append({
            "title":        r["title"],
            "artist":       r["artist"],
            "album":        r["album"],
            "release_mbid": r["release_mbid"],
            "year":         r["year"] if r["year"] != 9999 else None,
            "primary_type": r["primary_type"],
        })

    return jsonify({"albums": albums[:15]})


_MBID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
)

# Simple in-memory cache for cover art lookups
_cover_art_cache: dict = {}  # key -> url or None


@app.route("/api/cover_art")
def api_cover_art():
    """Return a cover art URL for the given artist+album or MusicBrainz release MBID."""
    mbid   = request.args.get("mbid",   "").strip()
    artist = request.args.get("artist", "").strip()
    album  = request.args.get("album",  "").strip()

    # Validate MBID to prevent injection
    if mbid and not _MBID_RE.match(mbid.lower()):
        mbid = ""

    cache_key = mbid or f"{artist.lower()}|{album.lower()}"
    if not cache_key or cache_key == "|":
        return jsonify({"url": None})

    if cache_key in _cover_art_cache:
        return jsonify({"url": _cover_art_cache[cache_key]})

    # If we don't already have an MBID, search MusicBrainz releases
    if not mbid:
        parts = []
        if artist:
            parts.append(f'artist:"{artist}"')
        if album:
            parts.append(f'release:"{album}"')
        if not parts:
            _cover_art_cache[cache_key] = None
            return jsonify({"url": None})

        query = " AND ".join(parts)
        url = "https://musicbrainz.org/ws/2/release/?" + urllib.parse.urlencode({
            "query": query,
            "fmt":   "json",
            "limit": "5",
        })
        req = urllib.request.Request(url, headers={
            "User-Agent": "MusicOrganizer/1.0 (music-organizer)",
        })
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except Exception:
            _cover_art_cache[cache_key] = None
            return jsonify({"url": None})

        for release in result.get("releases", []):
            candidate_mbid = release.get("id", "")
            if candidate_mbid and _MBID_RE.match(candidate_mbid):
                mbid = candidate_mbid
                break

    if not mbid:
        _cover_art_cache[cache_key] = None
        return jsonify({"url": None})

    cover_url = f"https://coverartarchive.org/release/{mbid}/front/250"
    _cover_art_cache[cache_key] = cover_url
    return jsonify({"url": cover_url, "mbid": mbid})


# ─── Pre-fetch next card in background ────────────────────────────────────────

def _prefetch_next(exclude: str | None = None):
    """Background thread: fingerprint the next pending file(s) so they're ready.

    ``exclude`` is the path currently being shown to the user — skip it so we
    pre-fetch files the user hasn't seen yet.
    """
    all_f = _all_audio_files()
    state = _load_state()
    accepted = set(state.get("accepted", []))
    skipped  = set(state.get("skipped", []))
    pending  = [f for f in all_f if f not in accepted and f not in skipped and f != exclude]
    for candidate in pending[:2]:
        with _cache_lock:
            if candidate in _fingerprint_cache:
                continue
        path = Path(candidate)
        duration, fp = get_fingerprint(path)
        candidates = []
        if duration and fp and API_KEY:
            candidates = lookup_acoustid(duration, fp, API_KEY)
        existing = get_existing_tags(path)
        with _cache_lock:
            _fingerprint_cache[candidate] = {"existing": existing, "candidates": candidates}


def _trigger_prefetch(exclude: str | None = None) -> None:
    """Start a daemon thread to pre-fetch the next pending file(s)."""
    threading.Thread(target=_prefetch_next, kwargs={"exclude": exclude}, daemon=True).start()


# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser, time

    if not SOURCE_FOLDER:
        print("Set SOURCE_FOLDER in .env or as environment variable before starting.")
        raise SystemExit(1)

    print(f"Source : {SOURCE_FOLDER}")
    print(f"Output : {OUTPUT_FOLDER}")
    print("Starting pre-fetch…")
    _trigger_prefetch()

    port = int(os.environ.get("WEB_PORT", 5050))
    print(f"Open http://localhost:{port}  in your browser")

    # open browser after a short delay
    def _open():
        time.sleep(1.2)
        webbrowser.open(f"http://localhost:{port}")
    threading.Thread(target=_open, daemon=True).start()

    app.run(host="0.0.0.0", port=port, debug=False)
