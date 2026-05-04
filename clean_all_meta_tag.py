import os
from pathlib import Path
from dotenv import load_dotenv
from mutagen import File
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, ID3NoHeaderError
from mutagen.mp4 import MP4
from mutagen.flac import FLAC
from mutagen.ogg import OggFileType



def clean_and_tag_file(file_path: Path, artist: str, album: str, title: str):
    audio = File(file_path, easy=True)
    if audio is None:
        print(f"  [SKIP] Formato non supportato: {file_path.name}")
        return

    # Elimina tutti i tag esistenti
    audio.delete()
    audio.save()

    # Ricarica e imposta i nuovi tag (easy=True supporta chiavi generiche)
    audio = File(file_path, easy=True)
    if audio is None:
        return

    audio["title"] = [title]
    audio["artist"] = [artist]
    audio["album"] = [album]

    audio.save()
    print(f"  [OK] {artist} / {album} / {title}")


def process_folder(output_folder: Path):
    audio_extensions = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wma"}
    files = sorted(output_folder.rglob("*"))
    for file_path in files:
        if file_path.suffix.lower() not in audio_extensions:
            continue

        # Struttura attesa: <OUTPUT_FOLDER>/<Artista>/<Album>/<NomeCanzone.ext>
        try:
            relative = file_path.relative_to(output_folder)
            parts = relative.parts
            if len(parts) != 3:
                print(f"  [SKIP] Path non conforme (atteso Artista/Album/Canzone): {relative}")
                continue
            artist, album, filename = parts
        except ValueError:
            print(f"  [SKIP] Impossibile determinare il path relativo: {file_path}")
            continue

        title = file_path.stem

        clean_and_tag_file(file_path, artist, album, title)


def main():
    load_dotenv()
    raw_folder = os.environ.get("OUTPUT_FOLDER")
    if not raw_folder:
        raise EnvironmentError("La variabile d'ambiente OUTPUT_FOLDER non è impostata.")

    output_folder = Path(raw_folder).expanduser().resolve()
    if not output_folder.is_dir():
        raise NotADirectoryError(f"OUTPUT_FOLDER non è una directory valida: {output_folder}")

    print(f"Cartella: {output_folder}")
    process_folder(output_folder)
    print("Completato.")


if __name__ == "__main__":
    main()
