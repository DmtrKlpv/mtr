# Этап 1 — фундамент

## Что сделано
- Две изолированные схемы: `app/db/schema_users.sql`, `app/db/schema_music.sql`
- Миграции по `PRAGMA user_version` (`app/db/migrations.py`)
- Асинхронный слой SQLite: WAL, ретраи на BUSY/LOCKED, вложенные транзакции
- Крипто: Fernet (обратимый, для Subsonic token) + PBKDF2 (для веб-логина)
- Парсер тегов mutagen: mp3/flac/ogg/opus/m4a/aac/wav/aiff/wma/ape/wv/mpc
- Репозитории users / tracks, скан-ядро с diff по (path, size, mtime, missing)
- CLI: `initdb`, `createuser`, `scan`, `stats`

## Инварианты
1. Между базами нет SQL JOIN/FK. `playlists.owner_id` — мягкая ссылка.
2. Трек идентифицируется путём; `tracks.id` не переиспользуется.
3. Удалённый файл → `missing=1`, не DELETE.
4. Парсер не бросает исключений: ошибка файла = `ok=False` + причина.

## Запуск
    docker build -t mtr:0.1.0 .
    docker run -p 4533:4533 -v /path/music:/music -v mtr-data:/data \
        -e MTR_SECRET_KEY=... mtr:0.1.0
    docker exec -it <id> python -m app.cli initdb --bootstrap-admin admin --bootstrap-password '...'
    docker exec -it <id> python -m app.cli scan --mode full
