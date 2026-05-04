#!/usr/bin/env python3
"""Music Organizer — identify and organize music files using audio fingerprinting (AcoustID/Chromaprint)."""

import os
import re
import sys
import shutil
import argparse
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import acoustid
except ImportError:
    print("Error: pyacoustid not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

try:
    from mutagen import File as MutagenFile
except ImportError:
    print("Error: mutagen not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
except ImportError:
    print("Error: rich not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

try:
    import questionary
except ImportError:
    print("Error: questionary not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

# ─── Configuration ─────────────────────────────────────────────────────────────

ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY", "")

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".ogg", ".wav",
    ".aac", ".wma", ".opus", ".ape", ".mpc",
}

console = Console()

# ─── Audio Fingerprinting ──────────────────────────────────────────────────────

def get_fingerprint(file_path: Path):
    """Generate a Chromaprint audio fingerprint for the given file.
    Requires fpcalc to be installed (package: chromaprint-tools / chromaprint).
    """
    try:
        duration, fingerprint = acoustid.fingerprint_file(str(file_path))
        return duration, fingerprint
    except acoustid.FingerprintGenerationError as exc:
        msg = str(exc)
        if "fpcalc" in msg.lower():
            console.print(
                "  [red]fpcalc not found.[/red] Install Chromaprint:\n"
                "    Ubuntu/Debian : sudo apt install libchromaprint-tools\n"
                "    macOS         : brew install chromaprint\n"
                "    Windows       : https://acoustid.org/chromaprint"
            )
        else:
            console.print(f"  [red]Fingerprint error:[/red] {exc}")
        return None, None
    except Exception as exc:
        console.print(f"  [red]Unexpected fingerprint error:[/red] {exc}")
        return None, None


def lookup_acoustid(duration: float, fingerprint: str, api_key: str) -> list:
    """Query AcoustID and return up to 5 candidate metadata dicts, sorted by score."""
    try:
        results = acoustid.lookup(
            api_key, fingerprint, duration,
            meta="recordings releases",
        )
    except acoustid.WebServiceError as exc:
        console.print(f"  [red]AcoustID lookup error:[/red] {exc}")
        return []
    except Exception as exc:
        console.print(f"  [red]Unexpected lookup error:[/red] {exc}")
        return []

    if not results or results.get("status") != "ok":
        return []

    candidates: dict = {}
    for result in results.get("results", []):
        score = result.get("score", 0.0)
        for recording in result.get("recordings", []):
            title = recording.get("title", "")
            artists = recording.get("artists", [])
            artist = artists[0].get("name", "") if artists else ""
            if not (title and artist):
                continue
            for release in recording.get("releases", []):
                album = release.get("title", "")
                key = (artist.lower(), album.lower(), title.lower())
                if key not in candidates or score > candidates[key]["score"]:
                    candidates[key] = {
                        "score": score,
                        "title": title,
                        "artist": artist,
                        "album": album,
                    }

    return sorted(candidates.values(), key=lambda x: x["score"], reverse=True)[:5]


# ─── Metadata helpers ─────────────────────────────────────────────────────────

def get_existing_tags(file_path: Path) -> dict:
    """Read embedded ID3/Vorbis/MP4/etc. tags from an audio file."""
    try:
        audio = MutagenFile(str(file_path), easy=True)
        if audio:
            def first(tag: str) -> str:
                val = audio.get(tag, [])
                return str(val[0]).strip() if val else ""
            return {
                "title": first("title"),
                "artist": first("artist"),
                "album": first("album"),
            }
    except Exception:
        pass
    return {"title": "", "artist": "", "album": ""}


def write_tags(file_path: Path, title: str, artist: str, album: str) -> None:
    """Write title/artist/album tags into the audio file using mutagen."""
    try:
        audio = MutagenFile(str(file_path), easy=True)
        if audio is None:
            return
        if title:
            audio["title"] = [title]
        if artist:
            audio["artist"] = [artist]
        if album:
            audio["album"] = [album]
        audio.save()
    except Exception as exc:
        console.print(f"  [yellow]Warning: could not write tags:[/yellow] {exc}")


# ─── File-system helpers ───────────────────────────────────────────────────────

def sanitize(name: str) -> str:
    """Strip/replace filesystem-invalid characters."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(". ")
    return name or "Unknown"


def organize_file(
    source: Path,
    artist: str,
    album: str,
    title: str,
    output_dir: Path,
    copy: bool = False,
) -> Path:
    """Move (or copy) the file to output_dir / artist / album / title.ext."""
    ext = source.suffix.lower()
    dest_dir = (
        output_dir
        / sanitize(artist or "Unknown Artist")
        / sanitize(album or "Unknown Album")
    )
    dest_dir.mkdir(parents=True, exist_ok=True)

    base_name = sanitize(title or source.stem)
    dest = dest_dir / f"{base_name}{ext}"

    # Avoid silently overwriting a different file
    counter = 1
    while dest.exists() and dest.resolve() != source.resolve():
        dest = dest_dir / f"{base_name} ({counter}){ext}"
        counter += 1

    action = shutil.copy2 if copy else shutil.move
    action(str(source), str(dest))
    return dest


# ─── Interactive per-file processing ─────────────────────────────────────────

def process_file(
    file_path: Path,
    output_dir: Path,
    api_key: str,
    copy: bool = False,
    dry_run: bool = False,
) -> bool:
    """Fingerprint, look up, prompt the user, and organize a single audio file."""
    console.rule(f"[bold cyan]{file_path.name}[/bold cyan]")

    existing = get_existing_tags(file_path)

    with console.status("Generating audio fingerprint…", spinner="dots"):
        duration, fp = get_fingerprint(file_path)

    candidates = []
    if duration and fp:
        if api_key:
            with console.status("Looking up on AcoustID…", spinner="dots"):
                candidates = lookup_acoustid(duration, fp, api_key)
        else:
            console.print(
                "  [yellow]No AcoustID API key — skipping online lookup.[/yellow]\n"
                "  Use --api-key or set the ACOUSTID_API_KEY environment variable.\n"
                "  Get a free key at: https://acoustid.org/login"
            )

    # ── Display table ──────────────────────────────────────────────────────────
    table = Table(show_header=True, header_style="bold magenta", expand=False)
    table.add_column("#", width=3, justify="right")
    table.add_column("Source", width=16)
    table.add_column("Title")
    table.add_column("Artist")
    table.add_column("Album")

    if any(existing.values()):
        table.add_row(
            "0", "[yellow]Current tags[/yellow]",
            existing["title"], existing["artist"], existing["album"],
        )

    for i, c in enumerate(candidates, 1):
        pct = f"{c['score'] * 100:.0f}%"
        table.add_row(
            str(i), f"[cyan]AcoustID {pct}[/cyan]",
            c["title"], c["artist"], c["album"],
        )

    console.print(table)

    if not any(existing.values()) and not candidates:
        console.print("  [yellow]No metadata found — use manual entry or skip.[/yellow]")

    # ── Build interactive choices ──────────────────────────────────────────────
    choices = []
    if any(existing.values()):
        label = (
            f"[0] Existing tags:  "
            f"{existing['artist']} / {existing['album']} / {existing['title']}"
        )
        choices.append(questionary.Choice(label, value=existing))

    for i, c in enumerate(candidates, 1):
        label = (
            f"[{i}] AcoustID {c['score']*100:.0f}%:  "
            f"{c['artist']} / {c['album']} / {c['title']}"
        )
        choices.append(questionary.Choice(label, value=c))

    choices += [
        questionary.Choice("[m] Enter manually", value="manual"),
        questionary.Choice("[s] Skip this file", value="skip"),
    ]

    selected = questionary.select("Choose metadata:", choices=choices).ask()

    if selected is None or selected == "skip":
        console.print("  [dim]Skipped.[/dim]")
        return False

    if selected == "manual":
        title  = questionary.text("Title:",  default=existing.get("title",  "")).ask() or ""
        artist = questionary.text("Artist:", default=existing.get("artist", "")).ask() or ""
        album  = questionary.text("Album:",  default=existing.get("album",  "")).ask() or ""
        selected = {"title": title, "artist": artist, "album": album}

    # ── Dry-run preview ────────────────────────────────────────────────────────
    if dry_run:
        ext = file_path.suffix.lower()
        preview = (
            output_dir
            / sanitize(selected.get("artist") or "Unknown Artist")
            / sanitize(selected.get("album")  or "Unknown Album")
            / f"{sanitize(selected.get('title') or file_path.stem)}{ext}"
        )
        console.print(f"  [dim][DRY RUN] Would move to:[/dim] {preview}")
        return True

    # ── Move / copy ────────────────────────────────────────────────────────────
    dest = organize_file(
        file_path,
        selected.get("artist", ""),
        selected.get("album",  ""),
        selected.get("title",  ""),
        output_dir,
        copy=copy,
    )
    write_tags(dest, selected.get("title", ""), selected.get("artist", ""), selected.get("album", ""))
    verb = "Copied" if copy else "Moved"
    console.print(f"  [green]✓ {verb}:[/green] {dest}")
    return True


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Music Organizer — identify audio files via fingerprinting and "
            "organize them as  OUTPUT/Artist/Album/Title.ext"
        )
    )
    parser.add_argument("source", help="Folder containing music files to organize")
    parser.add_argument(
        "output", nargs="?",
        help="Destination root folder (default: same as source)",
    )
    parser.add_argument(
        "--copy", action="store_true",
        help="Copy files instead of moving them",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without actually moving files",
    )
    parser.add_argument(
        "--api-key",
        default=ACOUSTID_API_KEY,
        metavar="KEY",
        help="AcoustID API key (or set ACOUSTID_API_KEY env var)",
    )
    args = parser.parse_args()

    source_path = Path(args.source).expanduser().resolve()
    if not source_path.is_dir():
        console.print(f"[red]Error:[/red] '{source_path}' is not a valid directory.")
        sys.exit(1)

    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else source_path
    )

    mode_label = ("Copy" if args.copy else "Move") + (" [dim](dry run)[/dim]" if args.dry_run else "")
    console.print(
        Panel(
            f"  Source : {source_path}\n"
            f"  Output : {output_path}\n"
            f"  Mode   : {mode_label}",
            title="[bold blue]Music Organizer[/bold blue]",
            expand=False,
        )
    )

    audio_files = sorted(
        p for p in source_path.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )

    if not audio_files:
        console.print("[yellow]No audio files found in the specified folder.[/yellow]")
        sys.exit(0)

    console.print(f"Found [bold]{len(audio_files)}[/bold] audio file(s).\n")

    processed = skipped = 0
    try:
        for file_path in audio_files:
            ok = process_file(
                file_path, output_path, args.api_key,
                copy=args.copy, dry_run=args.dry_run,
            )
            if ok:
                processed += 1
            else:
                skipped += 1
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")

    console.print(
        f"\n[bold green]Done![/bold green]  "
        f"Processed: [green]{processed}[/green]   "
        f"Skipped: [yellow]{skipped}[/yellow]"
    )


if __name__ == "__main__":
    main()
