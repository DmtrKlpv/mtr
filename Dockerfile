# =====================================================================
# MTR Music Server
#
# Собирается под linux/amd64, linux/arm64 и linux/arm/v7.
#
# Сборка под arm/v7 идёт через QEMU-эмуляцию и потому медленная
# (apt-get ffmpeg занимает ~9 минут). Это нормально, не зависание.
#
# Колёса под armv7l для cffi, uvloop, httptools и pyyaml есть только
# на piwheels — он подключён как extra-index-url. PyPI остаётся
# основным индексом, piwheels добирает недостающее.
#
# --only-binary=:all: обязателен: без него pip молча пробует собрать
# пакет из исходников и падает на отсутствии gcc через полчаса
# эмуляции. С ним падение происходит сразу и с понятным текстом.
# =====================================================================
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    MUSIC_DIR=/music \
    DATA_DIR=/data \
    MTR_LOG_LEVEL=INFO

# ffmpeg нужен аудиоядру. Один слой с очисткой apt-списков.
# libwebp*: колёса armv7 (piwheels) линкуются динамически к системным libwebp.so.7 и другим.
# Без них PIL._webp не импортируется, и Pillow пишет ?WEBP support not installed?.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
        libwebp7 libwebpmux3 libwebpdemux2 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Зависимости копируются отдельно: слой pip переиспользуется, пока
# не изменился requirements.txt.
COPY requirements.txt ./
RUN pip install --no-cache-dir --only-binary=:all: \
        --extra-index-url https://www.piwheels.org/simple \
        -r requirements.txt

# --chown копирует сразу с нужным владельцем, без отдельного chown -R
# (он бы создал лишний слой, дублирующий весь код в образе).
COPY --chown=1000:1000 app ./app

# Скрипты обслуживания (самопроверка образа). Нужны внутри контейнера:
# docker exec mtr-app sh /app/scripts/selfcheck.sh
COPY --chown=1000:1000 scripts ./scripts

# Каталог состояния: обе БД, ключ шифрования, кэш обложек.
RUN mkdir -p /data/covers /music \
 && useradd --create-home --uid 1000 --user-group mtr \
 && chown -R mtr:mtr /data

USER mtr

EXPOSE 4533
VOLUME ["/music", "/data"]

# HEALTHCHECK в exec-форме одной строкой: точка с запятой внутри CMD
# превращала предыдущую версию в нерабочую конструкцию.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4533/healthz', timeout=3).status == 200 else 1)"]

# --loop auto и --http auto оставлены намеренно: uvicorn сам выберет
# uvloop/httptools там, где они установлены, и откатится на asyncio/h11
# там, где их нет (arm/v7).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "4533", "--loop", "auto", "--http", "auto"]