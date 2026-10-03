"""Перегонка записи из приложения (m4a/AAC) в WAV 16 кГц моно через ffmpeg."""

import asyncio
import tempfile
from pathlib import Path


class AudioConversionError(RuntimeError):
    pass


async def to_wav16k(data: bytes) -> bytes:
    # m4a хранит moov-атом в конце файла, поэтому ffmpeg читает из файла, а не из пайпа
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "input"
        src.write_bytes(data)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-i", str(src), "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        wav, err = await proc.communicate()
    if proc.returncode != 0 or not wav:
        raise AudioConversionError(err.decode(errors="replace").strip() or "ffmpeg failed")
    return wav
