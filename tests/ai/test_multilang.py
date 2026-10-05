"""Языки en | ru | ro: язык речи решает всё о выступлении, язык интерфейса — только темы, ошибки и прогресс.

Без внешних API.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.ai import delivery, jury, live, llm, refine, stt
from app.ai.clients import MissingKeyError
from app.ai.config import get_settings
from app.ai.delivery_metrics import analyze, filler_candidates, find_fillers
from app.ai.jury import DraftQuestion, DraftQuestions, RoundStateError
from app.ai.live import LiveAnalyzer, LiveCheck
from app.ai.pitch import Pitch
from app.ai.schemas import (
    Audience,
    CriterionScore,
    Metrics,
    PronunciationAssessment,
    RefineMode,
    RefineRequest,
)
from app.ai.slides import FitSlide, FitSlidesDraft, assemble
from app.ai.stt import Transcript, Word
from app.core.lang import default_lang, language_name, normalize_lang, parse_accept_language, text_lang


def _spoken(text: str, step: float = 0.5) -> Transcript:
    words = [Word(w, i * step, i * step + 0.4) for i, w in enumerate(text.split())]
    return Transcript(text, words, duration=90)


@pytest.fixture
def settings_env(monkeypatch, tmp_path):
    """Свои настройки AI на время теста: кэш и статика во временной папке."""
    monkeypatch.setenv("AI_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("STATIC_DIR", str(tmp_path / "static"))
    monkeypatch.setenv("STT_PROVIDER", "elevenlabs")
    monkeypatch.setenv("STT_MODEL", "scribe_v2")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


# --- язык интерфейса ---


@pytest.mark.parametrize(
    ("code", "expected"),
    [("ru-RU", "ru"), ("rus", "ru"), ("eng", "en"), ("ron", "ro"), ("rum", "ro"), ("mol", "ro"), ("english", "en"),
     ("Romanian", "ro"), ("ro_MD", "ro"), ("ukr", None), ("de", None), ("", None), (None, None)],
)  # fmt: skip
def test_language_codes_are_normalized(code, expected) -> None:
    assert normalize_lang(code) == expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("ru", "ru"),
        ("ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7", "ru"),
        ("de-DE,de;q=0.9,ro;q=0.8,en;q=0.5", "ro"),
        ("en;q=0.5, ro;q=0.9", "ro"),
        ("fr-FR, de", None),
        ("ru;q=0", None),
        ("", None),
        (None, None),
    ],
)
def test_accept_language_picks_the_first_supported_language(header, expected) -> None:
    assert parse_accept_language(header) == (expected or default_lang())


def test_stt_language_setting_is_the_fallback(settings_env) -> None:
    assert parse_accept_language(None) == default_lang() == (normalize_lang(get_settings().stt_language) or "en")
    settings_env.setenv("STT_LANGUAGE", "ro")
    get_settings.cache_clear()
    assert parse_accept_language("fr") == "ro" and language_name(None) == "Romanian"


# --- язык речи ---


def _scribe(monkeypatch, language_code: str | None, text: str) -> list[dict]:
    calls: list[dict] = []

    async def convert(**kwargs):
        calls.append(kwargs)
        words = [SimpleNamespace(text=w, start=i * 0.5, end=i * 0.5 + 0.4, type="word") for i, w in enumerate(text.split())]
        return SimpleNamespace(text=text, words=words, audio_duration_secs=3.0, language_code=language_code)

    monkeypatch.setattr(stt, "elevenlabs_client", lambda: SimpleNamespace(speech_to_text=SimpleNamespace(convert=convert)))
    return calls


def test_scribe_detects_the_language_and_keeps_cyrillic(settings_env) -> None:
    calls = _scribe(settings_env, "rus", "Ну, это наш продукт.")
    transcript = asyncio.run(stt.transcribe(b"\x00" * 2000))
    assert (transcript.text, transcript.language) == ("Ну, это наш продукт.", "ru")
    assert [w.text for w in transcript.words][:2] == ["Ну,", "это"]  # не латиница
    assert "language_code" not in calls[0]  # язык не навязываем — Scribe определяет его сам
    assert stt.speech_lang(transcript, "en") == "ru"
    again = asyncio.run(stt.transcribe(b"\x00" * 2000))  # из кэша — с тем же языком
    assert again.language == "ru" and len(calls) == 1


def test_unknown_or_unsupported_speech_language_uses_the_fallback(settings_env) -> None:
    _scribe(settings_env, "ukr", "Привіт, це наш продукт.")
    transcript = asyncio.run(stt.transcribe(b"\x01" * 2000))
    assert transcript.language is None and stt.speech_lang(transcript, "ro") == "ro"
    assert stt.speech_lang(Transcript("", [], 0.0), "ru") == "ru"


# --- паразиты по языкам ---


def test_romanian_fillers_and_ambiguous_words() -> None:
    words = _spoken("Păi, deci, ideea mea, ăă, cum să zic, e simplă și merge bine.").words
    assert [w for _, w in find_fillers(words, lang="ro")] == ["păi", "deci", "ăă", "cum să zic"]
    # однозначные паразиты считаются на любом языке, двусмысленные румынские — только в румынской речи
    assert [w for _, w in find_fillers(words, lang="en")] == ["păi", "ăă", "cum să zic"]
    assert [c[2] for c in filler_candidates(words, "ro")] == ["deci", "bine"]
    assert filler_candidates(words, "en") == []
    # «bine» по смыслу — паразит: так решил LLM
    assert [w for _, w in find_fillers(words, {1: False, 12: True}, lang="ro")] == ["păi", "ăă", "cum să zic", "bine"]


def test_russian_fillers_by_language() -> None:
    words = _spoken("Это, знаете, важно, в общем, э-э, вы знаете ответ.").words
    assert [w for _, w in find_fillers(words, lang="ru")] == ["знаете", "в общем", "э-э"]
    assert [w for _, w in find_fillers(_spoken("Ну, эм, так сказать, это самое").words, lang="ru")] == [
        "ну", "эм", "так сказать", "это самое"
    ]  # fmt: skip


def test_english_fillers_are_unchanged() -> None:
    words = _spoken("Um, so it's, like, simple. So, you know, it's kind of fine.").words
    assert [w for _, w in find_fillers(words, lang="en")] == ["um", "so", "like", "so", "you know", "kind of"]
    assert find_fillers(words) == find_fillers(words, lang="en")


def test_live_stream_catches_fillers_of_every_language() -> None:
    a = LiveAnalyzer()
    a.on_audio(32_000)
    assert [e.word for e in a.on_partial_text("păi asta e ideea ăă")] == ["păi", "ăă"]
    assert [e.word for e in a.on_partial_text("păi asta e ideea ăă ну и um")] == ["ну", "um"]


def test_timeline_texts_follow_the_speech_language() -> None:
    words = [Word("Представьте", 0.0, 0.4), Word("бабушку", 0.5, 0.7), Word("утром", 5.0, 5.3)]
    transcript = Transcript("Представьте бабушку утром", words, duration=90)
    by_lang = {lang: [e.text for e in analyze(transcript, [], 60, 180, lang=lang).events] for lang in ("en", "ru", "ro", "de")}
    assert by_lang == {
        "en": ["Pause of 4.3 s mid-phrase"],
        "ru": ["Пауза 4,3 с посреди фразы"],
        "ro": ["Pauză de 4,3 s în mijlocul frazei"],
        "de": ["Pause of 4.3 s mid-phrase"],
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Мой питч про кофе", "ru"), ("Ideea mea e simplă: cafea pentru toți.", "ro"), ("My pitch is about coffee", None),
     ("Ideea mea e simpla", None), ("123 —", None), ("Telegram для бабушек и дедушек", "ru")],
)  # fmt: skip
def test_text_language_is_guessed_by_its_letters(text, expected) -> None:
    assert text_lang(text) == expected


# --- промпты получают язык речи ---


def _capture_llm(monkeypatch, answers: dict[str, object]) -> dict[str, dict]:
    """Подменяет llm.generate: проверяет, что промпт собирается из переданных переменных, и запоминает их."""
    seen: dict[str, dict] = {}

    async def generate(name, schema, **variables):
        prompt = llm.load_prompt(name).substitute(variables)  # KeyError — переменной промпта не передали
        seen[name] = {**variables, "_prompt": prompt}
        if isinstance(answer := answers[name], Exception):
            raise answer
        return answer

    monkeypatch.setattr(llm, "generate", generate)
    return seen


def _metrics() -> Metrics:
    return Metrics(duration_sec=60, words=100, wpm=120, fillers=1, fillers_per_min=1.0, long_pauses=0)


def test_content_and_warmup_feedback_is_in_the_speech_language(monkeypatch) -> None:
    answer = delivery.ContentAssessment(criteria=[], tips=["a", "b", "c"])
    seen = _capture_llm(monkeypatch, {"content_score": answer, "warmup_score": answer})
    # язык интерфейса раунда (ro) на разбор не влияет
    pitch = Pitch(title="t", brief="b", audience=Audience.PUBLIC, ui_lang="ro")
    asyncio.run(delivery.assess_content(pitch, "Привет, это мой питч", _metrics(), speech="ru"))
    warmup = Pitch(title="t", brief="b", audience=Audience.PUBLIC, is_warmup=True, ui_lang="ru")
    asyncio.run(delivery.assess_content(warmup, "Hi", _metrics()))
    content = seen["content_score"]
    assert content["speech_language"] == "Russian" and "feedback_language" not in content
    assert "spoken in Russian" in content["_prompt"] and "Tips (`tips`) are written in Russian" in content["_prompt"]
    assert "Romanian" not in content["_prompt"].split("## How to score")[0]
    assert seen["warmup_score"]["speech_language"] == "English" and "Russian" not in seen["warmup_score"]["_prompt"].split("## Transcript")[0]


def test_no_speech_tip_is_in_the_speech_language() -> None:
    pitch = Pitch(title="t", brief="b", audience=Audience.PUBLIC, ui_lang="ro")
    result = asyncio.run(delivery.assess_content(pitch, "", _metrics(), speech="ru"))
    assert result.tips == [delivery.NO_SPEECH_TIPS["ru"]]


def test_filler_judge_jury_and_live_prompts_follow_the_speaker(monkeypatch) -> None:
    seen = _capture_llm(
        monkeypatch,
        {
            "filler_judge": delivery.FillerVerdicts(fillers=[1]),
            "jury_questions": DraftQuestions(questions=[DraftQuestion(juror="strict", text="Почему именно сейчас?")]),
            "live_check": LiveCheck(relevance=80, substance=80, comment="Exemplu bun"),
        },
    )
    verdicts = asyncio.run(delivery.judge_fillers(_spoken("Deci, ideea e bună."), "ro"))
    assert verdicts == {0: True} and seen["filler_judge"]["speech_language"] == "Romanian"

    pitch = Pitch(title="t", brief="b", audience=Audience.BUSINESS, ui_lang="en")
    draft = asyncio.run(jury._draft(pitch, "Ну, это продукт", "easy", "ru"))
    assert seen["jury_questions"]["speech_language"] == "Russian" and "conversational Russian" in seen["jury_questions"]["_prompt"]
    questions = jury.one_per_juror(draft, pitch, "ru").questions
    assert [q.text for q in questions] == ["Почему именно сейчас?", *jury._FALLBACK_TEXTS["ru"][Audience.BUSINESS][1:]]

    sent: list[dict] = []

    async def send_json(data):
        sent.append(data)

    analyzer = LiveAnalyzer()
    analyzer.committed = "pizza mea preferată e cu ciuperci pentru că mi-o făcea bunica".split()
    asyncio.run(live._check_content(SimpleNamespace(send_json=send_json), analyzer, pitch))
    assert "same language as the speaker's words" in seen["live_check"]["_prompt"]
    assert sent[-1] == {"type": "content", "t": 0.0, "score": 80, "comment": "Exemplu bun"}


def test_refine_writes_in_the_language_of_the_text(monkeypatch) -> None:
    draft = refine.ImproveDraft(
        language="ru",
        weaknesses=["нет цифр"],
        blocks=[refine.DraftBlock(kind="hook", text="Представь утро без кофе.")],
        changes=["добавлен хук"],
    )
    seen = _capture_llm(monkeypatch, {"refine_improve": draft})
    req = RefineRequest(text="Мой питч про кофе", audience=Audience.PUBLIC, mode=RefineMode.IMPROVE)
    result = asyncio.run(refine.run_refine(req, "ro"))
    assert seen["refine_improve"]["fallback_language"] == "Russian"  # по буквам текста, не по интерфейсу
    assert result.notes == ["Слабое место: нет цифр", "Изменено: добавлен хук"]
    assert result.blocks[0].title == "Хук" and result.text == "Хук: Представь утро без кофе."


def test_refine_without_ai_keeps_the_text_language_and_reports_in_the_interface_language(monkeypatch) -> None:
    from google.genai import errors as genai_errors

    _capture_llm(monkeypatch, {"refine_structure": genai_errors.APIError(503, {"error": {"message": "overloaded"}})})
    req = RefineRequest(text="Привет, это мой питч.\nНаша проблема.", audience=Audience.PUBLIC, mode=RefineMode.STRUCTURE)
    result = asyncio.run(refine.run_refine(req, "ro"))
    assert [b.title for b in result.blocks][:2] == ["Хук", "Проблема"]
    assert result.notes == [refine.NOTE_TEXTS["ro"]["offline"]]


def test_fit_slides_labels_follow_the_language_of_the_texts() -> None:
    draft = FitSlidesDraft(
        language="ru", slides=[FitSlide(n=1, title="Проблема", kind="talk", text="Люди забывают."), FitSlide(n=2, title="Демо", kind="demo", text="")]
    )
    assert assemble(draft).text == "[Слайд 1 — Проблема]\nЛюди забывают.\n\n[Слайд 2 — Демо]\nВремя демо."
    unknown = draft.model_copy(update={"language": ""})
    assert assemble(unknown, "ro").slides[1].text == "Momentul demo."


# --- разбор и жюри: всё на языке речи ---


def _delivery_fakes(monkeypatch, pitch: Pitch, transcript: Transcript, previous: dict | None = None) -> dict:
    """previous — прошлый разбор этого раунда (из него берётся язык речи, если распознавание его не определит)."""
    state: dict = {"azure": 0, "saved": {}}

    async def to_wav(_audio):
        return b"wav"

    async def transcribe(_wav):
        return transcript

    async def assess(_wav):
        state["azure"] += 1
        return PronunciationAssessment(
            overall_score=80, accuracy_score=80, fluency_score=80, prosody_score=90, words_total=10,
            mispronounced_words_count=0, unexpected_breaks_count=0, monotone=False, tips=["Confident pronunciation — keep it up."],
        )  # fmt: skip

    monkeypatch.setattr(delivery, "resolve_pitch", lambda _rid: pitch)
    monkeypatch.setattr(delivery, "to_wav16k", to_wav)
    monkeypatch.setattr(delivery, "transcribe", transcribe)
    monkeypatch.setattr(delivery.pronunciation, "assess", assess)
    monkeypatch.setattr(delivery.game_api, "get_ai_result", lambda _rid, kind: previous if kind == "delivery" else None)
    monkeypatch.setattr(delivery.game_api, "save_ai_result", lambda _rid, kind, payload: state["saved"].update({kind: payload}))
    state["llm"] = _capture_llm(
        monkeypatch,
        {
            "content_score": delivery.ContentAssessment(
                criteria=[CriterionScore(name="topic", score=70, quote="это продукт")], tips=["Совет 1", "Совет 2", "Совет 3"]
            ),
            "filler_judge": delivery.FillerVerdicts(fillers=[]),
        },
    )
    return state


RUSSIAN_PITCH = Transcript(
    "Ну, это продукт", [Word("Ну,", 0.0, 0.3), Word("это", 0.5, 0.8), Word("продукт", 5.0, 5.4)], 60.0, language="ru"
)


def test_delivery_feedback_follows_the_detected_speech_not_the_interface(monkeypatch) -> None:
    pitch = Pitch(title="My Morning", brief="b", audience=Audience.PUBLIC, ui_lang="ro")
    state = _delivery_fakes(monkeypatch, pitch, RUSSIAN_PITCH)
    result = asyncio.run(delivery.run_delivery("rnd_x", b"audio", []))
    assert (result.speech_lang, result.transcript) == ("ru", "Ну, это продукт")
    assert result.pronunciation is None  # произношение — только для английской речи
    assert state["llm"]["content_score"]["speech_language"] == "Russian"
    assert [e.text for e in result.events] == ["«ну»", "Пауза 4,2 с посреди фразы"]
    assert state["saved"]["delivery"]["speech_lang"] == "ru"


def test_english_speech_gets_pronunciation_whatever_the_interface(monkeypatch) -> None:
    pitch = Pitch(title="My Morning", brief="b", audience=Audience.PUBLIC, ui_lang="ro")
    # прошлый разбор был по-русски — Azure не стартует заранее, а запускается, когда речь оказалась английской
    english = Transcript("I wake up early.", _spoken("I wake up early.").words, 60.0, "en")
    state = _delivery_fakes(monkeypatch, pitch, english, previous={"speech_lang": "ru"})
    result = asyncio.run(delivery.run_delivery("rnd_x", b"audio", []))
    assert result.speech_lang == "en" and state["azure"] == 1
    assert result.pronunciation is not None and result.pronunciation.tips == ["Confident pronunciation — keep it up."]


def test_undetected_language_falls_back_to_the_rounds_speech_then_default(monkeypatch) -> None:
    pitch = Pitch(title="My Morning", brief="b", audience=Audience.PUBLIC, ui_lang="ru")
    unknown = Transcript("Ciao a tutti.", _spoken("Ciao a tutti.").words, 60.0, None)
    state = _delivery_fakes(monkeypatch, pitch, unknown, previous={"speech_lang": "ro"})
    result = asyncio.run(delivery.run_delivery("rnd_x", b"audio", []))
    assert result.speech_lang == "ro" and state["azure"] == 0  # не английская — Azure не запускался
    assert state["llm"]["content_score"]["speech_language"] == "Romanian"

    state = _delivery_fakes(monkeypatch, pitch, unknown, previous=None)
    result = asyncio.run(delivery.run_delivery("rnd_x", b"audio", []))
    assert result.speech_lang == default_lang()  # не язык интерфейса (ru)


def test_jury_questions_and_voices_use_the_speech_language(settings_env) -> None:
    pitch = Pitch(title="t", brief="b", audience=Audience.BUSINESS, ui_lang="ro")
    store: dict = {("r1", "delivery"): {"transcript": "Ну, это продукт", "speech_lang": "ru"}}
    voiced: list[tuple[str, str, str]] = []

    async def synthesize(text, voice, lang="en"):
        voiced.append((text, voice.openai_instructions, lang))
        return b"mp3"

    settings_env.setattr(jury, "resolve_pitch", lambda _rid: pitch)
    settings_env.setattr(jury.game_api, "get_ai_result", lambda rid, kind: store.get((rid, kind)))
    settings_env.setattr(jury.game_api, "save_ai_result", lambda rid, kind, payload: store.__setitem__((rid, kind), payload))
    settings_env.setattr(jury, "synthesize", synthesize)
    seen = _capture_llm(
        settings_env, {"jury_questions": DraftQuestions(questions=[DraftQuestion(juror="strict", text="Сколько это стоит?")])}
    )
    result = asyncio.run(jury.run_jury_questions("r1"))
    assert seen["jury_questions"]["speech_language"] == "Russian"
    assert result.questions[0].text == "Сколько это стоит?"
    assert result.questions[1].text == jury._FALLBACK_TEXTS["ru"][Audience.BUSINESS][1]
    assert {lang for _, _, lang in voiced} == {"ru"} and all("Speak Russian" in style for _, style, _ in voiced)

    # старый разбор без языка речи — STT_LANGUAGE, а не язык интерфейса раунда
    store.clear()
    store[("r2", "delivery")] = {"transcript": "Hello"}
    voiced.clear()
    asyncio.run(jury.run_jury_questions("r2"))
    assert {lang for _, _, lang in voiced} == {default_lang()}


def test_jury_speaks_with_native_voices_in_russian_and_romanian(settings_env) -> None:
    settings_env.setenv("ELEVENLABS_VOICE_ID", "shared")
    settings_env.setenv("ELEVENLABS_VOICE_ID_STRICT", "")
    settings_env.setenv("ELEVENLABS_VOICE_ID_SKEPTIC", "")
    settings_env.setenv("ELEVENLABS_VOICE_ID_KIND", "kind_en")
    settings_env.setenv("ELEVENLABS_VOICE_ID_SKEPTIC_RO", "")  # пустой — голоса для всех языков
    get_settings.cache_clear()

    assert jury.voice_for("strict", "ru").elevenlabs_voice_id == "9AjtU6o19uipv7QL8dLL"
    assert jury.voice_for("kind", "ro").elevenlabs_voice_id == "QtObtrglHRaER8xlDZsr"
    assert jury.voice_for("kind", "en").elevenlabs_voice_id == "kind_en"
    assert jury.voice_for("strict", "en").elevenlabs_voice_id == "shared"
    assert jury.voice_for("skeptic", "ro").elevenlabs_voice_id == "shared"
    # пол из персоны доходит до модели: от него зависят формы в русском и румынском
    assert jury.JURORS["kind"].persona.startswith("A woman.") and jury.JURORS["strict"].persona.startswith("A man.")


def test_jury_answers_are_judged_in_the_language_of_the_answer(monkeypatch) -> None:
    pitch = Pitch(title="t", brief="b", audience=Audience.BUSINESS, ui_lang="ro")
    question = {"questions": [{"id": "q1", "juror": "strict", "text": "Сколько это стоит?", "audio_url": "/x.mp3"}]}
    store: dict = {("r1", "jury_questions"): question, ("r1", "delivery"): {"transcript": "x", "speech_lang": "ru"}}
    monkeypatch.setattr(jury, "resolve_pitch", lambda _rid: pitch)
    monkeypatch.setattr(jury.game_api, "get_ai_result", lambda rid, kind: store.get((rid, kind)))
    monkeypatch.setattr(jury.game_api, "save_ai_result", lambda rid, kind, payload: store.__setitem__((rid, kind), payload))
    answers = iter([Transcript("", [], 0.0, None), Transcript("It costs ten dollars.", [], 3.0, "en")])

    async def to_wav(_audio):
        return b"wav"

    async def transcribe(_wav):
        return next(answers)

    monkeypatch.setattr(jury, "to_wav16k", to_wav)
    monkeypatch.setattr(jury, "transcribe", transcribe)
    seen = _capture_llm(monkeypatch, {"jury_answer": jury.AnswerAssessment(score=70, comment="Good, but no numbers.")})

    silent = asyncio.run(jury.run_jury_answer("r1", "q1", b"audio"))  # язык не определён — язык питча (ru)
    assert silent.comment == "Мы не услышали ответа." and store[("r1", "jury_answer")]["speech_lang"] == "ru"
    spoken = asyncio.run(jury.run_jury_answer("r1", "q1", b"audio"))
    assert spoken.comment == "Good, but no numbers." and seen["jury_answer"]["answer_language"] == "English"
    assert "feedback_language" not in seen["jury_answer"] and "comment` — one short sentence **in English**" in seen["jury_answer"]["_prompt"]
    assert store[("r1", "jury_answer")]["speech_lang"] == "en"
    skipped = asyncio.run(jury.run_jury_skip("r1", "q1"))
    assert skipped.comment == jury.ANSWER_TEXTS["ru"]["skipped"]
    assert RoundStateError("no_delivery").message("ro") == "Trimite mai întâi pitch-ul la analiză (delivery)"


# --- живой зал ---


def test_live_recognition_language_ignores_the_interface(monkeypatch) -> None:
    """Deepgram получает язык речи раунда из прошлого разбора, иначе STT_LANGUAGE; Scribe определяет язык сам."""
    asked: list[str] = []

    def make_stream(_settings, lang):
        asked.append(lang)
        raise MissingKeyError("test")  # дальше не идём: нужен только выбранный язык

    closed: list[str] = []

    async def close(code, reason):
        closed.append(reason)

    known: dict[str, str | None] = {"r1": "ro", "r2": None}
    monkeypatch.setattr(live, "make_stream", make_stream)
    monkeypatch.setattr(live, "resolve_pitch", lambda _rid: Pitch(title="t", brief="b", audience=Audience.PUBLIC, ui_lang="ru"))
    monkeypatch.setattr(live, "known_speech_lang", lambda rid: known[rid])
    for rid in known:
        asyncio.run(live.run_live(SimpleNamespace(close=close), rid))
    assert asked == ["ro", default_lang()] and len(closed) == 2

    s = get_settings()
    assert "language=ro" in live.DeepgramStream(s.model_copy(update={"deepgram_api_key": "k"}), "ro").url
    assert "language_code" not in live.ElevenLabsStream(s.model_copy(update={"elevenlabs_api_key": "k"}), "ro").url
