#!/usr/bin/env python3
"""Music Organizer — Graphical User Interface (customtkinter)."""

import os
import sys
import shutil
import queue
import subprocess
import threading
import tkinter as tk
import tkinter.ttk as ttk
from pathlib import Path
from tkinter import filedialog, messagebox

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import customtkinter as ctk
except ImportError:
    print("Error: customtkinter not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

from music_organizer import (
    AUDIO_EXTENSIONS,
    get_existing_tags,
    get_fingerprint,
    lookup_acoustid,
    organize_file,
    sanitize,
)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


# ─── Metadata dialog ──────────────────────────────────────────────────────────

class MetadataDialog(ctk.CTkToplevel):
    """Modal window shown for each audio file: fingerprint, propose, user picks."""

    def __init__(
        self,
        parent,
        file_path: Path,
        existing: dict,
        api_key: str,
        output_dir: Path,
        copy: bool = False,
        dry_run: bool = False,
    ):
        super().__init__(parent)
        self.title(f"Metadata — {file_path.name}")
        self.geometry("780x610")
        self.minsize(620, 520)
        self.update_idletasks()
        self.grab_set()
        self.lift()
        self.focus_force()
        self.protocol("WM_DELETE_WINDOW", self._on_skip)

        self.file_path = file_path
        self.existing = existing
        self.api_key = api_key
        self.output_dir = output_dir
        self.copy = copy
        self.dry_run = dry_run

        self.result: dict | None = None
        self._candidates: list = []
        self._radio_var = tk.IntVar(value=-1)
        self._spinner_idx = 0
        self._result_q: queue.Queue = queue.Queue()

        self._build_ui()
        self._start_fingerprinting()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # Header
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 0))
        hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            hdr,
            text=f"🎵  {self.file_path.name}",
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        self._status_lbl = ctk.CTkLabel(
            hdr,
            text="",
            font=ctk.CTkFont(size=13),
            text_color=("gray50", "gray60"),
            anchor="e",
        )
        self._status_lbl.grid(row=0, column=1, sticky="e")

        # Divider
        ctk.CTkFrame(self, height=1, fg_color=("gray70", "gray30")).grid(
            row=1, column=0, sticky="ew", padx=0, pady=8
        )

        # ── Player bar ────────────────────────────────────────────────────────
        self._player_proc: subprocess.Popen | None = None
        player_bar = ctk.CTkFrame(self, fg_color=("gray85", "gray20"), corner_radius=8)
        player_bar.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 6))

        ctk.CTkLabel(
            player_bar, text="🔊  Preview:", font=ctk.CTkFont(weight="bold")
        ).pack(side="left", padx=(10, 6), pady=6)

        self._play_btn = ctk.CTkButton(
            player_bar, text="▶  Play", width=90, command=self._toggle_play
        )
        self._play_btn.pack(side="left", padx=4, pady=6)

        ctk.CTkButton(
            player_bar, text="⏹  Stop", width=80,
            fg_color=("gray70", "gray35"),
            hover_color=("gray60", "gray45"),
            command=self._stop_player,
        ).pack(side="left", padx=4, pady=6)

        self._player_status_lbl = ctk.CTkLabel(
            player_bar, text="",
            text_color=("gray40", "gray60"),
            font=ctk.CTkFont(size=12),
        )
        self._player_status_lbl.pack(side="left", padx=8, pady=6)

        # Scrollable suggestions
        self._cand_frame = ctk.CTkScrollableFrame(
            self, fg_color="transparent", label_text="Suggestions"
        )
        self._cand_frame.grid(row=3, column=0, sticky="nsew", padx=16, pady=0)
        self._cand_frame.grid_columnconfigure(0, weight=1)

        # Manual entry
        manual = ctk.CTkFrame(self, fg_color=("gray88", "gray18"), corner_radius=10)
        manual.grid(row=4, column=0, sticky="ew", padx=16, pady=(10, 0))
        manual.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            manual, text="✏  Manual entry", font=ctk.CTkFont(weight="bold")
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(8, 2))

        self._title_var = tk.StringVar(value=self.existing.get("title", ""))
        self._artist_var = tk.StringVar(value=self.existing.get("artist", ""))
        self._album_var = tk.StringVar(value=self.existing.get("album", ""))

        for row_i, (label, var) in enumerate(
            [("Title", self._title_var), ("Artist", self._artist_var), ("Album", self._album_var)],
            start=1,
        ):
            var.trace_add("write", self._update_preview)
            ctk.CTkLabel(manual, text=f"{label}:").grid(
                row=row_i, column=0, sticky="w", padx=(12, 4), pady=3
            )
            ctk.CTkEntry(manual, textvariable=var).grid(
                row=row_i, column=1, sticky="ew", padx=(0, 12), pady=3
            )

        # Preview path
        prev = ctk.CTkFrame(self, fg_color="transparent")
        prev.grid(row=5, column=0, sticky="ew", padx=16, pady=(6, 0))
        prev.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(prev, text="Path:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self._preview_lbl = ctk.CTkLabel(
            prev, text="", anchor="w",
            text_color=("gray30", "gray70"), font=ctk.CTkFont(size=12),
        )
        self._preview_lbl.grid(row=0, column=1, sticky="ew")

        # Buttons
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=6, column=0, sticky="ew", padx=16, pady=12)

        ctk.CTkButton(
            btns, text="⏭  Skip", width=120,
            fg_color=("gray70", "gray30"),
            hover_color=("gray60", "gray40"),
            command=self._on_skip,
        ).pack(side="left")

        ctk.CTkButton(
            btns, text="Confirm  →", width=160, command=self._on_confirm
        ).pack(side="right")

        self._update_preview()

    # ── Fingerprinting ────────────────────────────────────────────────────────

    def _start_fingerprinting(self) -> None:
        self._tick_spinner()

        def worker() -> None:
            duration, fp = get_fingerprint(self.file_path)
            candidates: list = []
            if duration and fp and self.api_key:
                candidates = lookup_acoustid(duration, fp, self.api_key)
            self._result_q.put(candidates)

        threading.Thread(target=worker, daemon=True).start()
        self._poll_result()

    def _tick_spinner(self) -> None:
        if not self.winfo_exists():
            return
        if not self._result_q.empty():
            return
        frame = SPINNER[self._spinner_idx % len(SPINNER)]
        self._status_lbl.configure(text=f"{frame} Analyzing…")
        self._spinner_idx += 1
        self.after(100, self._tick_spinner)

    def _poll_result(self) -> None:
        try:
            candidates = self._result_q.get_nowait()
        except queue.Empty:
            self.after(150, self._poll_result)
            return

        self._candidates = candidates
        n = len(candidates)
        if n:
            self._status_lbl.configure(
                text=f"✓ {n} match{'es' if n > 1 else ''} found",
                text_color=("#22c55e", "#4ade80"),
            )
        else:
            self._status_lbl.configure(
                text="No matches found",
                text_color=("#f97316", "#fb923c"),
            )
        self._populate_candidates()

    # ── Candidate rows ────────────────────────────────────────────────────────

    def _populate_candidates(self) -> None:
        parent = self._cand_frame

        if any(self.existing.values()):
            self._add_row(parent, 0, "Existing tags", self.existing)

        for i, c in enumerate(self._candidates, start=1):
            self._add_row(parent, i, f"AcoustID  {c['score'] * 100:.0f}%", c)

        if self._candidates:
            self._select(1, self._candidates[0])
        elif any(self.existing.values()):
            self._select(0, self.existing)

    def _add_row(self, parent, idx: int, label: str, data: dict) -> None:
        row = ctk.CTkFrame(parent, fg_color=("gray85", "gray22"), corner_radius=8)
        row.grid(sticky="ew", pady=3, padx=2)
        row.grid_columnconfigure(0, weight=1)

        ctk.CTkRadioButton(
            row,
            text=label,
            variable=self._radio_var,
            value=idx,
            font=ctk.CTkFont(weight="bold"),
            command=lambda d=data: self._select(idx, d),
        ).grid(row=0, column=0, padx=(10, 4), pady=(7, 2), sticky="w")

        info = (
            f"{data.get('artist') or '—'}  ·  "
            f"{data.get('album') or '—'}  ·  "
            f"{data.get('title') or '—'}"
        )
        ctk.CTkLabel(
            row, text=info, anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=("gray35", "gray65"),
        ).grid(row=1, column=0, padx=(38, 10), pady=(0, 7), sticky="w")

    def _select(self, idx: int, data: dict) -> None:
        self._radio_var.set(idx)
        self._title_var.set(data.get("title", ""))
        self._artist_var.set(data.get("artist", ""))
        self._album_var.set(data.get("album", ""))

    # ── Preview ───────────────────────────────────────────────────────────────

    def _update_preview(self, *_) -> None:
        artist = self._artist_var.get()
        album = self._album_var.get()
        title = self._title_var.get()
        ext = self.file_path.suffix.lower()
        path = (
            Path(sanitize(artist or "Unknown Artist"))
            / sanitize(album or "Unknown Album")
            / f"{sanitize(title or self.file_path.stem)}{ext}"
        )
        self._preview_lbl.configure(text=f"…/{path}")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _on_skip(self) -> None:
        self._stop_player()
        self.result = None
        self.destroy()

    def _on_confirm(self) -> None:
        self._stop_player()
        self.result = {
            "title": self._title_var.get().strip(),
            "artist": self._artist_var.get().strip(),
            "album": self._album_var.get().strip(),
        }
        self.destroy()

    # ── Player ────────────────────────────────────────────────────────────────

    def _find_player_cmd(self) -> list[str] | None:
        """Return the command prefix for the first available CLI audio player."""
        candidates = [
            ["ffplay", "-nodisp", "-autoexit"],
            ["mpv", "--no-terminal", "--really-quiet"],
            ["mplayer", "-really-quiet"],
        ]
        for cmd in candidates:
            if shutil.which(cmd[0]):
                return cmd
        return None

    def _toggle_play(self) -> None:
        if self._player_proc and self._player_proc.poll() is None:
            self._stop_player()
        else:
            self._start_player()

    def _start_player(self) -> None:
        cmd_base = self._find_player_cmd()
        if cmd_base is None:
            self._player_status_lbl.configure(text="ffplay/mpv not found")
            return
        self._player_proc = subprocess.Popen(
            cmd_base + [str(self.file_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._play_btn.configure(text="⏸  Pause")
        self._player_status_lbl.configure(text="▶ Playing…")
        self._poll_player()

    def _stop_player(self) -> None:
        if self._player_proc and self._player_proc.poll() is None:
            self._player_proc.terminate()
        self._player_proc = None
        if self.winfo_exists():
            self._play_btn.configure(text="▶  Play")
            self._player_status_lbl.configure(text="")

    def _poll_player(self) -> None:
        if not self.winfo_exists():
            return
        if self._player_proc and self._player_proc.poll() is not None:
            self._player_proc = None
            self._play_btn.configure(text="▶  Play")
            self._player_status_lbl.configure(text="")
            return
        if self._player_proc:
            self.after(400, self._poll_player)

    def wait_result(self) -> dict | None:
        self.wait_window()
        return self.result


# ─── Main window ──────────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Music Organizer")
        self.geometry("960x660")
        self.minsize(720, 520)

        self._audio_files: list[Path] = []
        self._current_idx: int = 0

        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # ── Settings panel ────────────────────────────────────────────────────
        settings = ctk.CTkFrame(self)
        settings.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 6))
        settings.grid_columnconfigure(1, weight=1)

        # Source
        ctk.CTkLabel(settings, text="Source folder:").grid(
            row=0, column=0, sticky="w", padx=(12, 4), pady=6
        )
        self._source_var = tk.StringVar(value=os.environ.get("SOURCE_FOLDER", ""))
        ctk.CTkEntry(settings, textvariable=self._source_var).grid(
            row=0, column=1, sticky="ew", padx=4, pady=6
        )
        ctk.CTkButton(
            settings, text="Browse…", width=90, command=self._browse_source
        ).grid(row=0, column=2, padx=(4, 12), pady=6)

        # Output
        ctk.CTkLabel(settings, text="Output folder:").grid(
            row=1, column=0, sticky="w", padx=(12, 4), pady=6
        )
        self._output_var = tk.StringVar(value=os.environ.get("OUTPUT_FOLDER", ""))
        ctk.CTkEntry(
            settings, textvariable=self._output_var,
            placeholder_text="(same as source)"
        ).grid(row=1, column=1, sticky="ew", padx=4, pady=6)
        ctk.CTkButton(
            settings, text="Browse…", width=90, command=self._browse_output
        ).grid(row=1, column=2, padx=(4, 12), pady=6)

        # API Key
        ctk.CTkLabel(settings, text="AcoustID key:").grid(
            row=2, column=0, sticky="w", padx=(12, 4), pady=6
        )
        self._apikey_var = tk.StringVar(value=os.environ.get("ACOUSTID_API_KEY", ""))
        self._apikey_entry = ctk.CTkEntry(
            settings, textvariable=self._apikey_var, show="•"
        )
        self._apikey_entry.grid(row=2, column=1, sticky="ew", padx=4, pady=6)
        self._show_btn = ctk.CTkButton(
            settings, text="Show", width=90, command=self._toggle_key
        )
        self._show_btn.grid(row=2, column=2, padx=(4, 12), pady=6)
        self._key_hidden = True

        # Options row
        opts = ctk.CTkFrame(settings, fg_color="transparent")
        opts.grid(row=3, column=0, columnspan=3, sticky="ew", padx=8, pady=(2, 10))

        self._copy_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            opts, text="Copy (don't move)", variable=self._copy_var
        ).pack(side="left", padx=(4, 20))

        self._dryrun_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            opts, text="Dry run (preview only)", variable=self._dryrun_var
        ).pack(side="left")

        self._scan_btn = ctk.CTkButton(
            opts, text="🔍  Scan folder", width=150, command=self._scan
        )
        self._scan_btn.pack(side="right", padx=(0, 4))

        # ── Divider ───────────────────────────────────────────────────────────
        ctk.CTkFrame(self, height=1, fg_color=("gray70", "gray30")).grid(
            row=1, column=0, sticky="ew"
        )

        # ── File list ─────────────────────────────────────────────────────────
        list_frame = ctk.CTkFrame(self, fg_color="transparent")
        list_frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=(8, 0))
        list_frame.grid_columnconfigure(0, weight=1)
        list_frame.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            list_frame, text="Files", font=ctk.CTkFont(weight="bold"), anchor="w"
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))

        tree_wrap = ctk.CTkFrame(list_frame, fg_color=("gray80", "gray15"))
        tree_wrap.grid(row=1, column=0, sticky="nsew")
        tree_wrap.grid_columnconfigure(0, weight=1)
        tree_wrap.grid_rowconfigure(0, weight=1)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "MO.Treeview",
            background="#1e1e1e",
            foreground="#e0e0e0",
            fieldbackground="#1e1e1e",
            rowheight=28,
            borderwidth=0,
            font=("Helvetica", 11),
        )
        style.configure(
            "MO.Treeview.Heading",
            background="#141414",
            foreground="#aaaaaa",
            borderwidth=0,
            relief="flat",
            font=("Helvetica", 11, "bold"),
        )
        style.map("MO.Treeview", background=[("selected", "#1f6feb")])

        self._tree = ttk.Treeview(
            tree_wrap,
            style="MO.Treeview",
            columns=("file", "status", "destination"),
            show="headings",
            selectmode="browse",
        )
        self._tree.heading("file", text="File")
        self._tree.heading("status", text="Status")
        self._tree.heading("destination", text="Destination")
        self._tree.column("file", width=240, minwidth=140, anchor="w")
        self._tree.column("status", width=140, minwidth=100, anchor="center")
        self._tree.column("destination", width=500, minwidth=200, anchor="w")
        self._tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self._tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self._tree.configure(yscrollcommand=vsb.set)

        # ── Bottom bar ────────────────────────────────────────────────────────
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=3, column=0, sticky="ew", padx=16, pady=(6, 14))
        bottom.grid_columnconfigure(0, weight=1)

        self._progress = ctk.CTkProgressBar(bottom, height=14)
        self._progress.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self._progress.set(0)

        bar_info = ctk.CTkFrame(bottom, fg_color="transparent")
        bar_info.grid(row=1, column=0, sticky="ew")

        self._progress_lbl = ctk.CTkLabel(
            bar_info, text="Scan a folder to begin.",
            text_color=("gray40", "gray60"), anchor="w",
        )
        self._progress_lbl.pack(side="left")

        self._start_btn = ctk.CTkButton(
            bar_info, text="▶  Start", width=140,
            state="disabled", command=self._start_processing,
        )
        self._start_btn.pack(side="right")

    # ── Folder helpers ────────────────────────────────────────────────────────

    def _browse_source(self) -> None:
        path = filedialog.askdirectory(title="Select source folder")
        if path:
            self._source_var.set(path)

    def _browse_output(self) -> None:
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self._output_var.set(path)

    def _toggle_key(self) -> None:
        if self._key_hidden:
            self._apikey_entry.configure(show="")
            self._show_btn.configure(text="Hide")
            self._key_hidden = False
        else:
            self._apikey_entry.configure(show="•")
            self._show_btn.configure(text="Show")
            self._key_hidden = True

    # ── Scan ──────────────────────────────────────────────────────────────────

    def _scan(self) -> None:
        source_str = self._source_var.get().strip()
        if not source_str:
            messagebox.showwarning("Missing folder", "Please select a source folder first.")
            return

        source_path = Path(source_str).expanduser().resolve()
        if not source_path.is_dir():
            messagebox.showerror("Invalid folder", f"'{source_path}' is not a valid directory.")
            return

        self._audio_files = sorted(
            p for p in source_path.rglob("*")
            if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
        )

        # Rebuild list
        for item in self._tree.get_children():
            self._tree.delete(item)
        for f in self._audio_files:
            self._tree.insert("", "end", iid=str(f), values=(f.name, "⏳ Pending", ""))

        total = len(self._audio_files)
        self._current_idx = 0
        self._progress.set(0)

        if total:
            self._progress_lbl.configure(text=f"0 / {total}")
            self._start_btn.configure(state="normal")
            messagebox.showinfo("Scan complete", f"Found {total} audio file(s).\nClick ▶ Start to begin.")
        else:
            self._progress_lbl.configure(text="No audio files found.")
            self._start_btn.configure(state="disabled")
            messagebox.showinfo("Scan complete", "No supported audio files found in that folder.")

    # ── Processing ────────────────────────────────────────────────────────────

    def _start_processing(self) -> None:
        self._start_btn.configure(state="disabled")
        self._process_next()

    def _process_next(self) -> None:
        total = len(self._audio_files)
        if self._current_idx >= total:
            self._progress.set(1)
            self._progress_lbl.configure(text=f"{total} / {total}  —  Done!")
            messagebox.showinfo("Done", f"All {total} file(s) processed!")
            return

        file_path = self._audio_files[self._current_idx]
        self._tree.set(str(file_path), "status", "🔍 Analyzing…")
        self._tree.see(str(file_path))

        existing = get_existing_tags(file_path)
        api_key = self._apikey_var.get().strip()

        output_str = self._output_var.get().strip()
        output_dir = (
            Path(output_str).expanduser().resolve()
            if output_str
            else Path(self._source_var.get()).expanduser().resolve()
        )

        dlg = MetadataDialog(
            self, file_path, existing, api_key, output_dir,
            copy=self._copy_var.get(),
            dry_run=self._dryrun_var.get(),
        )
        result = dlg.wait_result()

        if result is None:
            self._tree.set(str(file_path), "status", "⏭ Skipped")
            self._tree.set(str(file_path), "destination", "")
        else:
            artist = result.get("artist", "")
            album = result.get("album", "")
            title = result.get("title", "")
            dry_run = self._dryrun_var.get()
            try:
                if dry_run:
                    ext = file_path.suffix.lower()
                    dest = (
                        output_dir
                        / sanitize(artist or "Unknown Artist")
                        / sanitize(album or "Unknown Album")
                        / f"{sanitize(title or file_path.stem)}{ext}"
                    )
                    self._tree.set(str(file_path), "status", "👁 Dry run")
                else:
                    dest = organize_file(
                        file_path, artist, album, title, output_dir,
                        copy=self._copy_var.get(),
                    )
                    self._tree.set(str(file_path), "status", "✅ Done")
                self._tree.set(str(file_path), "destination", str(dest))
            except Exception as exc:
                self._tree.set(str(file_path), "status", "❌ Error")
                messagebox.showerror("Error moving file", str(exc))

        self._current_idx += 1
        done = self._current_idx
        self._progress.set(done / total)
        self._progress_lbl.configure(text=f"{done} / {total}")

        # small delay so the UI repaints before next dialog
        self.after(80, self._process_next)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
