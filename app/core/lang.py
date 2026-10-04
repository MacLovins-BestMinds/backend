"""Языки приложения: en | ru | ro.

- язык речи — его определяет распознавание по записи (app/ai/stt.py). Он решает всё о выступлении:
  расшифровку, вопросы и голос жюри, оценки, советы и подсказки зала;
- язык интерфейса — заголовок Accept-Language (или поле lang раунда). Только тексты тем, ошибок и прогресса,
  на разбор выступления он не влияет.
Всё неподдерживаемое → язык по умолчанию: STT_LANGUAGE (по умолчанию en).
"""

from collections.abc import Mapping
from typing import Annotated, Literal

from fastapi import Depends, Header

type Lang = Literal["en", "ru", "ro"]
LANGS = ("en", "ru", "ro")
LANGUAGE_NAMES = {"en": "English", "ru": "Russian", "ro": "Romanian"}
# Scribe отдаёт ISO 639-3 («eng», «rus», «ron»), Whisper — название языка («english»)
_ALIASES = {
    "eng": "en",
    "english": "en",
    "rus": "ru",
    "russian": "ru",
    "ron": "ro",
    "rum": "ro",
    "mol": "ro",
    "romanian": "ro",
    "moldavian": "ro",
    "moldovan": "ro",
}


def normalize_lang(code: str | None) -> str | None:
    """«ru-RU», «rus», «Russian» → «ru»; пусто или язык не поддерживается → None."""
    if not code:
        return None
    primary = code.strip().lower().replace("_", "-").split("-")[0]
    primary = _ALIASES.get(primary, primary)
    return primary if primary in LANGS else None


def default_lang() -> str:
    """Язык, когда о нём ничего не известно: STT_LANGUAGE из настроек AI (по умолчанию en)."""
    from app.ai.config import get_settings  # noqa: PLC0415 — app.ai импортирует app.game, наверху нельзя

    return normalize_lang(get_settings().stt_language) or "en"


def parse_accept_language(header: str | None) -> str:
    """Первый поддерживаемый язык из Accept-Language с учётом q: «ro-RO,ro;q=0.9,en;q=0.8» → «ro»."""
    ranked: list[tuple[float, int, str]] = []
    for n, part in enumerate((header or "").split(",")):
        tag, _, params = part.partition(";")
        q = 1.0
        for param in params.split(";"):
            key, _, value = param.strip().partition("=")
            if key == "q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
        if q > 0 and (lang := normalize_lang(tag)):
            ranked.append((-q, n, lang))
    return min(ranked)[2] if ranked else default_lang()


def ui_lang(accept_language: Annotated[str | None, Header()] = None) -> str:
    """Зависимость FastAPI: язык интерфейса из заголовка Accept-Language."""
    return parse_accept_language(accept_language)


UiLang = Annotated[str, Depends(ui_lang)]


def language_name(lang: str | None) -> str:
    """Полное название для промптов: «English», «Russian», «Romanian»."""
    return LANGUAGE_NAMES[normalize_lang(lang) or default_lang()]


_RO_LETTERS = frozenset("ăâîșțşţ")


def text_lang(text: str) -> str | None:
    """Язык письменного текста по буквам: в основном кириллица → ru, есть ă, â, î, ș, ț → ro; иначе не понять — None."""
    letters = [c for c in text.lower() if c.isalpha()]
    if not letters:
        return None
    if sum("а" <= c <= "я" or c == "ё" for c in letters) > len(letters) / 2:
        return "ru"
    return "ro" if any(c in _RO_LETTERS for c in letters) else None


def pick[T](texts: Mapping[str, T], lang: str | None) -> T:
    """Строка или таблица строк на нужном языке; перевода нет — английская."""
    return texts.get(lang or "", texts["en"])
