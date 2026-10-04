"""Статические переводы тем: content/topics.json → content/topics.ru.json и content/topics.ro.json.

Запуск (нужен GEMINI_API_KEY, удобнее всего в контейнере):
    docker exec backend-backend-1 python tools/translate_topics.py          # ru и ro
    docker exec backend-backend-1 python tools/translate_topics.py ro       # только румынский

Повторный запуск переводит только новые темы и темы, чей английский текст изменился (source_hash);
остальные записи, в том числе поправленные руками, не трогает. Темы, которых больше нет в topics.json, удаляются.
Перевод — Gemini с промптом app/ai/prompts/localize_case.md; ответы кэшируются на диске (.ai_cache).
Бэкенд читает файлы при старте (app/game/content.py) — после обновления перезапустите сервер.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel  # noqa: E402

from app.ai import llm  # noqa: E402
from app.core.lang import language_name  # noqa: E402
from app.game import content  # noqa: E402

LANGS = ("ru", "ro")
PARALLEL = 4  # переводов одновременно — не упираться в лимиты Gemini
# Аудиторий всего четыре — их названия заданы, чтобы у всех тем они звучали одинаково
AUDIENCES = {
    "ru": {"general public": "широкая публика", "business people": "бизнесмены", "contest jury": "жюри конкурса",
           "teachers": "учителя"},
    "ro": {"general public": "publicul larg", "business people": "oameni de afaceri", "contest jury": "juriul concursului",
           "teachers": "profesori"},
}  # fmt: skip


class CaseTexts(BaseModel):
    """Тексты темы, которые видит игрок."""

    title: str
    brief: str
    audience: str
    summary: str = ""
    category: str = ""


def _harmonize(out: dict[str, dict[str, str]], lang: str) -> None:
    """Одна и та же аудитория и категория — одинаково во всех темах: аудитория из AUDIENCES,
    категория — как в первой теме этой категории (по порядку topics.json)."""
    canonical: dict[tuple[str, str], str] = {
        ("audience", english): value for english, value in AUDIENCES.get(lang, {}).items()
    }
    for cid, entry in out.items():
        texts = content.english_texts(cid)
        for field in ("audience", "category"):
            if field in entry:
                entry[field] = canonical.setdefault((field, texts[field]), entry[field])


def _is_fresh(entry: dict[str, str] | None, texts: dict[str, str]) -> bool:
    """Перевод есть, сделан с текущего английского текста и не пропускает ни одного непустого поля."""
    if not entry or entry.get("source_hash") != content.source_hash(texts):
        return False
    return all(entry.get(field) for field, english in texts.items() if english)


async def _translate(texts: dict[str, str], lang: str, gate: asyncio.Semaphore) -> dict[str, str]:
    source = CaseTexts(**texts)
    async with gate:
        result = await llm.generate(
            "localize_case",
            CaseTexts,
            language=language_name(lang),
            texts=json.dumps(source.model_dump(), ensure_ascii=False, indent=1),
        )
    entry = {field: value.strip() for field, value in result.model_dump().items() if value.strip()}
    return {**entry, "source_hash": content.source_hash(texts)}


async def update(lang: str) -> int:
    """Обновить content/topics.<lang>.json; вернуть число тем, которые перевести не удалось."""
    path = content.translations_path(lang)
    existing: dict[str, dict[str, str]] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    ids = list(content._topics())  # порядок как в topics.json
    todo = [cid for cid in ids if not _is_fresh(existing.get(cid), content.english_texts(cid))]
    gate = asyncio.Semaphore(PARALLEL)
    results = await asyncio.gather(
        *(_translate(content.english_texts(cid), lang, gate) for cid in todo), return_exceptions=True
    )
    fresh = dict(zip(todo, results, strict=True))
    failed = [cid for cid, result in fresh.items() if isinstance(result, BaseException)]
    for cid in failed:
        print(f"  {lang}/{cid}: не перевелось — {type(fresh[cid]).__name__}: {fresh[cid]}", file=sys.stderr)

    out: dict[str, dict[str, str]] = {}
    for cid in ids:
        if isinstance(result := fresh.get(cid), dict):
            out[cid] = result
        elif cid in existing:
            out[cid] = dict(existing[cid])  # не перевелось — прежняя запись; устаревшую бэкенд всё равно не покажет
    _harmonize(out, lang)
    removed = len(set(existing) - set(ids))
    if out != existing:
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{path.name}: тем {len(ids)}, переведено {len(todo) - len(failed)}, не вышло {len(failed)}, удалено {removed}")
    return len(failed)


async def main(langs: list[str]) -> int:
    failed = 0
    for lang in langs:
        failed += await update(lang)
    return 1 if failed else 0


if __name__ == "__main__":
    langs = sys.argv[1:] or list(LANGS)
    unknown = [lang for lang in langs if lang not in LANGS]
    if unknown:
        sys.exit(f"Языки: {', '.join(LANGS)}; неизвестные: {', '.join(unknown)}")
    sys.exit(asyncio.run(main(langs)))
