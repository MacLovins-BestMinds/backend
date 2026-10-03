#!/bin/bash
set -e

# Активация виртуального окружения если есть
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

# Проверка и установка зависимостей при необходимости
python3 -m pip install -q -r requirements.txt

# Запуск FastAPI
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
