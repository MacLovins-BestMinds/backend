"""Прогоняет эталонные записи через распознавание и анализ подачи и печатает, что найдено, рядом с эталоном.

Запуск из backend/:  PYTHONPATH=. python tools/delivery_check/check.py
Нужны ключи распознавания в .env; повторные прогоны берут расшифровку из кэша (.ai_cache).
"""

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from texts import RECORDINGS

from app.ai.audio import to_wav16k
from app.ai.delivery_metrics import analyze
from app.ai.stt import transcribe

KINDS = ("filler", "long_pause", "hesitation", "good_pause", "pace")


async def main() -> None:
    for name, spec in RECORDINGS.items():
        path = HERE / "recordings" / f"{name}.m4a"
        if not path.exists():
            sys.exit("Нет записей — сначала: python tools/delivery_check/make_recordings.py")
        transcript = await transcribe(await to_wav16k(path.read_bytes()))
        result = analyze(transcript, [], 60, 180)
        found: dict[str, list[str]] = {}
        for event in result.events:
            found.setdefault(event.type, []).append(event.text)
        print(f"\n━━━ {name}   эталон: {spec['expect']}")
        for kind in KINDS:
            if kind in found:
                print(f"  {kind:11} ({len(found[kind])}): {found[kind]}")
        s = result.score
        print(
            f"  темп {result.metrics.wpm} сл/мин | подача {s.total}: паразиты {s.fillers}, темп {s.pace}, паузы {s.pauses}"
        )


if __name__ == "__main__":
    asyncio.run(main())
