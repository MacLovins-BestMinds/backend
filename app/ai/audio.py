"""Перегонка записи из приложения (m4a/AAC) через ffmpeg: в WAV 16 кГц моно и в образец голоса (mp3)."""

import asyncio
import tempfile
from pathlib import Path


class AudioConversionError(RuntimeError):
    pass


async def _ffmpeg(data: bytes, *output_args: str) -> bytes:
    # m4a хранит moov-атом в конце файла, поэтому ffmpeg читает из файла, а не из пайпа
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "input"
        src.write_bytes(data)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(src), *output_args, "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        out, err = await proc.communicate()
    if proc.returncode != 0 or not out:
        reason = err.decode(errors="replace").strip() or "ffmpeg failed"
        # размер и первые байты помогают понять, что прислал клиент (webm/mp4/обрезанный файл)
        raise AudioConversionError(f"{reason}; size={len(data)} head={data[:16].hex()}")
    return out


async def to_wav16k(data: bytes) -> bytes:
    return await _ffmpeg(data, "-ac", "1", "-ar", "16000", "-f", "wav")


async def to_voice_sample(data: bytes, max_sec: int = 180) -> bytes:
    """Образец для клонирования голоса: mp3 44,1 кГц моно, не длиннее max_sec (больше клону не нужно)."""
    return await _ffmpeg(data, "-t", str(max_sec), "-ac", "1", "-ar", "44100", "-b:a", "128k", "-f", "mp3")
