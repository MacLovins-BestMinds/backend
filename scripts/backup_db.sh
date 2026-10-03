#!/bin/bash
# Скрипт создания бэкапа базы данных SQLite
BACKUP_DIR="backups"
DB_FILE="stage_zero.db"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="${BACKUP_DIR}/stage_zero_${TIMESTAMP}.db"

mkdir -p "$BACKUP_DIR"

if [ -f "$DB_FILE" ]; then
    cp "$DB_FILE" "$BACKUP_FILE"
    echo "✓ Бэкап базы данных успешно создан: $BACKUP_FILE"
else
    echo "База данных $DB_FILE еще не создана."
    exit 1
fi
