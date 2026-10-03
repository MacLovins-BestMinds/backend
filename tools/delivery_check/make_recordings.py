"""Генерирует эталонные записи голосом macOS (say -v Samantha) в tools/delivery_check/recordings/."""

import subprocess
from pathlib import Path

from texts import RECORDINGS

OUT = Path(__file__).parent / "recordings"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, spec in RECORDINGS.items():
        aiff, m4a = OUT / f"{name}.aiff", OUT / f"{name}.m4a"
        subprocess.run(["say", "-v", "Samantha", "-r", str(spec["rate"]), "-o", str(aiff), spec["text"]], check=True)
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(aiff), "-c:a", "aac", "-b:a", "96k", str(m4a)], check=True
        )
        aiff.unlink()
        print("записано:", m4a.name)


if __name__ == "__main__":
    main()
