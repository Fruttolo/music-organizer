# Music Organizer

Identify and organize music files using audio fingerprinting ([AcoustID](https://acoustid.org/) / [Chromaprint](https://acoustid.org/chromaprint)).  
Files are moved (or copied) into a tidy `Output / Artist / Album / Title.ext` hierarchy.

Three interfaces are available: **CLI**, **Desktop GUI** (customtkinter) and **Web UI** (Flask).

---

## Features

- Audio fingerprinting via Chromaprint (`fpcalc`) + AcoustID online lookup
- Reads existing embedded tags (ID3, Vorbis, MP4, …) as fallback
- Interactive metadata selection per file (AcoustID candidates, existing tags, or manual entry)
- Move **or** copy mode, with optional dry-run preview
- Supports MP3, FLAC, M4A, OGG, WAV, AAC, WMA, Opus, APE, MPC
- Persistent state file so the Web UI survives restarts without re-processing files

---

## Requirements

### System dependency

`fpcalc` must be available on your `PATH`:

| Platform | Command |
|---|---|
| Ubuntu / Debian | `sudo apt install libchromaprint-tools` |
| macOS | `brew install chromaprint` |
| Windows | Download from [acoustid.org/chromaprint](https://acoustid.org/chromaprint) |

### Python packages

```bash
pip install -r requirements.txt
```

Requires **Python 3.8+**.

---

## AcoustID API key

Online fingerprint lookup requires a free AcoustID API key.  
Register at <https://acoustid.org/login> and then provide it in one of these ways:

```bash
# Option 1 — environment variable (recommended)
export ACOUSTID_API_KEY="your_key_here"

# Option 2 — .env file in the project root
echo 'ACOUSTID_API_KEY=your_key_here' > .env

# Option 3 — CLI flag (see below)
```

Without a key the tool still works but skips online lookup and relies on existing embedded tags only.

---

## Usage

### CLI

```bash
python music_organizer.py <source_folder> [output_folder] [options]
```

| Option | Description |
|---|---|
| `source` | Folder containing music files to organize (required) |
| `output` | Destination root folder (default: same as source) |
| `--copy` | Copy files instead of moving them |
| `--dry-run` | Preview what would happen without touching files |
| `--api-key KEY` | AcoustID API key |

**Example:**

```bash
python music_organizer.py ~/Music/Unsorted ~/Music/Organized --copy --api-key YOUR_KEY
```

### Desktop GUI

```bash
python music_organizer_gui.py
```

A dark-themed window (customtkinter) guides you through the same fingerprint-and-select workflow.

### Web UI

```bash
SOURCE_FOLDER=/path/to/music OUTPUT_FOLDER=/path/to/output python music_organizer_web.py
```

Then open <http://localhost:5000> in your browser.  
The interface presents one file at a time in a card-based layout — pick the best metadata, accept, or skip.

| Environment variable | Description |
|---|---|
| `SOURCE_FOLDER` | Folder to scan for audio files (required) |
| `OUTPUT_FOLDER` | Destination folder (default: same as `SOURCE_FOLDER`) |
| `ACOUSTID_API_KEY` | AcoustID API key |

---

## Output structure

```
Output/
└── Artist Name/
    └── Album Name/
        └── Track Title.mp3
```

Characters that are invalid on common filesystems are replaced with `_`.  
Duplicate filenames are disambiguated automatically (`Title (1).mp3`, `Title (2).mp3`, …).

---

## Project structure

```
music_organizer.py        # Core logic + CLI entry point
music_organizer_gui.py    # Desktop GUI (customtkinter)
music_organizer_web.py    # Web UI (Flask)
requirements.txt
web_static/
    index.html            # Front-end for the Web UI
```

---

## License

This project is released under the [MIT License](LICENSE).
