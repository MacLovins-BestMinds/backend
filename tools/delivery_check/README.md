# Проверка разбора подачи на эталонных записях

Записи с заранее известными ошибками: паразиты (`bad_fillers`), ловушки без паразитов (`traps`),
паузы посреди фразы и после неё (`bad_pauses`), быстрая и медленная речь, чистая речь (`good`).
Эталон — поле `expect` в `texts.py`.

```sh
cd backend
python tools/delivery_check/make_recordings.py      # macOS: say + ffmpeg
PYTHONPATH=. python tools/delivery_check/check.py   # нужны ключи распознавания в .env
```

Голос Samantha говорит быстрее 175 слов/мин даже на малой скорости — поэтому балл за темп у `good` ниже 100.
