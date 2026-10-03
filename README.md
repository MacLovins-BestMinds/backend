# Stage Zero — бэкенд

FastAPI: игровой движок (`/api/game`), авторизация (`/api/auth`) и AI-движок (`/api/ai`). Документация API — `/docs`.

## Запуск

```sh
cp .env.example .env      # заполнить ключи, см. ниже
./run.sh                  # или: docker compose up
```

Нужен системный `ffmpeg` (перегонка записей m4a → WAV). Без обязательных AI-ключей сервер не стартует и пишет, каких не хватает; запустить без ключей на моках — `AI_MOCK=1 ./run.sh`.

## AI-движок

| Что | Провайдер по умолчанию | Переменные |
|---|---|---|
| Распознавание записей (`delivery`, ответы жюри) | ElevenLabs Scribe v2 (REST, таймкоды слов) | `STT_PROVIDER`, `STT_MODEL`, `STT_LANGUAGE` |
| Живое распознавание для зала (`WS /api/ai/live`) | ElevenLabs Scribe v2 Realtime (WebSocket, VAD) | `LIVE_STT_PROVIDER`, `LIVE_STT_MODEL` |
| Голоса жюри (mp3 в `static/jury/`) | ElevenLabs `eleven_flash_v2_5` | `TTS_PROVIDER`, `TTS_MODEL`, `ELEVENLABS_VOICE_ID*` |
| Оценка содержания, вопросы жюри, refine | Gemini | `GEMINI_API_KEY`, `GEMINI_MODEL` |

### Где взять ключи

- **`ELEVENLABS_API_KEY`** — https://elevenlabs.io/app/settings/api-keys (нужны права на Speech to Text и Text to Speech).
- **`ELEVENLABS_VOICE_ID`** — https://elevenlabs.io/app/voice-library: выбрать голос, который хорошо говорит по-русски, добавить в My Voices и скопировать его ID (⋯ → Copy voice ID). Чтобы у трёх членов жюри были разные голоса, задайте `ELEVENLABS_VOICE_ID_STRICT`, `_KIND`, `_SKEPTIC`; незаданные берут общий `ELEVENLABS_VOICE_ID`.
- **`GEMINI_API_KEY`** — https://aistudio.google.com/apikey.

Ключи живут только на сервере: приложение шлёт звук в наш бэкенд, а не напрямую в ElevenLabs.

### Вернуться на старых провайдеров

```env
STT_PROVIDER=openai
STT_MODEL=whisper-1
LIVE_STT_PROVIDER=deepgram
TTS_PROVIDER=openai
TTS_MODEL=gpt-4o-mini-tts
OPENAI_API_KEY=...
DEEPGRAM_API_KEY=...
```

Провайдеры независимы: можно, например, оставить Scribe для записей и Deepgram для живого потока.

### Полезное

- `?mock=1` у любого `/api/ai/*` или `AI_MOCK=1` — примеры ответов без ключей.
- Ответы AI кэшируются на диске (`.ai_cache/`): повтор той же записи отвечает мгновенно. `AI_CACHE=0` — выключить, удалить папку — сбросить.

## Вход, история и живая оценка

- **Вход.** `POST /api/auth/register` и `POST /api/auth/login` (ник и пароль) отдают JWT. Приложение шлёт его в `Authorization: Bearer …`; с токеном раунд создаётся от имени вошедшего пользователя. Ник, заведённый раньше без пароля (`/api/game/auth`), можно закрепить паролем через `register` — история раундов остаётся.
- **История и прогресс.** `GET /api/game/progress` (нужен токен): все сыгранные раунды с баллами и привычками речи (темп, паразиты, паузы, повторы), средние за последние 5 раундов против 5 предыдущих, серия дней, до следующего звания, советы по цифрам. `GET /api/game/rounds/{id}/review` — разбор своего старого раунда.
- **Живая оценка содержания.** В `WS /api/ai/live` раз в ~7 секунд (когда набралось 12 новых слов) Gemini оценивает последние слова: по теме ли и есть ли содержание. Клиенту уходит событие `content` с баллом 0–100 и короткой подсказкой; промпт — `app/ai/prompts/live_check.md`.
- **Ругань.** Бранные слова (английские и русские, в том числе в латинице после перевода расшифровки) ищет `find_profanity` в `delivery_metrics.py`. В живом потоке сразу уходит событие `profanity`; в разборе это отметка в тексте и минус 8 баллов подачи за слово (не больше 32); жюри и разбор содержания отчитывают за неё (правила в промптах `content_score`, `jury_questions`, `jury_answer`, `live_check`).
- **Паразиты по смыслу.** «um», «э-э», «типа» — паразиты всегда. «like», «so», «you know», «kind of» бывают обычными словами: в разборе их судит Gemini по контексту (`filler_judge.md`), в живом потоке — та же проверка содержания. Если Gemini не ответил, разбор оценивает такие слова по положению во фразе.

## Тесты

```sh
python -m pytest -q
```
