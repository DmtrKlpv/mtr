# Этап 2 — потоковая отдача через FFmpeg

## Эндпоинт
`GET /stream?id=<track_id>&format=<fmt>&timeOffset=<sec>`

| format   | поведение |
|----------|-----------|
| raw      | файл как есть, FileResponse с поддержкой Range |
| mp3_128 / mp3_256 / mp3_320 / mp3_v0 | перекодирование libmp3lame |
| flac     | перекодирование в FLAC |
| alac     | перекодирование в ALAC (fragmented MP4) |

`timeOffset` > 0 при `raw`: поток копируется без перекодирования (`-c:a copy`).
Для WAV/WMA/APE копирование невозможно — выдаётся mp3_320, в заголовке указана подмена.

## Заголовки ответа
- `X-MTR-Format` — фактически отданный формат
- `X-MTR-Strategy` — file | copy | encode
- `X-MTR-Substituted` — 1, если формат подменён (no-upscale или fallback)
- `X-MTR-Substitution-Reason` — percent-encoded причина

## Коды ответа
- 400 — неизвестный формат, некорректный timeOffset
- 404 — трек не найден/missing, файл отсутствует на диске
- 503 + Retry-After — достигнут лимит MTR_MAX_TRANSCODES
- 500 — ffmpeg не запустился

## Гарантии процессов
- Обрыв клиента, отмена корутины, ошибка — всегда kill + wait + освобождение слота.
- stderr во временном файле (без deadlock на заполненном PIPE).
- Чтение stdout через to_thread: event loop не блокируется.

## Конфигурация
- `MTR_MAX_TRANSCODES` (по умолчанию 4, диапазон 1–64)
- `MTR_DEFAULT_TRANSCODE` (по умолчанию raw)

## Осознанные ограничения
- Авторизации нет — появится на этапе 3. Не выставлять наружу до этого.
- Range для encode-потоков не поддерживается (длина заранее неизвестна).
- Сеть: копирование и перекодирование идут «на лету», без кэша.

## Проверено сквозным прогоном (uvicorn + curl)
- raw с Range: 206 Partial Content, Content-Range корректен
- mp3_128 из FLAC: 200, audio/mpeg, ~129 КБ за 8 с
- flac: magic `fLaC`; alac: контейнер `ftyp M4A`
- timeOffset=3 на 8-секундном треке: усечённый поток
- обрыв клиента на 4-минутном FLAC: ffmpeg жив во время стрима (1), после обрыва 0

## Требование окружения
`ffmpeg` должен быть в PATH. В Docker ставится через apt.
Вне Docker: установите системный ffmpeg или добавьте симлинк на бинарник.
