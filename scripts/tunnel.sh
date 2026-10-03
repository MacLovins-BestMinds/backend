#!/bin/bash
# Запуск HTTPS и WSS туннеля для подключения мобильного приложения с реального телефона
PORT=8000

echo "=== Запуск туннеля Stage Zero Backend (порт $PORT) ==="

if command -v cloudflared &> /dev/null; then
    echo "Используется Cloudflare Tunnel..."
    exec cloudflared tunnel --url http://localhost:$PORT
elif command -v npx &> /dev/null; then
    echo "cloudflared не найден, запускаем localtunnel через npx..."
    exec npx localtunnel --port $PORT
elif command -v ngrok &> /dev/null; then
    echo "Используется ngrok..."
    exec ngrok http $PORT
else
    echo "Установите cloudflared (brew install cloudflared) или используйте npx localtunnel --port $PORT"
    exit 1
fi
