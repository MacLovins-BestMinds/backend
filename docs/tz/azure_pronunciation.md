# ТЗ · Интеграция Azure Speech Service (Pronunciation Assessment)

Stage Zero · Версия от 3 октября 2026  
Модуль фонетического и интонационного анализа английской речи для питч-тренажёра.

---

## 1. Цель и контекст

### 1.1. Зачем это нужно
В текущей версии приложения за распознавание речи отвечает **ElevenLabs Scribe**, который возвращает только сухой текст (слова и таймкоды). Для тренировки питчей на английском языке критически важен глубокий анализ качества речи:
1. **Ударение в словах (Word Stress):** выявление ошибок смещения ударения (например, *«de-VEL-op-ment»*, а не *«devel-OP-ment»*).
2. **Точность звуков и акцент (Pronunciation & Phonemes):** понимание, какие именно фонемы звучат грязно или искажённо.
3. **Интонация и живость речи (Prosody):** определение монотонности, вопросительных интонаций, спадов энергетики.
4. **Беглость (Fluency):** паузы между словами, запинки и плавность потока речи.

**Azure Speech Service (Pronunciation Assessment)** позволяет в **одном API-запросе** закрыть сразу две задачи:
* Получить качественную транскрипцию (Speech-to-Text).
* Получить исчерпывающий отчёт по произношению, ударениям, интонации и фонемам.

---

## 2. Архитектура и разделение ролей AI-сервисов

| Сервис | Роль в проекте | За что отвечает |
| :--- | :--- | :--- |
| **Azure Speech Service** | **STT + Pronunciation Assessment** | Транскрибация речи на английском + оценка ударений, фонем, интонации (Prosody) и беглости (Fluency). |
| **ElevenLabs** | **TTS (Text-to-Speech)** | Высококачественная озвучка членов жюри (строгий, добрый, скептик) через Flash v2.5. |
| **Google Gemini (3.5-flash-lite / 3.8)** | **LLM (Logic & Content)** | Оценка структуры и смысла питча, генерация вопросов жюри, оценка ответов, AI Refine текста. |

---

## 3. Требования к Azure Pronunciation Assessment

### 3.1. Режим работы: Unscripted (без эталонного текста)
Спикер говорит свободный питч (свой текст или экспромт по карточке темы). Используется режим **Unscripted Assessment**:
* Параметр `ReferenceText` оставляется пустым (`""`).
* Azure сам распознаёт произнесённый текст и параллельно оценивает произношение каждого распознанного слова.

### 3.2. Параметры оценки (Comprehensive Assessment)
Запрос в Azure конфигурируется следующими параметрами:
* `GradingSystem`: `HundredMark` (шкала 0–100).
* `Granularity`: `Phoneme` (анализ вплоть до слогов и фонем).
* `Dimension`: `Comprehensive` (полный скоринг: точность + беглость + просодия + полнота).
* `EnableProsodyAssessment`: `True` — **обязательно** (анализ мелодики, тона, ударений и выразительности).
* `EnableMiscue`: `True` — выявление пропусков, замен и искажений.

---

## 4. Контракты данных (Data Schemas)

### 4.1. Модели данных бэкенда (`backend/app/ai/schemas.py`)

```python
from typing import Literal
from pydantic import BaseModel, Field

class SyllableScore(BaseModel):
    syllable: str = Field(description="Слог, например 'vel'")
    score: float = Field(ge=0, le=100, description="Точность произношения слога")
    stress_expected: bool | None = Field(None, description="Ожидалось ли ударение на этот слог")
    stress_actual: bool | None = Field(None, description="Поставил ли спикер ударение")
    stress_error: bool = Field(False, description="Ошибка в ударении на этом слоге")

class PhonemeScore(BaseModel):
    phoneme: str = Field(description="Символ фонемы (IPA или Arpabet)")
    score: float = Field(ge=0, le=100)

class WordPronunciation(BaseModel):
    word: str
    score: float = Field(ge=0, le=100, description="Общий балл слова")
    error_type: Literal["None", "Mispronunciation", "Omission", "Insertion"] = "None"
    stress_error: bool = Field(False, description="Есть ли ошибка ударения в слове")
    syllables: list[SyllableScore] = Field(default_factory=list)
    phonemes: list[PhonemeScore] = Field(default_factory=list)

class PronunciationAssessment(BaseModel):
    overall_score: float = Field(ge=0, le=100, description="Общий балл английской речи")
    accuracy_score: float = Field(ge=0, le=100, description="Фонетическая точность")
    fluency_score: float = Field(ge=0, le=100, description="Беглость и темп речи")
    prosody_score: float = Field(ge=0, le=100, description="Интонация, тон и живость")
    completeness_score: float = Field(ge=0, le=100, description="Полнота речи")
    words_total: int
    mispronounced_words_count: int
    stress_errors_count: int
    words: list[WordPronunciation] = Field(default_factory=list)
    tips: list[str] = Field(default_factory=list, description="Рекомендации по произношению")
```

### 4.2. Обновление `DeliveryResponse`

Поле `pronunciation` добавляется в существующий ответ эндпоинта `POST /api/ai/rounds/{round_id}/delivery`:

```python
class DeliveryResponse(BaseModel):
    transcript: str
    scores: Scores
    metrics: Metrics
    events: list[TimelineEvent]
    tips: list[str]
    pronunciation: PronunciationAssessment | None = None  # None для русской речи
```

---

## 5. Бэкенд-реализация

### 5.1. Переменные окружения (`backend/.env`)

```env
# Azure Cognitive Services (Speech)
AZURE_SPEECH_KEY=your_azure_speech_key_here
AZURE_SPEECH_REGION=eastus              # или westeurope, swedencentral
AZURE_SPEECH_LANGUAGE=en-US             # en-US | en-GB

# Выбор провайдера STT
STT_PROVIDER=azure                      # azure | elevenlabs | openai
```

### 5.2. Модуль `backend/app/ai/azure_speech.py`
Реализует асинхронную функцию:
```python
async def assess_pronunciation(audio_wav_16k: bytes, language: str = "en-US") -> tuple[str, PronunciationAssessment]:
    """
    Принимает 16кГц моно WAV.
    Возвращает (распознанный_текст, структура_оценки_произношения).
    """
```
* Если `STT_PROVIDER == "azure"` и язык раунда `en`, вызывается `assess_pronunciation`.
* Если язык `ru`, аудио направляется в стандартный пайплайн ElevenLabs Scribe.
* Предусматривается безопасный fallback: при сбое Azure возвращается стандартная транскрипция без падения сервера.

---

## 6. Фронтенд-реализация (React Native / Expo)

### 6.1. Обновление типов (`frontend/src/api/types.ts`)
Синхронизация типов с `PronunciationAssessment`, `WordPronunciation`, `SyllableScore`.

### 6.2. Новый UI-компонент на экране разбора (`frontend/app/result.tsx`)
Добавление карточки **«Произношение и интонация (English)»**:

```
+-----------------------------------------------------------+
| 🇬🇧 Английское произношение: 84 / 100                      |
|                                                           |
| Точность: 86%  ·  Интонация: 82%  ·  Беглость: 84%        |
|                                                           |
| ⚠️ Замечено ошибок:                                       |
| • 2 неверных ударения (de-VEL-opment, tech-NOL-ogy)       |
| • 1 смазанное слово (entrepreneur)                        |
|                                                           |
| Интерактивный транскрипт:                                 |
| "Hello everyone, today our [development]* platform will..."|
+-----------------------------------------------------------+
* По нажатию на слово всплывает плашка:
  "development" — Ударение сделано на 3 слог вместо 2-го!
```

---

## 7. Этапы внедрения

| Этап | Задача | Трудозатраты |
| :--- | :--- | :--- |
| **Этап 1: Azure Setup** | Создать ресурс Azure Speech (F0 Free Tier на 5 часов/мес) в Azure Portal, скопировать Key и Region в `.env`. | 15 мин |
| **Этап 2: Бэкенд клиент** | Создать `app/ai/azure_speech.py` с вызовом Pronunciation Assessment REST/SDK API и парсингом JSON-отчёта. | 1.5 часа |
| **Этап 3: Интеграция пайплайна** | Подключить вызов в `app/ai/delivery.py`, дополнить Pydantic-схему `DeliveryResponse`. | 45 мин |
| **Этап 4: Фронтенд типы & клиент** | Обновить `types.ts`, `client.ts` и моки `mocks.ts`. | 30 мин |
| **Этап 5: UI на экране разбора** | Сверстать `PronunciationCard` с подсветкой ударений и бейджами интонации на `app/result.tsx`. | 1 час |
| **Этап 6: Тестирование** | Прогон аудиозаписи на английском, проверка отдачи скоринга и корректной работы жюри. | 30 мин |
