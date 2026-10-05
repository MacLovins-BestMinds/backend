"""Метрики и оценка подачи кодом — правила из docs/tz («Оценка и звание»)."""

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

from app.ai.prosody import Prosody
from app.ai.schemas import DeliveryScore, GazePoint, Metrics, TimelineEvent
from app.ai.stt import Transcript, Word
from app.core.lang import pick

# Паразиты по языкам речи (en | ru | ro).
# Однозначные — паразиты всегда («um», «ну», «păi»); их не спутать со словом другого языка, поэтому они считаются
# в речи на любом языке. Двусмысленные («like», «deci», «знаете») бывают и обычными словами: паразитами они
# считаются только в «паразитной» позиции (см. _filler_position) или по решению LLM, и только для языка речи.
_EN_FILLERS = {"um", "uh", "er", "erm", "ehm", "ah", "hmm"}
# «вот», «значит», «кстати» бывают обычными словами («вот почему», «это значит») — они ниже, в двусмысленных
_RU_FILLERS = {"ну", "короче", "типа", "блин", "собственно", "э", "эм"}
_RU_PHRASES = {("как", "бы"), ("это", "самое"), ("в", "общем"), ("так", "сказать"), ("в", "принципе")}
_RO_FILLERS = {"păi", "pai", "ă", "ăă", "îî"}
_RO_PHRASES = {("cum", "să", "zic"), ("cum", "să", "spun"), ("cum", "sa", "zic"), ("cum", "sa", "spun")}
FILLER_WORDS = frozenset(_EN_FILLERS | _RU_FILLERS | _RO_FILLERS)
FILLER_PHRASES = frozenset(_RU_PHRASES | _RO_PHRASES)
# «a kind of tool» — нормальная речь; «it's kind of simple» — паразит-смягчение
_HEDGE_LEGIT_BEFORE = frozenset(
    {"a", "an", "the", "this", "that", "what", "which", "any", "some", "every", "one", "same"}
)


@dataclass(frozen=True, slots=True)
class FillerSet:
    """Двусмысленные паразиты одного языка: слова и фразы из 2–3 слов."""

    discourse: frozenset[str] = frozenset()  # «like», «so», «deci»: паразит, если стоит вставкой
    discourse_phrases: frozenset[tuple[str, ...]] = frozenset()  # «you know», «știi ce»
    hedges: frozenset[tuple[str, ...]] = frozenset()  # «kind of»: паразит, если перед ним нет артикля


FILLER_SETS: dict[str, FillerSet] = {
    # английские слова, которые бывают и паразитами, и нормальными словами («I like it», «Do you know why…»)
    "en": FillerSet(
        discourse=frozenset({"like", "so", "well", "actually", "basically", "literally", "right", "okay", "anyway", "anyways"}),
        discourse_phrases=frozenset({("you", "know"), ("i", "mean"), ("you", "see"), ("or", "something"), ("and", "stuff")}),
        hedges=frozenset({("kind", "of"), ("sort", "of")}),
    ),
    "ru": FillerSet(
        discourse=frozenset(
            {"знаете", "понимаете", "слушайте", "скажем", "допустим", "вот", "значит", "кстати", "просто", "вообще"}
        ),
        discourse_phrases=frozenset({("как", "говорится"), ("то", "есть")}),
    ),
    "ro": FillerSet(
        discourse=frozenset(
            {"deci", "adică", "adica", "gen", "practic", "efectiv", "bine", "na", "uite", "așa", "asa", "știi",
             "stii", "cumva"}
        ),  # fmt: skip
        discourse_phrases=frozenset(
            {("știi", "ce"), ("stii", "ce"), ("mă", "rog"), ("ma", "rog"), ("să", "zicem"), ("în", "fine"), ("in", "fine"),
             ("cum", "ar", "veni")}
        ),  # fmt: skip
    ),
}
# живой поток: язык речи заранее неизвестен — двусмысленные слова всех языков (их там всё равно не отмечают сразу)
_ANY_LANGUAGE = FillerSet(
    discourse=frozenset().union(*(f.discourse for f in FILLER_SETS.values())),
    discourse_phrases=frozenset().union(*(f.discourse_phrases for f in FILLER_SETS.values())),
    hedges=frozenset().union(*(f.hedges for f in FILLER_SETS.values())),
)


def filler_set(lang: str | None) -> FillerSet:
    """Двусмысленные паразиты языка речи; None — язык неизвестен (живой поток), берутся все языки."""
    return _ANY_LANGUAGE if lang is None else FILLER_SETS.get(lang, FILLER_SETS["en"])


# звуки-заминки: «um», «e-e-e», русское «э-э», «ммм», румынское «ăăă», «îîî»
_HESITATION = re.compile(r"(u+[hm]+|e+r+m*|e+h+m*|a+h+|h+m+|m+|e{2,}|a{2,}|y{2,}|э+м*|м+|а{2,}|а+м+|ы+|ă+m*|â+m*|î+m*)")
# запинка внутри слова: «th-th-the», «p-p-product», «я-я-я»; дефисные слова с настоящим повтором — не запинка
_STUTTER = re.compile(r"^(\w{1,3})(?:-\1)+(?:-?\w*)$", re.IGNORECASE)
_STUTTER_OK = frozenset({"so-so", "bye-bye", "no-no", "ha-ha", "еле-еле", "вот-вот", "так-так", "да-да", "нет-нет"})
# слова, которые повторяют нарочно («very, very good») или по грамматике («that that», «had had»)
_REPEAT_OK = frozenset(
    {"very", "really", "so", "much", "many", "long", "no", "yes", "bye", "ha", "that", "had", "is",
     "очень", "да", "нет", "foarte", "da", "nu"}
)  # fmt: skip
# служебные слова: фраза только из них повтором не считается
_STOPWORDS = frozenset(
    "a an the and or but of to in on at for from with by as is are was were be it its this that these those "
    "i you he she we they my your our their me him her us them do does did not no so if then there here "
    "и в во на с со к ко по о об от до за из у а но или что это как не же ли бы то я ты он она мы вы они "
    "și si în pe la cu de din că ca să sa nu e este al un o eu tu el ea noi voi ei ce".split()
)
# Ругань: слово считается бранным, если начинается с одного из корней. Русские корни — и кириллицей,
# и латиницей («blyat», «khuy», «pizdets»): так их иногда пишут распознавание и сами игроки.
_SWEAR_EN = ("fuck", "motherfuck", "shit", "bullshit", "bitch", "asshole", "bastard", "cunt", "dickhead", "piss", "wtf")
_SWEAR_RU = (
    "бля", "сука", "сучк", "хуй", "хуя", "хуе", "хуё", "хую", "хуи", "нахуй", "нахуя", "похуй", "нихуя",
    "пизд", "ебат", "ебал", "ебан", "ебуч", "ёб", "заеб", "заёб", "уеб", "уёб", "наеб", "мудак", "мудил", "говн",
    "пидор", "пидар",
)  # fmt: skip
_SWEAR_RO = ("futu-", "fututi", "futut")  # «pizdă» ловит русский корень латиницей «pizd»
# короткие румынские слова — только целиком: корнем они задели бы обычные слова («pulover», «muiere»)
SWEAR_WORDS = frozenset({"pula", "pulă", "muie", "cacat", "căcat", "curvă", "curva"})
_CYR = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
_LAT = ("a", "b", "v", "g", "d", "e", "yo", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
        "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya")  # fmt: skip
_TRANSLIT = {ord(c): lat for c, lat in zip(_CYR, _LAT, strict=True)}
SWEAR_STEMS = _SWEAR_EN + _SWEAR_RU + tuple(st.translate(_TRANSLIT) for st in _SWEAR_RU) + _SWEAR_RO
_CENSORED = re.compile(r"[a-zа-яё]\*{2,}\w*")  # распознавание иногда само ставит звёздочки: «f***»
PROFANITY_COST, PROFANITY_MAX = 8, 32  # столько баллов подачи снимает каждое бранное слово и не больше скольких всего
REPEAT_WINDOW_WORDS = 15  # повтор фразы считается ошибкой, только если он рядом; дальше — нормальный возврат к мысли
REPEAT_MAX_LEN = 8
_STRIP = " .,!?;:…—–-«»\"'()"
_SENTENCE_END = (".", "!", "?", "…")
_CLAUSE_END = (*_SENTENCE_END, ",", ";", ":", "—", "–")

HESITATION_SEC = 1.0  # посреди фразы от 1 до 3 с — запинка, от 3 с — длинная пауза (штраф)
LONG_PAUSE_SEC = 3.0
LONG_BREAK_SEC = 6.0  # пауза между фразами: до 6 с — нормальная, дольше — потеря нити, зал скучает (штраф)
GAZE_OFF_SEC = 3.0
PACE_WINDOW_SEC = 20.0  # окно по чистому времени речи
PACE_RANGE_WPM = (100, 180)
# Игрок сам выбирает, каким темпом хочет говорить. Спокойный: за медленную речь не ругаем вообще.
# Быстрый: медленная речь — уже ошибка, зал скучает.
PaceMode = Literal["slow", "normal", "fast"]
PACE_RANGES: dict[str, tuple[int, int]] = {"slow": (0, 170), "normal": PACE_RANGE_WPM, "fast": (140, 220)}
_PACE_CURVES: dict[str, list[tuple[float, float]]] = {
    "slow": [(0, 100), (150, 100), (200, 0)],
    "normal": [(80, 0), (120, 100), (160, 100), (200, 0)],
    "fast": [(110, 0), (150, 100), (200, 100), (240, 0)],
}
# Коридоры темпа заданы для английского. Русские и румынские слова длиннее (больше слогов на слово), поэтому та же
# скорость речи даёт меньше слов в минуту: русская речь на 120 слов/мин звучит как английская на 150.
PACE_LANG_FACTOR: dict[str, float] = {"en": 1.0, "ru": 0.8, "ro": 0.9}
SPEECH_GAP_SEC = 1.0  # паузы длиннее не входят во время речи: паузы не должны штрафоваться ещё и через темп

WEIGHTS = {"fillers": 0.30, "pace": 0.20, "gaze": 0.20, "pauses": 0.15, "timing": 0.15}

# Неуверенная речь бьёт по подаче напрямую, как ругань: первые два смягчения («мне кажется», «I think») — норма,
# дальше по 2 балла; извинение на сцене, вялое начало и слабый финал — по 3; всего не больше WEAK_MAX.
WEAK_FREE_HEDGES, WEAK_HEDGE_COST, WEAK_OTHER_COST, WEAK_MAX = 2, 2, 3, 12
WEAK_OPENING_WORDS = 15  # вялое начало ищется в первых словах
WEAK_CLOSING_WORDS = 8  # слабый финал — в последних
# Голос: монотонная речь и затухание к концу фраз тоже снимают баллы подачи (считает app/ai/prosody.py)
MONOTONE_COST, FADE_FREE, FADE_COST, PROSODY_MAX = 5, 2, 1, 10


def pace_range(mode: str = "normal", lang: str = "en") -> tuple[int, int]:
    """Коридор темпа (слов в минуту) для выбранного темпа и языка речи."""
    lo, hi = PACE_RANGES.get(mode, PACE_RANGE_WPM)
    k = PACE_LANG_FACTOR.get(lang, 1.0)
    return round(lo * k), round(hi * k)


@dataclass(frozen=True, slots=True)
class DeliveryAnalysis:
    metrics: Metrics
    score: DeliveryScore
    events: list[TimelineEvent]


def _norm(word: str) -> str:
    # ё → е; румынские ş/ţ с седилью (их выдают старые раскладки) → ș/ț с запятой; типографский апостроф → прямой
    return word.lower().strip(_STRIP).replace("ё", "е").replace("ş", "ș").replace("ţ", "ț").replace("’", "'")


def _is_hesitation(word: str) -> bool:
    return bool(_HESITATION.fullmatch(word.replace("-", "")))


def _interp(x: float, points: Sequence[tuple[float, float]]) -> float:
    """Кусочно-линейная функция по точкам (x, y), за краями — константа."""
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in pairwise(points):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


WeakKind = Literal["hedge", "apology", "weak_close", "flat_opening"]


def _grams(*phrases: str) -> frozenset[tuple[str, ...]]:
    return frozenset(tuple(_norm(w) for w in p.split()) for p in phrases)


@dataclass(frozen=True, slots=True)
class WeakSet:
    """Фразы неуверенной речи одного языка — то, что тренеры просят убрать первым делом."""

    hedges: frozenset[tuple[str, ...]]  # смягчения: «мне кажется», «I think», «poate»
    apologies: frozenset[tuple[str, ...]]  # извинения и оправдания: «sorry», «я волнуюсь», «scuze»
    closers: frozenset[tuple[str, ...]]  # вялый финал: «that's it», «вот как-то так», «asta e tot»
    openers: frozenset[tuple[str, ...]]  # вялое начало: «my topic is», «сегодня я расскажу», «tema mea este»


WEAK_SETS: dict[str, WeakSet] = {
    "en": WeakSet(
        hedges=_grams(
            "i think", "i guess", "i suppose", "maybe", "perhaps", "probably", "hopefully", "i'm not sure", "i am not sure",
            "i don't know", "i feel like", "a little bit", "something like that", "i would say", "i'd say",
        ),  # fmt: skip
        apologies=_grams(
            "sorry", "i'm sorry", "i am sorry", "excuse me", "i apologize", "i apologise", "forgive me", "bear with me",
            "i'm nervous", "i am nervous", "i'm a bit nervous", "i'm so nervous", "i'm not a good speaker",
            "i haven't prepared", "i didn't prepare", "i'm not prepared", "i am not prepared",
        ),  # fmt: skip
        closers=_grams(
            "that's it", "that is it", "that's all", "that is all", "i'm done", "i am done", "that's about it", "so yeah",
            "yeah so", "and yeah", "i guess that's it", "that's basically it", "i think that's it", "okay that's it",
            "the end",
        ),  # fmt: skip
        openers=_grams(
            "my topic is", "the topic is", "my topic today", "the topic of my", "today i will talk about",
            "today i'm going to talk about", "today i am going to talk about", "i'm going to talk about",
            "i am going to talk about", "i will talk about", "i want to talk about", "i would like to talk about",
            "i'd like to talk about", "i want to tell you about", "i was asked to", "i got the topic",
            "i'll be talking about", "i will be talking about", "let me tell you about", "i'm here to talk about",
        ),  # fmt: skip
    ),
    "ru": WeakSet(
        hedges=_grams(
            "мне кажется", "наверное", "наверно", "возможно", "может быть", "я думаю", "вроде", "вроде бы", "я не знаю",
            "не знаю", "скорее всего", "я не уверен", "я не уверена", "не уверен", "не уверена", "кажется", "видимо",
            "похоже", "как-то так",
        ),  # fmt: skip
        apologies=_grams(
            "извините", "извиняюсь", "простите", "прошу прощения", "сорри", "я волнуюсь", "я нервничаю", "я очень волнуюсь",
            "я не готовился", "я не готовилась", "я не подготовился", "я не подготовилась", "я плохо говорю",
            "я не умею выступать", "я не оратор",
        ),  # fmt: skip
        closers=_grams(
            "вот как-то так", "как-то так", "ну вот", "вот и всё", "в общем всё", "это всё", "у меня всё", "на этом всё",
            "ну всё", "наверное всё", "вроде всё", "короче всё", "вот так", "ну вот как-то так", "всё",
        ),  # fmt: skip
        openers=_grams(
            "моя тема", "тема моего", "тема моя", "тема у меня", "сегодня я расскажу", "сегодня я хочу рассказать",
            "сегодня я хотел бы", "сегодня я хотела бы", "я хотел бы рассказать", "я хотела бы рассказать",
            "я хочу рассказать", "хочу рассказать вам", "я расскажу вам", "я расскажу о", "я расскажу про",
            "мне дали тему", "мне выпала тема", "мне досталась тема", "мне попалась тема", "я буду говорить о",
            "я буду рассказывать", "сейчас я расскажу", "хочу поговорить о", "давайте поговорим о",
        ),  # fmt: skip
    ),
    "ro": WeakSet(
        hedges=_grams(
            "cred că", "cred ca", "mi se pare", "poate", "poate că", "poate ca", "probabil", "nu știu", "nu stiu",
            "nu sunt sigur", "nu sunt sigură", "nu sunt sigura", "parcă", "parca", "sau ceva", "ceva de genul", "oarecum",
            "într-un fel", "intr-un fel", "presupun", "bănuiesc", "banuiesc", "zic eu",
        ),  # fmt: skip
        apologies=_grams(
            "scuze", "scuzați", "scuzati", "scuzați-mă", "scuzati-ma", "îmi pare rău", "imi pare rau", "iertați-mă",
            "iertati-ma", "sunt emoționat", "sunt emotionat", "sunt emoționată", "sunt emotionata", "am emoții", "am emotii",
            "nu m-am pregătit", "nu m-am pregatit", "nu sunt un bun vorbitor",
        ),  # fmt: skip
        closers=_grams(
            "asta e tot", "asta a fost tot", "asta este tot", "cam asta e", "cam asta ar fi", "asta e", "asta ar fi",
            "asta este", "atât", "atat", "cam atât", "cam atat", "am terminat", "și gata", "si gata", "gata", "cam așa",
            "cam asa", "deci da", "da deci", "și da", "si da",
        ),  # fmt: skip
        openers=_grams(
            "tema mea este", "tema mea e", "tema mea", "tema este", "azi voi vorbi despre", "astăzi voi vorbi despre",
            "astazi voi vorbi despre", "voi vorbi despre", "o să vorbesc despre", "o sa vorbesc despre",
            "vreau să vă vorbesc despre", "vreau sa va vorbesc despre", "vreau să vorbesc despre", "vreau sa vorbesc despre",
            "aș vrea să vorbesc despre", "as vrea sa vorbesc despre", "aș vrea să vă povestesc", "as vrea sa va povestesc",
            "mi s-a dat tema", "mi-a picat tema", "am primit tema", "o să vă povestesc", "o sa va povestesc",
            "vă voi povesti", "va voi povesti", "am să vă vorbesc", "am sa va vorbesc",
        ),  # fmt: skip
    ),
}
WEAK_MAX_LEN = 6


def weak_set(lang: str | None) -> WeakSet:
    return WEAK_SETS.get(lang or "", WEAK_SETS["en"])


# --- детекторы ---


def _ends(raw: str, marks: tuple[str, ...]) -> bool:
    return raw.rstrip(" \"'»)").endswith(marks)


def _filler_position(raw: Sequence[str], i: int, j: int, prev_is_filler: bool) -> bool:
    """Слова raw[i..j] стоят как вставка: отделены запятыми/границей фразы или идут сразу после «um»."""
    starts_clause = i == 0 or _ends(raw[i - 1], _CLAUSE_END)
    return (starts_clause and _ends(raw[j], _CLAUSE_END)) or prev_is_filler


def find_fillers(
    words: Sequence[Word], verdicts: Mapping[int, bool] | None = None, lang: str | None = "en"
) -> list[tuple[float, str]]:
    """Слова-паразиты по контексту: «so, the idea…», «about, like, stoicism», но не «Do you know why…».

    lang — язык речи (en | ru | ro), от него зависят двусмысленные слова; None — язык неизвестен (живой поток).
    """
    return [(words[i].start, hit) for i, _, hit in filler_hits(words, verdicts, lang)]


PHRASE_LENGTHS = (3, 2)  # сначала длинные фразы: «cum să zic» раньше, чем «să zic»


def _ambiguous_phrase(norm: Sequence[str], i: int, fs: FillerSet) -> int:
    """Длина двусмысленной фразы («you know», «kind of», «știi ce»), которая начинается со слова i; 0 — её нет."""
    for n in PHRASE_LENGTHS:
        gram = tuple(norm[i : i + n])
        if len(gram) == n and (gram in fs.discourse_phrases or gram in fs.hedges):
            return n
    return 0


def filler_candidates(words: Sequence[Word], lang: str | None = "en") -> list[tuple[int, int, str]]:
    """Слова, которые бывают и паразитами, и обычными словами («like», «so», «you know», «kind of», «deci»):
    (номер первого слова, сколько слов, фраза). Паразит ли это здесь — решается по смыслу, см. verdicts."""
    fs = filler_set(lang)
    norm = [_norm(w.text) for w in words]
    found: list[tuple[int, int, str]] = []
    i = 0
    while i < len(norm):
        if n := _ambiguous_phrase(norm, i, fs):
            found.append((i, n, " ".join(norm[i : i + n])))
            i += n
            continue
        if norm[i] in fs.discourse:
            found.append((i, 1, norm[i]))
        i += 1
    return found


def _hit_at(
    raw: Sequence[str],
    norm: Sequence[str],
    i: int,
    fs: FillerSet,
    verdicts: Mapping[int, bool] | None,
    prev_is_filler: bool,
) -> tuple[str | None, int]:
    """Паразит, который начинается со слова i, и сколько слов он занимает: (паразит или None, длина)."""
    for n in PHRASE_LENGTHS:
        gram = tuple(norm[i : i + n])
        if len(gram) < n:
            continue
        phrase = " ".join(gram)
        if gram in FILLER_PHRASES:
            return phrase, n
        if gram not in fs.discourse_phrases and gram not in fs.hedges:
            continue
        if verdicts is not None:
            # двусмысленная фраза — по смыслу; обычную фразу («you know the answer») по словам дальше не проверяем
            return (phrase if verdicts.get(i, False) else None), n
        if gram in fs.discourse_phrases and _filler_position(raw, i, i + n - 1, prev_is_filler):
            return phrase, n
        if gram in fs.hedges and (i == 0 or norm[i - 1] not in _HEDGE_LEGIT_BEFORE):
            return phrase, n
    word = norm[i]
    if word in FILLER_WORDS or _is_hesitation(word):
        return word, 1
    if word in fs.discourse:
        is_filler = verdicts.get(i, False) if verdicts is not None else _filler_position(raw, i, i, prev_is_filler)
        return (word if is_filler else None), 1
    return None, 1


def filler_hits(
    words: Sequence[Word], verdicts: Mapping[int, bool] | None = None, lang: str | None = "en"
) -> list[tuple[int, int, str]]:
    """То же, что find_fillers, но с местом в тексте: (номер первого слова, сколько слов, паразит).

    verdicts — решение по смыслу для двусмысленных слов (номер первого слова → паразит или нет), его даёт LLM.
    Без него (None) такие слова оцениваются по положению во фразе; пустой словарь — двусмысленные не считаются.
    Однозначные паразиты («um», «э-э», «типа», «păi») считаются всегда.
    """
    fs = filler_set(lang)
    raw = [w.text for w in words]
    norm = [_norm(r) for r in raw]
    found: list[tuple[int, int, str]] = []
    prev_is_filler = False
    i = 0
    while i < len(words):
        hit, span = _hit_at(raw, norm, i, fs, verdicts, prev_is_filler)
        if hit:
            found.append((i, span, hit))
        prev_is_filler = hit is not None and (norm[i] in FILLER_WORDS or _is_hesitation(norm[i]))
        i += span
    return found


def find_profanity(words: Sequence[Word]) -> list[tuple[int, str]]:
    """Ругань: (номер слова, слово). На сцене её быть не должно — ни в питче, ни в ответах жюри."""
    found = []
    for i, w in enumerate(words):
        raw = w.text.lower().strip(_STRIP.replace("*", ""))
        word = _norm(w.text)
        if word.startswith(SWEAR_STEMS) or word in SWEAR_WORDS or _CENSORED.fullmatch(raw):
            found.append((i, word or raw))
    return found


def find_weak_phrases(
    words: Sequence[Word], lang: str | None = "en", skip: Collection[int] = ()
) -> list[tuple[int, int, WeakKind, str]]:
    """Неуверенная речь: (номер первого слова, сколько слов, вид, фраза). skip — слова, уже занятые другой отметкой.

    Смягчения («мне кажется», «I think», «poate») и извинения («sorry», «я волнуюсь») ищутся по всему тексту,
    вялое начало («my topic is», «сегодня я расскажу») — только в первых WEAK_OPENING_WORDS словах, слабый финал
    («that's it», «вот как-то так», «asta e tot») — только в последних WEAK_CLOSING_WORDS; финал из одного слова
    («всё», «atât», «gata») — только если это самое последнее слово.
    """
    ws = weak_set(lang)
    norm = [_norm(w.text) for w in words]
    n = len(norm)
    found: list[tuple[int, int, WeakKind, str]] = []
    i = 0
    while i < n:
        hit: tuple[int, WeakKind] | None = None
        for size in range(min(WEAK_MAX_LEN, n - i), 0, -1):
            gram = tuple(norm[i : i + size])
            if not all(gram) or any(k in skip for k in range(i, i + size)):
                continue
            if i < WEAK_OPENING_WORDS and gram in ws.openers:
                hit = (size, "flat_opening")
            elif i >= n - WEAK_CLOSING_WORDS and gram in ws.closers and (size > 1 or i == n - 1):
                hit = (size, "weak_close")
            elif gram in ws.apologies:
                hit = (size, "apology")
            elif gram in ws.hedges:
                hit = (size, "hedge")
            if hit:
                break
        if hit:
            size, kind = hit
            found.append((i, size, kind, " ".join(norm[i : i + size])))
            i += size
        else:
            i += 1
    return found


def weak_phrases_cost(kinds: Sequence[str]) -> int:
    """Сколько баллов подачи снимает неуверенная речь: смягчения сверх WEAK_FREE_HEDGES и всё остальное."""
    hedges = sum(k == "hedge" for k in kinds)
    other = len(kinds) - hedges
    return min(WEAK_MAX, WEAK_HEDGE_COST * max(0, hedges - WEAK_FREE_HEDGES) + WEAK_OTHER_COST * other)


def find_stumbles(words: Sequence[Word]) -> list[tuple[int, int]]:
    """Запинки: (номер первого слова, сколько слов).

    Оборванное слово, начатое заново («pro- product», «we- we have»), и заикание внутри слова («th-th-the»,
    «я-я-я»). Звуки-заминки («e-e-e») — паразиты, здесь не считаются.
    """
    found: list[tuple[int, int]] = []
    i = 0
    while i < len(words):
        raw = words[i].text.strip().strip("\"'«»()")
        norm = _norm(raw)
        if raw.endswith(("-", "—", "–")) and i + 1 < len(words):
            stem = norm.replace("-", "")
            nxt = _norm(words[i + 1].text).replace("-", "")
            if stem and nxt.startswith(stem) and not _is_hesitation(stem):
                found.append((i, 2))
                i += 2
                continue
        if "-" in norm and norm not in _STUTTER_OK and _STUTTER.match(norm) and not _is_hesitation(norm.replace("-", "")):
            found.append((i, 1))
        i += 1
    return found


def _parallel(words: Sequence[Word], norm: Sequence[str], i: int, n: int, lo: int) -> bool:
    """Повтор через другие слова в начале или в конце соседних частей фразы — параллельная конструкция, приём,
    а не ошибка: «если я ругаюсь, им это не нравится, если я молчу, им это не нравится».
    Оборванное прошлое вхождение («если он сейчас-- вот, если он сейчас начнёт») — фальстарт, это ошибка."""
    gram = list(norm[i : i + n])
    j = max((k for k in range(lo, i - n + 1) if list(norm[k : k + n]) == gram), default=None)
    if j is None or words[j + n - 1].text.rstrip().endswith("-"):
        return False
    between = [t for t in norm[j + n : i] if t and t not in FILLER_WORDS and not _is_hesitation(t)]
    # подряд или через «um» — запинка; через одно слово — только если между ними закончилось предложение
    if len(between) < (1 if _ends(words[i - 1].text, _SENTENCE_END) else 2):
        return False
    starts = all(k == 0 or _ends(words[k - 1].text, _CLAUSE_END) for k in (j, i))
    ends = all(_ends(words[k + n - 1].text, _CLAUSE_END) for k in (j, i))
    return starts or ends


def find_repeats(words: Sequence[Word]) -> list[tuple[int, int]]:
    """Повторы слов и выражений: (номер первого слова повтора, сколько слов).

    Ошибкой считается: слово, сказанное два раза подряд («Telegram. Telegram»), фраза из трёх и более слов,
    повторённая рядом («they will leave… they will leave»), и пара слов, прозвучавшая рядом в третий раз.
    Параллельная конструкция (см. _parallel) — приём, а не ошибка.
    """
    norm = [_norm(w.text).replace("-", "") for w in words]
    found: list[tuple[int, int]] = []
    i = 0
    while i < len(norm):
        length = 0
        lo = max(0, i - REPEAT_WINDOW_WORDS)
        # самая длинная фраза, начинающаяся здесь и уже звучавшая рядом; последним словом предложения фраза
        # не начинается — иначе в «…is money. They need money. They need money» отметится «money. They need»
        longest = 1 if _ends(words[i].text, _SENTENCE_END) else min(REPEAT_MAX_LEN, len(norm) - i)
        for n in range(longest, 1, -1):
            gram = norm[i : i + n]
            if not all(gram) or all(w in _STOPWORDS for w in gram):
                continue
            earlier = sum(norm[j : j + n] == gram for j in range(lo, i - n + 1))
            adjacent = i >= n and norm[i - n : i] == gram
            if earlier >= (1 if n >= 3 or adjacent else 2):
                length = 0 if _parallel(words, norm, i, n, lo) else n
                break
        if not length and i > 0 and norm[i] and norm[i] == norm[i - 1] and norm[i] not in _REPEAT_OK:
            length = 1
        if length:
            found.append((i, length))
            i += length
        else:
            i += 1
    return found


def word_spans(transcript: Transcript) -> list[tuple[int, int]]:
    """Где каждое слово стоит в transcript.text (символы) — чтобы приложение отметило ошибку прямо в тексте."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for w in transcript.words:
        token = w.text.strip()
        at = transcript.text.find(token, cursor) if token else -1
        if at < 0:  # текст и слова разошлись — ставим точку на текущем месте
            spans.append((cursor, cursor))
            continue
        spans.append((at, at + len(token)))
        cursor = at + len(token)
    return spans


def find_gaps(words: Sequence[Word], min_sec: float, max_sec: float = float("inf")) -> list[tuple[float, float]]:
    """Паузы между словами: (время начала паузы, длительность)."""
    gaps = []
    for prev, cur in pairwise(words):
        gap = cur.start - prev.end
        if min_sec <= gap <= max_sec:
            gaps.append((prev.end, round(gap, 2)))
    return gaps


def classify_pauses(words: Sequence[Word]) -> dict[str, list[tuple[float, float]]]:
    """Только плохие паузы. Посреди фразы: запинка (от 1 с) или длинная пауза (от 3 с). После конца фразы пауза
    естественна и не отмечается — пока не затянется дольше LONG_BREAK_SEC: это уже потеря нити («break»)."""
    result: dict[str, list[tuple[float, float]]] = {"hesitation": [], "long": [], "break": []}
    for prev, cur in pairwise(words):
        gap = round(cur.start - prev.end, 2)
        if gap < HESITATION_SEC:
            continue
        if _ends(prev.text, _SENTENCE_END):
            if gap >= LONG_BREAK_SEC:
                result["break"].append((prev.end, gap))
            continue
        if gap >= LONG_PAUSE_SEC:
            result["long"].append((prev.end, gap))
        elif not _ends(prev.text, _CLAUSE_END):  # пауза после запятой — естественная, не запинка
            result["hesitation"].append((prev.end, gap))
    return result


def speech_minutes(words: Sequence[Word]) -> float:
    """Чистое время речи: от первого до последнего слова без пауз длиннее SPEECH_GAP_SEC."""
    if not words:
        return 1.0
    span = words[-1].end - words[0].start
    pauses = sum(g for p, c in pairwise(words) if (g := c.start - p.end) > SPEECH_GAP_SEC)
    return max((span - pauses) / 60, 1 / 60)


def _gaze_spans(gaze: Sequence[GazePoint], duration: float) -> list[tuple[float, float, bool]]:
    """Таймлайн взгляда как отрезки (start, end, on). До первой точки — состояние первой точки."""
    points = sorted(gaze, key=lambda p: p.t)
    spans = []
    for i, p in enumerate(points):
        start = 0.0 if i == 0 else min(p.t, duration)
        end = points[i + 1].t if i + 1 < len(points) else duration
        end = min(end, duration)
        if end > start:
            spans.append((start, end, p.on))
    return spans


def gaze_on_ratio(gaze: Sequence[GazePoint], duration: float) -> float | None:
    """Доля времени со взглядом в зал; None — взгляд не измерялся (нет камеры, например в вебе)."""
    if not gaze or duration <= 0:
        return None
    on = sum(end - start for start, end, is_on in _gaze_spans(gaze, duration) if is_on)
    return round(on / duration, 3)


def pace_alerts(
    words: Sequence[Word], filler_starts: set[float], limits: tuple[int, int] = PACE_RANGE_WPM
) -> list[tuple[float, int]]:
    """Темп по окнам 20 с чистого времени речи (длинные паузы вырезаны) вне коридора limits (слов в минуту)."""
    if not words:
        return []
    alerts = []
    speech_t = 0.0  # время речи к началу текущего слова
    window_start_real = words[0].start
    window_start_speech = 0.0
    count = 0
    for prev, cur in zip((None, *words), words, strict=False):
        if prev is not None:
            gap = cur.start - prev.end
            speech_t += (prev.end - prev.start) + (0.0 if gap > SPEECH_GAP_SEC else gap)
        if speech_t - window_start_speech >= PACE_WINDOW_SEC:
            wpm = round(count * 60 / (speech_t - window_start_speech))
            if not limits[0] <= wpm <= limits[1]:
                alerts.append((round(window_start_real, 2), wpm))
            window_start_real, window_start_speech, count = cur.start, speech_t, 0
        if cur.start not in filler_starts:
            count += 1
    return alerts


# --- оценки 0–100 ---


def fillers_score(per_min: float) -> int:
    return round(_interp(per_min, [(1, 100), (5, 40), (12, 0)]))


def pace_score(wpm: float, mode: str = "normal", lang: str = "en") -> int:
    """Кривые заданы для английского; для других языков темп приводится к английскому эквиваленту."""
    return round(_interp(wpm / PACE_LANG_FACTOR.get(lang, 1.0), _PACE_CURVES[mode]))


def pauses_score(long_pauses: int) -> int:
    return max(0, 100 - 10 * long_pauses)


def gaze_score(ratio: float) -> int:
    return round(_interp(ratio, [(0, 0), (0.7, 100)]))


def timing_score(duration: float, min_sec: float, max_sec: float) -> int:
    return round(_interp(duration, [(0, 0), (min_sec, 100), (max_sec, 100), (max_sec + 60, 0)]))


# Подписи маркеров таймлайна — на языке речи
EVENT_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "repeat": "Repeated: «{phrase}»",
        "stumble": "Stumble: «{phrase}»",
        "profanity": "Swearing: «{word}»",
        "long_pause": "Pause of {sec} s mid-phrase",
        "long_break": "Pause of {sec} s between phrases",
        "hesitation": "Hesitation of {sec} s mid-phrase",
        "gaze_off": "Looking away for {sec} s",
        "pace": "Pace {wpm} words/min — {verdict}",
        "fast": "too fast",
        "slow": "too slow",
        "hedge": "Hedging: «{phrase}»",
        "apology": "Apology on stage: «{phrase}»",
        "weak_close": "Weak ending: «{phrase}»",
        "flat_opening": "Flat opening: «{phrase}»",
        "fade": "The voice fades at the end of the phrase (−{db} dB)",
    },
    "ru": {
        "repeat": "Повтор: «{phrase}»",
        "stumble": "Запинка: «{phrase}»",
        "profanity": "Ругань: «{word}»",
        "long_pause": "Пауза {sec} с посреди фразы",
        "long_break": "Пауза {sec} с между фразами",
        "hesitation": "Заминка {sec} с посреди фразы",
        "gaze_off": "Взгляд в сторону {sec} с",
        "pace": "Темп {wpm} слов/мин — {verdict}",
        "fast": "слишком быстро",
        "slow": "слишком медленно",
        "hedge": "Неуверенность: «{phrase}»",
        "apology": "Извинение на сцене: «{phrase}»",
        "weak_close": "Слабый финал: «{phrase}»",
        "flat_opening": "Вялое начало: «{phrase}»",
        "fade": "Голос затухает к концу фразы (−{db} дБ)",
    },
    "ro": {
        "repeat": "Repetiție: «{phrase}»",
        "stumble": "Poticnire: «{phrase}»",
        "profanity": "Înjurătură: «{word}»",
        "long_pause": "Pauză de {sec} s în mijlocul frazei",
        "long_break": "Pauză de {sec} s între fraze",
        "hesitation": "Ezitare de {sec} s în mijlocul frazei",
        "gaze_off": "Privire în altă parte {sec} s",
        "pace": "Ritm {wpm} cuvinte/min — {verdict}",
        "fast": "prea rapid",
        "slow": "prea lent",
        "hedge": "Nesiguranță: «{phrase}»",
        "apology": "Scuze pe scenă: «{phrase}»",
        "weak_close": "Final slab: «{phrase}»",
        "flat_opening": "Început plat: «{phrase}»",
        "fade": "Vocea se stinge la finalul frazei (−{db} dB)",
    },
}


def _seconds(value: float, digits: int, lang: str) -> str:
    """Число секунд для подписи: в русском и румынском дробная часть — через запятую."""
    text = f"{value:.{digits}f}"
    return text if lang == "en" else text.replace(".", ",")


def analyze(
    transcript: Transcript,
    gaze: Sequence[GazePoint],
    min_sec: float,
    max_sec: float,
    filler_verdicts: Mapping[int, bool] | None = None,
    pace: str = "normal",
    lang: str = "en",
    prosody: Prosody | None = None,
) -> DeliveryAnalysis:
    """lang — язык речи: от него зависят паразиты, коридор темпа и язык подписей маркеров.
    prosody — голос по записи (app/ai/prosody.py): затухание фраз и монотонность; None — не измерялся."""
    words = transcript.words
    duration = transcript.duration
    speech_min = speech_minutes(words)
    texts = pick(EVENT_TEXTS, lang)
    lang = lang if lang in EVENT_TEXTS else "en"

    hits = filler_hits(words, filler_verdicts, lang)
    fillers = [(words[i].start, hit) for i, _, hit in hits]
    filler_starts = {t for t, _ in fillers}
    spans_in_text = word_spans(transcript)
    by_end = {w.end: i for i, w in enumerate(words)}
    by_start = {w.start: i for i, w in enumerate(words)}

    def after_word(t: float) -> dict[str, int | None]:
        """Точка в тексте сразу после слова, которое закончилось в момент t (пауза стоит между словами)."""
        i = by_end.get(t)
        return {"start": spans_in_text[i][1], "end": spans_in_text[i][1]} if i is not None else {}

    def before_word(t: float) -> dict[str, int | None]:
        i = by_start.get(t)
        return {"start": spans_in_text[i][0], "end": spans_in_text[i][0]} if i is not None else {}

    def at_time(t: float) -> dict[str, int | None]:
        """Точка в тексте перед первым словом, прозвучавшим не раньше момента t (взгляд к словам не привязан)."""
        i = next((k for k, w in enumerate(words) if w.end >= t), None)
        return {"start": spans_in_text[i][0], "end": spans_in_text[i][0]} if i is not None else {}

    def over_words(i: int, n: int) -> dict[str, int | None]:
        return {"start": spans_in_text[i][0], "end": spans_in_text[i + n - 1][1]}

    pauses = classify_pauses(words)
    long_pauses = pauses["long"] + pauses["break"]
    spans = _gaze_spans(gaze, duration)
    ratio = gaze_on_ratio(gaze, duration)
    content_words = len(words) - sum(len(f.split()) for _, f in fillers)
    wpm = round(content_words / speech_min)
    fillers_per_min = round(len(fillers) / speech_min, 1)

    parts = {
        "fillers": fillers_score(fillers_per_min),
        "pace": pace_score(wpm, pace, lang),
        "gaze": gaze_score(ratio) if ratio is not None else None,
        "pauses": pauses_score(len(long_pauses)),
        "timing": timing_score(duration, min_sec, max_sec),
    }
    # без камеры взгляд не измерен: его вес делится между остальными частями, а не даётся даром
    measured = {k: w for k, w in WEIGHTS.items() if parts[k] is not None}
    total = round(sum(parts[k] * w for k, w in measured.items()) / sum(measured.values()))

    events = [TimelineEvent(type="filler", t=words[i].start, text=f"«{hit}»", **over_words(i, n)) for i, n, hit in hits]
    taken = {i + k for i, n, _ in hits for k in range(n)}  # слова, уже отмеченные: одно место — одна отметка
    stumbles = [(i, n) for i, n in find_stumbles(words) if taken.isdisjoint(range(i, i + n))]
    for i, n in stumbles:
        taken.update(range(i, i + n))
        phrase = " ".join(w.text.strip() for w in words[i : i + n])
        events.append(TimelineEvent(type="stumble", t=words[i].start, text=texts["stumble"].format(phrase=phrase), **over_words(i, n)))
    for i, n in find_repeats(words):
        if taken.isdisjoint(range(i, i + n)):  # «um, um» уже отмечено как паразит, «we- we» — как запинка
            taken.update(range(i, i + n))
            phrase = " ".join(_norm(w.text) for w in words[i : i + n])
            events.append(
                TimelineEvent(type="repeat", t=words[i].start, text=texts["repeat"].format(phrase=phrase), **over_words(i, n))
            )
    weak = find_weak_phrases(words, lang, skip=taken)
    total -= weak_phrases_cost([kind for _, _, kind, _ in weak])  # неуверенная речь бьёт по подаче напрямую
    events += [
        TimelineEvent(type="weak_phrase", t=words[i].start, text=texts[kind].format(phrase=phrase), **over_words(i, n))
        for i, n, kind, phrase in weak
    ]
    swears = find_profanity(words)
    total -= min(PROFANITY_MAX, PROFANITY_COST * len(swears))  # и ругань тоже
    events += [
        TimelineEvent(type="profanity", t=words[i].start, text=texts["profanity"].format(word=word), **over_words(i, 1))
        for i, word in swears
    ]
    events += [
        TimelineEvent(
            type="long_pause", t=t, text=texts["long_pause"].format(sec=_seconds(d, 1, lang)), **after_word(t)
        )
        for t, d in pauses["long"]
    ]
    events += [
        TimelineEvent(
            type="long_pause", t=t, text=texts["long_break"].format(sec=_seconds(d, 1, lang)), **after_word(t)
        )
        for t, d in pauses["break"]
    ]
    events += [
        TimelineEvent(
            type="hesitation", t=t, text=texts["hesitation"].format(sec=_seconds(d, 1, lang)), **after_word(t)
        )
        for t, d in pauses["hesitation"]
    ]
    events += [
        TimelineEvent(
            type="gaze_off",
            t=round(start, 2),
            text=texts["gaze_off"].format(sec=_seconds(end - start, 0, lang)),
            **at_time(start),
        )
        for start, end, on in spans
        if not on and end - start > GAZE_OFF_SEC
    ]
    limits = pace_range(pace, lang)
    for t, window_wpm in pace_alerts(words, filler_starts, limits):
        verdict = texts["fast"] if window_wpm > limits[1] else texts["slow"]
        events.append(
            TimelineEvent(type="pace", t=t, text=texts["pace"].format(wpm=window_wpm, verdict=verdict), **before_word(t))
        )
    if prosody is not None:
        # голос: затухание к концу фразы — отметка на последнем слове; монотонность — в метриках и в баллах
        events += [
            TimelineEvent(type="energy", t=words[i].start, text=texts["fade"].format(db=_seconds(drop, 0, lang)), **over_words(i, 1))
            for i, drop in prosody.fades
        ]
        voice_cost = (MONOTONE_COST if prosody.monotone else 0) + FADE_COST * max(0, len(prosody.fades) - FADE_FREE)
        total -= min(PROSODY_MAX, voice_cost)
    total = max(0, total)
    events.sort(key=lambda e: e.t)

    return DeliveryAnalysis(
        metrics=Metrics(
            duration_sec=round(duration, 1),
            words=content_words,
            wpm=wpm,
            fillers=len(fillers),
            fillers_per_min=fillers_per_min,
            long_pauses=len(long_pauses),
            profanity=len(swears),
            gaze_on_ratio=ratio,
            stumbles=len(stumbles),
            weak_phrases=len(weak),
            pitch_variation=prosody.pitch_variation if prosody else None,
            monotone=prosody.monotone if prosody else None,
            fades=len(prosody.fades) if prosody else 0,
        ),
        score=DeliveryScore(total=total, **parts),
        events=events,
    )
